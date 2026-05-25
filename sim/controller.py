"""
Контроллер (то, что в реальности будет на Raspberry Pi).

Стейт-машины для потстила и LM-ректификации, ШИМ клапана узла отбора,
барокоррекция, tripwires, watchdog.

Принципиально отделён от физики: видит только sensors{} (как с ESP) и
выдаёт Outputs (как в ESP по native API).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from still import Outputs, mass_to_abv_vol


class Phase(str, Enum):
    IDLE = "IDLE"
    INIT = "INIT"
    HEAT_UP = "HEAT_UP"
    STABILIZE = "STABILIZE"
    HEADS = "HEADS"
    STABILIZE2 = "STABILIZE2"
    BODY = "BODY"
    TAILS = "TAILS"
    SHUTDOWN = "SHUTDOWN"
    DONE = "DONE"
    EMERGENCY = "EMERGENCY"
    PAUSE = "PAUSE"


class Mode(str, Enum):
    POTSTILL = "POTSTILL"  # одиночная перегонка без разделения
    REFLUX = "REFLUX"  # ректификация с разделением голов/тела/хвостов


@dataclass
class Recipe:
    """Параметры сессии, обычно загружаются из БД."""

    mode: Mode = Mode.REFLUX

    # Целевая мощность по фазам, %
    p_heat_up: float = 100
    p_work: float = 60
    p_tails: float = 80

    # Температуры (с учётом барокоррекции)
    t_head_start: float = 78.0  # колонна вышла на режим
    t_head_max_body: float = 78.5  # выход из body — T_head поднялась = хвосты пошли
    t_kub_body: float = 88.0  # переход heads → body по T_kub
    t_kub_tails: float = 95.0
    t_kub_stop: float = 99.0

    # Стабилизация
    t_stable_s: int = 600  # 10 мин total reflux после прогрева
    t_stable2_s: int = 300  # 5 мин между heads и body

    # ШИМ
    pwm_period_s: float = 60.0
    duty_heads: float = 0.15  # 15% — головы медленно
    duty_body: float = 0.40
    duty_tails: float = 0.70

    # Прочее
    t_dwell_max: float = 30  # сек удержания T_head > порога перед переходом

    # Safety
    t_water_out_warn: float = 50.0
    t_water_out_reduce: float = 65.0
    t_water_out_stop: float = 80.0
    t_kub_emergency: float = 105.0

    # Phase timeouts — защита от «застревания» (см. форум HelloDistiller, БКУ-099)
    t_heat_up_max_s: int = 3 * 3600  # 3ч — если за это время не вышли на режим, ALERT
    t_body_max_s: int = 6 * 3600  # 6ч — если BODY не выходит, ALERT

    # STABILIZE условие выхода
    stable_dT_dt_thresh: float = 0.02  # °C/мин — порог «успокоилась»

    # Антизахлёб
    duty_flood_warn: float = 0.75  # warn при средней нагрузке > 75 %
    dt_water_flood_warn: float = 30  # °C дельта по воде — pre-flood симптом

    # PZEM-style: расхождение между заданной и измеренной мощностью
    power_mismatch_kW: float = 0.5  # порог |P_set - P_meas|
    power_mismatch_dwell_s: int = 10  # сколько секунд держаться, чтобы триггернуть


def baro_correct(T_head_C: float, P_atm_hPa: float) -> float:
    """Корректирует T_head к стандартному давлению 1013.25 hPa.
    Коэффициент 0.037 °C/hPa для этанола."""
    return T_head_C - 0.037 * (P_atm_hPa - 1013.25)


@dataclass
class ControllerState:
    """Внутреннее состояние контроллера."""

    phase: Phase = Phase.IDLE
    phase_started_at: float = 0.0  # sim-time когда вошли в фазу

    # ШИМ
    pwm_phase_start: float = 0.0
    valve_takeoff_open: bool = False

    # Tripwires
    last_alert: str = ""
    alerts: list[str] = field(default_factory=list)
    _alerts_max: int = 200  # ring-buffer, защита от memory growth

    # Watchdog
    last_pi_command_at: float = 0.0

    # Гистерезис: время с момента T_head стабильно > порога
    t_head_over_at: float | None = None

    # Для условий по dT/dt: последняя точка
    t_head_prev: float | None = None
    t_head_prev_time: float | None = None
    dT_head_per_min: float = 0.0  # сглаженная

    # Warning-флаги (без повторных алертов)
    warned_water: bool = False
    warned_flood: bool = False

    # PZEM-расхождение
    power_mismatch_since: float | None = None  # sim-time когда начало расходиться

    # Последняя выданная мощность (для PZEM)
    last_cmd_power: float = 0.0


class Controller:
    """
    Стейт-машина. Метод tick(t_sim, sensors) возвращает Outputs.
    Работает с теми же sensors{} что вернёт реальный ESP через aioesphomeapi.
    """

    def __init__(self, recipe: Recipe | None = None):
        self.r = recipe or Recipe()
        self.st = ControllerState()
        self.session_started_at: float | None = None
        self.duty_current: float = 0.0
        self.pause_requested: bool = False

    def start(self, t_sim: float, recipe: Recipe | None = None):
        if recipe:
            self.r = recipe
        self.st = ControllerState(
            phase=Phase.INIT,
            phase_started_at=t_sim,
            pwm_phase_start=t_sim,
            last_pi_command_at=t_sim,
        )
        self.session_started_at = t_sim
        self.duty_current = 0.0
        self.pause_requested = False

    def request_stop(self, t_sim: float):
        """Корректный стоп: используем sim-time, не wall clock."""
        if self.st.phase not in (Phase.IDLE, Phase.DONE, Phase.EMERGENCY):
            self._transition(Phase.SHUTDOWN, t_sim)

    def emergency(self, reason: str, t_sim: float):
        if self.st.phase != Phase.EMERGENCY:
            self.st.phase = Phase.EMERGENCY
            self.st.phase_started_at = t_sim
            self.st.last_alert = f"EMERGENCY: {reason}"
            self._add_alert(f"{t_sim:.0f}s: {self.st.last_alert}")

    def acknowledge_emergency(self):
        """Оператор подтвердил аварию — возвращаемся в IDLE,
        но сессия не возобновляется автоматически."""
        if self.st.phase == Phase.EMERGENCY:
            self.st.phase = Phase.IDLE
            self.st.last_alert = ""
            self._add_alert("operator ACK emergency → IDLE")

    def _add_alert(self, msg: str):
        self.st.alerts.append(msg)
        if len(self.st.alerts) > self.st._alerts_max:
            self.st.alerts = self.st.alerts[-self.st._alerts_max:]

    def _update_dT_dt(self, t_sim: float, t_head: float):
        """Сглаженная производная T_head, °C/мин."""
        if math.isnan(t_head):
            return
        st = self.st
        if st.t_head_prev is None or st.t_head_prev_time is None:
            st.t_head_prev = t_head
            st.t_head_prev_time = t_sim
            return
        dt = t_sim - st.t_head_prev_time
        if dt < 30:  # не пересчитываем чаще раза в 30 sec
            return
        rate_per_min = (t_head - st.t_head_prev) / dt * 60
        # EMA, τ=2мин
        alpha = dt / (120 + dt)
        st.dT_head_per_min += (rate_per_min - st.dT_head_per_min) * alpha
        st.t_head_prev = t_head
        st.t_head_prev_time = t_sim

    def tick(self, t_sim: float, sensors: dict, pi_alive: bool = True) -> Outputs:
        """Один шаг логики. Возвращает выходы для железа."""
        outs = Outputs()
        r = self.r
        st = self.st

        if pi_alive:
            st.last_pi_command_at = t_sim
        watchdog_dt = t_sim - st.last_pi_command_at

        t_kub = sensors.get("T_kub", float("nan"))
        t_head = sensors.get("T_head", float("nan"))
        t_water_out = sensors.get("T_water_out", float("nan"))
        duty_avg = sensors.get("duty_avg", 0.0)

        self._update_dT_dt(t_sim, t_head)

        # === Hard safety triggers (всегда, независимо от фазы) ===
        active_phases = {
            Phase.HEAT_UP, Phase.STABILIZE, Phase.HEADS,
            Phase.STABILIZE2, Phase.BODY, Phase.TAILS,
        }
        if watchdog_dt > 30.0 and st.phase in active_phases:
            self.emergency(f"watchdog: нет команд от Pi {watchdog_dt:.0f}s", t_sim)
        if math.isnan(t_kub) and st.phase in active_phases:
            self.emergency("T_kub sensor lost", t_sim)
        if not math.isnan(t_kub) and t_kub > r.t_kub_emergency:
            self.emergency(f"T_kub > {r.t_kub_emergency}°C", t_sim)
        if sensors.get("estop"):
            self.emergency("E-stop pressed", t_sim)
        if sensors.get("bimetal_tripped"):
            self.emergency("bimetal tripped", t_sim)
        if not math.isnan(t_water_out) and t_water_out > r.t_water_out_stop:
            self.emergency(f"T_water_out > {r.t_water_out_stop}°C", t_sim)

        # PZEM-style: расхождение заданной мощности и реально измеренной
        # (детектирует пробой SSR в проводящее состояние / залипание реле / обрыв)
        p_meas = sensors.get("P_heater_kW", 0.0)
        p_expected = st.last_cmd_power * 5.0  # P_max = 5 kW; в проде брать из конфига
        if st.phase in active_phases:
            if abs(p_meas - p_expected) > r.power_mismatch_kW:
                if st.power_mismatch_since is None:
                    st.power_mismatch_since = t_sim
                elif t_sim - st.power_mismatch_since > r.power_mismatch_dwell_s:
                    self.emergency(
                        f"SSR mismatch: cmd {p_expected:.2f}kW, meas {p_meas:.2f}kW", t_sim
                    )
            else:
                st.power_mismatch_since = None

        # === EMERGENCY ===
        if st.phase == Phase.EMERGENCY:
            outs.heater_power = 0
            outs.valve_takeoff = False
            outs.contactor_enable = False
            outs.valve_water = (t_sim - st.phase_started_at) < 300
            return outs

        # === IDLE / DONE ===
        if st.phase in (Phase.IDLE, Phase.DONE):
            return outs

        # Барокоррекция T_head
        p_atm = sensors.get("P_atm_hPa", 1013.25)
        t_head_corr = baro_correct(t_head, p_atm) if not math.isnan(t_head) else t_head

        # Warnings (one-shot)
        if not math.isnan(t_water_out) and t_water_out > r.t_water_out_warn and not st.warned_water:
            st.warned_water = True
            self._add_alert(f"{t_sim:.0f}s: WARN T_water_out {t_water_out:.1f}°C > {r.t_water_out_warn}°C")

        # Антизахлёб (косвенно: высокий duty + рост ΔT воды)
        dt_water = t_water_out - sensors.get("T_water_in", 12.0) if not math.isnan(t_water_out) else 0
        if (
            duty_avg > r.duty_flood_warn
            and dt_water > r.dt_water_flood_warn
            and not st.warned_flood
            and st.phase in active_phases
        ):
            st.warned_flood = True
            self._add_alert(
                f"{t_sim:.0f}s: WARN pre-flood — duty {duty_avg*100:.0f}% + ΔT_воды {dt_water:.1f}°C"
            )

        # Контактор и вода включены пока сессия активна
        outs.contactor_enable = True
        outs.valve_water = True

        power_scale = 1.0
        if not math.isnan(t_water_out) and t_water_out > r.t_water_out_reduce:
            power_scale = 0.5

        phase_elapsed = t_sim - st.phase_started_at

        # === Стейт-машина ===
        if st.phase == Phase.INIT:
            self._transition(Phase.HEAT_UP, t_sim)

        elif st.phase == Phase.HEAT_UP:
            outs.heater_power = r.p_heat_up / 100.0 * power_scale
            outs.valve_takeoff = False

            if r.mode == Mode.REFLUX:
                if not math.isnan(t_head_corr) and t_head_corr > r.t_head_start:
                    self._transition(Phase.STABILIZE, t_sim)
            else:  # POTSTILL: пар пошёл, переходим в BODY
                if not math.isnan(t_kub) and t_kub > 78:
                    self._transition(Phase.BODY, t_sim)

            if phase_elapsed > r.t_heat_up_max_s:
                self.emergency(
                    f"HEAT_UP > {r.t_heat_up_max_s//60} мин, не вышли на режим", t_sim
                )

        elif st.phase == Phase.STABILIZE:
            outs.heater_power = r.p_work / 100.0 * power_scale
            outs.valve_takeoff = False
            self.duty_current = 0.0
            # Выход: timeout И колонна успокоилась (dT/dt мал)
            time_ok = phase_elapsed > r.t_stable_s
            stable_ok = abs(st.dT_head_per_min) < r.stable_dT_dt_thresh
            if time_ok and stable_ok:
                self._transition(Phase.HEADS, t_sim)
            elif phase_elapsed > 2 * r.t_stable_s:
                # Жёсткий timeout если не дождались стабильности
                self._add_alert(
                    f"{t_sim:.0f}s: STABILIZE timeout, dT/dt={st.dT_head_per_min:.3f}, переходим"
                )
                self._transition(Phase.HEADS, t_sim)

        elif st.phase == Phase.HEADS:
            outs.heater_power = r.p_work / 100.0 * power_scale
            self.duty_current = r.duty_heads
            outs.valve_takeoff = self._pwm(t_sim, r.duty_heads, r.pwm_period_s)
            if not math.isnan(t_kub) and t_kub > r.t_kub_body:
                self._transition(Phase.STABILIZE2, t_sim)

        elif st.phase == Phase.STABILIZE2:
            outs.heater_power = r.p_work / 100.0 * power_scale
            outs.valve_takeoff = False
            self.duty_current = 0.0
            if phase_elapsed > r.t_stable2_s:
                self._transition(Phase.BODY, t_sim)

        elif st.phase == Phase.BODY:
            outs.heater_power = r.p_work / 100.0 * power_scale
            self.duty_current = r.duty_body
            outs.valve_takeoff = self._pwm(t_sim, r.duty_body, r.pwm_period_s)

            # Конец тела: T_head стабильно выше порога t_dwell сек
            t_head_over = (
                not math.isnan(t_head_corr)
                and t_head_corr > r.t_head_max_body
                and phase_elapsed > 60
            )
            if t_head_over:
                if st.t_head_over_at is None:
                    st.t_head_over_at = t_sim
                elif t_sim - st.t_head_over_at > r.t_dwell_max:
                    self._transition(Phase.TAILS, t_sim)
            else:
                st.t_head_over_at = None

            if not math.isnan(t_kub) and t_kub > r.t_kub_tails:
                self._transition(Phase.TAILS, t_sim)

            # Body timeout
            if phase_elapsed > r.t_body_max_s:
                self._add_alert(
                    f"{t_sim:.0f}s: BODY timeout {r.t_body_max_s//3600}ч, "
                    f"T_kub={t_kub:.1f}, переходим в TAILS"
                )
                self._transition(Phase.TAILS, t_sim)

        elif st.phase == Phase.TAILS:
            outs.heater_power = r.p_tails / 100.0 * power_scale
            # POTSTILL не должен делать ШИМ — клапан всегда открыт
            if r.mode == Mode.POTSTILL:
                self.duty_current = 1.0
                outs.valve_takeoff = True
            else:
                self.duty_current = r.duty_tails
                outs.valve_takeoff = self._pwm(t_sim, r.duty_tails, r.pwm_period_s)
            if not math.isnan(t_kub) and t_kub > r.t_kub_stop:
                self._transition(Phase.SHUTDOWN, t_sim)

        elif st.phase == Phase.SHUTDOWN:
            outs.heater_power = 0
            outs.valve_takeoff = False
            self.duty_current = 0.0
            outs.valve_water = phase_elapsed < 60
            outs.contactor_enable = False
            if phase_elapsed > 60:
                self._transition(Phase.DONE, t_sim)

        # Специальный случай: POTSTILL в BODY — клапан всегда открыт
        if r.mode == Mode.POTSTILL and st.phase == Phase.BODY:
            self.duty_current = 1.0
            outs.valve_takeoff = True

        # Запоминаем последнюю команду на мощность для PZEM-проверки
        st.last_cmd_power = outs.heater_power

        return outs

    def _transition(self, new_phase: Phase, t_sim: float):
        old = self.st.phase
        self.st.phase = new_phase
        self.st.phase_started_at = t_sim
        self.st.pwm_phase_start = t_sim
        # Сбрасываем гистерезис-таймеры при смене фазы
        self.st.t_head_over_at = None
        self._add_alert(f"{t_sim:.0f}s: {old.value} → {new_phase.value}")

    def _pwm(self, t_sim: float, duty: float, period: float) -> bool:
        """Slow PWM с привязкой к фазе. Возвращает текущее состояние клапана."""
        if duty <= 0:
            return False
        if duty >= 1:
            return True
        t_in_period = (t_sim - self.st.pwm_phase_start) % period
        t_open = duty * period
        return t_in_period < t_open
