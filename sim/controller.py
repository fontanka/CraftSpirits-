"""
Контроллер (то, что в реальности будет на Raspberry Pi).

Стейт-машины для потстила и LM-ректификации, ШИМ клапана узла отбора,
барокоррекция, tripwires, watchdog.

Принципиально отделён от физики: видит только sensors{} (как с ESP) и
выдаёт Outputs (как в ESP по native API).
"""
from __future__ import annotations

import math
import time
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

    # Watchdog
    last_pi_command_at: float = 0.0  # для имитации pi watchdog


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

    def request_stop(self):
        self.st.phase = Phase.SHUTDOWN
        self.st.phase_started_at = time.time()

    def emergency(self, reason: str, t_sim: float):
        if self.st.phase != Phase.EMERGENCY:
            self.st.phase = Phase.EMERGENCY
            self.st.phase_started_at = t_sim
            self.st.last_alert = f"EMERGENCY: {reason}"
            self.st.alerts.append(f"{t_sim:.0f}s: {self.st.last_alert}")

    def tick(self, t_sim: float, sensors: dict, pi_alive: bool = True) -> Outputs:
        """Один шаг логики контроллера. Возвращает выходы для железа."""
        outs = Outputs()
        r = self.r
        st = self.st

        # Watchdog: если Pi не отвечает >30 сек, ESP сам в safe state
        # (тут мы и Pi и ESP в одном питоне, но имитируем разрыв)
        if pi_alive:
            st.last_pi_command_at = t_sim

        watchdog_dt = t_sim - st.last_pi_command_at
        if watchdog_dt > 30.0 and st.phase not in (Phase.IDLE, Phase.DONE, Phase.EMERGENCY):
            self.emergency(f"watchdog: нет команд от Pi {watchdog_dt:.0f}s", t_sim)

        # Hardware-level safety (ESP-side)
        t_kub = sensors.get("T_kub", float("nan"))
        t_head = sensors.get("T_head", float("nan"))
        t_water_out = sensors.get("T_water_out", float("nan"))

        if math.isnan(t_kub) and st.phase in (
            Phase.HEAT_UP, Phase.STABILIZE, Phase.HEADS,
            Phase.STABILIZE2, Phase.BODY, Phase.TAILS,
        ):
            self.emergency("T_kub sensor lost", t_sim)

        if not math.isnan(t_kub) and t_kub > r.t_kub_emergency:
            self.emergency(f"T_kub > {r.t_kub_emergency}°C", t_sim)

        if sensors.get("estop"):
            self.emergency("E-stop pressed", t_sim)
        if sensors.get("bimetal_tripped"):
            self.emergency("bimetal tripped", t_sim)

        # === EMERGENCY ===
        if st.phase == Phase.EMERGENCY:
            outs.heater_power = 0
            outs.valve_takeoff = False
            outs.contactor_enable = False
            # Вода остаётся открытой на остывание ещё 5 минут
            outs.valve_water = (t_sim - st.phase_started_at) < 300
            return outs

        # === IDLE / DONE ===
        if st.phase in (Phase.IDLE, Phase.DONE):
            return outs  # всё в нуле

        # Барокоррекция T_head
        p_atm = sensors.get("P_atm_hPa", 1013.25)
        t_head_corr = baro_correct(t_head, p_atm) if not math.isnan(t_head) else t_head

        # Tripwires по T_water_out (потеря охлаждения)
        if not math.isnan(t_water_out):
            if t_water_out > r.t_water_out_stop:
                self.emergency(f"T_water_out > {r.t_water_out_stop}°C", t_sim)
            elif t_water_out > r.t_water_out_warn and "water_warn" not in st.last_alert:
                st.last_alert = "water_warn: T_water_out высокая"
                st.alerts.append(f"{t_sim:.0f}s: {st.last_alert}")

        # Включаем контактор и воду как только не IDLE
        outs.contactor_enable = True
        outs.valve_water = True

        # === Power scaling по T_water_out ===
        power_scale = 1.0
        if not math.isnan(t_water_out) and t_water_out > r.t_water_out_reduce:
            power_scale = 0.5  # принудительно снижаем

        # === Стейт-машина ===
        if st.phase == Phase.INIT:
            # Один tick — переходим в HEAT_UP
            self._transition(Phase.HEAT_UP, t_sim)

        elif st.phase == Phase.HEAT_UP:
            outs.heater_power = r.p_heat_up / 100.0 * power_scale
            outs.valve_takeoff = False  # закрыт, total reflux

            # Условие выхода — T_head вышла на режим (для рефлюкса)
            # или T_kub дошла до точки кипения (для потстила)
            if r.mode == Mode.REFLUX:
                if not math.isnan(t_head_corr) and t_head_corr > r.t_head_start:
                    self._transition(Phase.STABILIZE, t_sim)
            else:  # POTSTILL — без отдельной стабилизации
                if not math.isnan(t_kub) and t_kub > 78:
                    self._transition(Phase.BODY, t_sim)

        elif st.phase == Phase.STABILIZE:
            outs.heater_power = r.p_work / 100.0 * power_scale
            outs.valve_takeoff = False  # total reflux
            self.duty_current = 0.0
            if t_sim - st.phase_started_at > r.t_stable_s:
                self._transition(Phase.HEADS, t_sim)

        elif st.phase == Phase.HEADS:
            outs.heater_power = r.p_work / 100.0 * power_scale
            self.duty_current = r.duty_heads
            outs.valve_takeoff = self._pwm(t_sim, r.duty_heads, r.pwm_period_s)

            # Переход в STABILIZE2 по T_kub
            if not math.isnan(t_kub) and t_kub > r.t_kub_body:
                self._transition(Phase.STABILIZE2, t_sim)

        elif st.phase == Phase.STABILIZE2:
            outs.heater_power = r.p_work / 100.0 * power_scale
            outs.valve_takeoff = False
            self.duty_current = 0.0
            if t_sim - st.phase_started_at > r.t_stable2_s:
                self._transition(Phase.BODY, t_sim)

        elif st.phase == Phase.BODY:
            outs.heater_power = r.p_work / 100.0 * power_scale
            self.duty_current = r.duty_body
            outs.valve_takeoff = self._pwm(t_sim, r.duty_body, r.pwm_period_s)

            # Условие конца тела: T_head пошла вверх (хвосты пробились)
            # или T_kub высокая
            if (
                not math.isnan(t_head_corr)
                and t_head_corr > r.t_head_max_body
                and t_sim - st.phase_started_at > 60
            ):
                # Запомним время превышения порога, если держится > t_dwell — переход
                if not hasattr(st, "t_head_over_at"):
                    st.t_head_over_at = t_sim
                elif t_sim - st.t_head_over_at > r.t_dwell_max:
                    self._transition(Phase.TAILS, t_sim)
                    if hasattr(st, "t_head_over_at"):
                        delattr(st, "t_head_over_at")
            else:
                if hasattr(st, "t_head_over_at"):
                    delattr(st, "t_head_over_at")

            if not math.isnan(t_kub) and t_kub > r.t_kub_tails:
                self._transition(Phase.TAILS, t_sim)

        elif st.phase == Phase.TAILS:
            outs.heater_power = r.p_tails / 100.0 * power_scale
            self.duty_current = r.duty_tails
            outs.valve_takeoff = self._pwm(t_sim, r.duty_tails, r.pwm_period_s)

            if not math.isnan(t_kub) and t_kub > r.t_kub_stop:
                self._transition(Phase.SHUTDOWN, t_sim)

        elif st.phase == Phase.SHUTDOWN:
            outs.heater_power = 0
            outs.valve_takeoff = False
            self.duty_current = 0.0
            # Вода ещё 60 сек на остывание
            outs.valve_water = (t_sim - st.phase_started_at) < 60
            outs.contactor_enable = False
            if t_sim - st.phase_started_at > 60:
                self._transition(Phase.DONE, t_sim)

        return outs

    def _transition(self, new_phase: Phase, t_sim: float):
        old = self.st.phase
        self.st.phase = new_phase
        self.st.phase_started_at = t_sim
        self.st.pwm_phase_start = t_sim
        self.st.alerts.append(f"{t_sim:.0f}s: {old.value} → {new_phase.value}")

    def _pwm(self, t_sim: float, duty: float, period: float) -> bool:
        """Slow PWM с привязкой к фазе. Возвращает текущее состояние клапана."""
        if duty <= 0:
            return False
        if duty >= 1:
            return True
        t_in_period = (t_sim - self.st.pwm_phase_start) % period
        t_open = duty * period
        return t_in_period < t_open
