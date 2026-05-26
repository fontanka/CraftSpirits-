"""
sim/hardware.py — модели «железа» с failure modes.

Stage 6: полный hardware module.
- LevelSensor: контактный датчик уровня в приёмнике голов (БКУ)
- Valve: 15-var state с failure modes из 12.11.6 (12 отказов)
- DS18B20: sentinel values, counterfeit families, EMI clustering (12.11.4/5)
- SSR: thermal model (Fotek counterfeit, Crydom premium)
- Contactor: coil chatter при low V

Принцип: каждый компонент имеет state vector и step(dt, inputs)
метод, мутирующий state. physics.py использует их как «реальное железо».
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum


# ============================================================================
# LEVEL SENSOR (БКУ сифонный transfer, см. 12.12)
# ============================================================================

@dataclass
class LevelSensorState:
    """Контактный кондуктометрический датчик «прибыла жидкость» (БКУ-07,
    russsam.ru/samogon-i-vodka.ru, артикул «датчик наполнения автомат
    отбора 07», ~1060₽, щуп 70×10mm, 2m кабель).

    Стоит НЕ внутри бутылки на уровне жидкости (как обычный поплавок), а
    на БОКОВОМ ВЫХОДЕ устройства автоперевода. В норме сухой; срабатывает
    в момент, когда давление в герметичной бутылке голов выдавливает
    первую каплю спирта через боковой выход — это и есть сигнал «голов
    набрано». Жидкость замыкает 2 электрода щупа → contact closed.

    Объём голов задан НЕ этим датчиком, а длиной щупа в бутылке (см.
    Still.DEFAULT_HEADS_CUP_VOLUME_L); датчик — просто триггер фазы."""
    raw_signal: bool = False
    debounced_signal: bool = False
    last_change_t: float = 0.0
    oxidation_level: float = 0.0
    mechanical_stuck_closed: bool = False
    mechanical_stuck_open: bool = False
    bounce_counter: int = 0


class LevelSensor:
    """Контактный датчик БКУ-07 на боковом выходе устройства автоперевода.
    Failure modes (учитывая что это «датчик пришедшей капли», не уровня):
    1. Окисление контактов: растёт со временем экспозиции в спиртовых парах.
       Оператор чистит наждачкой перед каждой сессией (форум БКУ-099).
    2. Засорение бокового выхода: твёрдые частицы (накипь, мука браги) могут
       заблокировать канал → переключение не сработает, головы перельются.
       В sim модель как mechanical_stuck_open.
    3. Bouncing — первые капли могут пунктирно замыкать-размыкать пока
       давление в бутылке не стабилизировалось. Особенно при низком ABV
       первых капель тела (78-80% — проводит лучше, потом ухудшается).
    4. False positive — splash при сильном отборе, капля проскочила в
       побочный выход случайно (например, при наклоне аппарата).
    5. False negative «капля прошла мимо» — если бутылка голов установлена
       криво, давление может не достигнуть выходного канала.

    Подключение к ESP: один pin как digital input с pull-up, второй — GND.
    Замыкается через жидкость (~0.5-5 kΩ зависит от ABV).
    """

    OXIDATION_FAILURE_THRESHOLD = 0.8
    BOUNCE_BASE_RATE = 0.01

    def __init__(self, debounce_window_ms: float = 500.0):
        self.s = LevelSensorState()
        self.debounce_window_ms = debounce_window_ms

    def step(self, dt: float, t_sim: float, physical_full: bool,
             vapor_exposure_factor: float = 1.0):
        s = self.s
        s.oxidation_level = min(1.0, s.oxidation_level + dt / 86400 * vapor_exposure_factor)

        if s.mechanical_stuck_open:
            new_raw = False
        elif s.mechanical_stuck_closed:
            new_raw = True
        elif s.oxidation_level > self.OXIDATION_FAILURE_THRESHOLD:
            if physical_full:
                new_raw = random.random() > (s.oxidation_level - 0.5)
            else:
                new_raw = random.random() < 0.01 * s.oxidation_level
        else:
            new_raw = physical_full
            if physical_full and random.random() < self.BOUNCE_BASE_RATE * dt:
                new_raw = not new_raw
                s.bounce_counter += 1

        if new_raw != s.raw_signal:
            s.raw_signal = new_raw
            s.last_change_t = t_sim

        if t_sim - s.last_change_t >= self.debounce_window_ms / 1000.0:
            s.debounced_signal = s.raw_signal

    def reset(self):
        """Чистка наждачкой — оператор сбрасывает окисление перед сессией."""
        self.s.oxidation_level = 0.0
        self.s.bounce_counter = 0


# ============================================================================
# VALVE (15-var state, см. 12.11.6 — 12 failure modes)
# ============================================================================

class CoilClass(str, Enum):
    """IEC 60085 thermal class — max coil temperature."""
    A = "A"   # 105°C
    E = "E"   # 120°C
    B = "B"   # 130°C
    F = "F"   # 155°C
    H = "H"   # 180°C


COIL_T_MAX = {
    CoilClass.A: 105, CoilClass.E: 120, CoilClass.B: 130,
    CoilClass.F: 155, CoilClass.H: 180,
}


@dataclass
class ValveState:
    """Полное состояние клапана (см. 12.11.6 — 15 переменных)."""
    # Динамика
    lift_fraction: float = 0.0  # [0..1] — фактическое положение штока
    open_delay_ms: float = 30   # задержка отклика на «открыть»
    close_delay_ms: float = 20

    # Износ
    cycle_count: int = 0
    seal_wear: float = 0.0           # 0..1 (FKM swell, particle damage)
    insulation_age: float = 0.0      # 0..1 (Arrhenius decay катушки)

    # Тепловой режим
    T_coil_C: float = 25.0
    coil_class: CoilClass = CoilClass.B

    # Загрязнение
    seat_debris: bool = False         # частица на седле → leak
    scale_mass_g: float = 0.0         # кальций (для water valve)
    stiction_force_N: float = 0.0     # binding после простоя

    # Электрика
    V_supply: float = 230.0
    contact_resistance_ohm: float = 0.01  # DIN 43650 терминалы
    snubber_present: bool = True

    # Flow
    Cv_effective: float = 1.0
    leak_rate_ml_s_when_closed: float = 0.0

    # Общее здоровье
    coil_health: float = 1.0          # 0=dead

    # Time tracking
    last_command: bool = False
    last_command_change_t: float = 0.0
    last_energized_t: float = 0.0
    closed_since_t: float | None = 0.0  # для stiction
    chatter_high_freq: bool = False  # buzz сейчас


class Valve:
    """Электромагнитный клапан с 12 failure modes (см. 12.11.6).

    cmd_open: bool — команда от контроллера ('open'/'close')
    Step возвращает effective_open_fraction [0..1] и leak_rate_ml_s.
    """

    # Параметры aging
    HEAT_SOAK_ALPHA = 0.02  # Cv drift per °C above T_ref
    T_REF_C = 25.0
    STICTION_BUILDUP_PER_HR = 0.5  # N/hr after long-off
    STICTION_THRESHOLD_HR = 4.0  # 4ч без активации → возможный stuck

    def __init__(self, coil_class: CoilClass = CoilClass.B,
                 snubber: bool = True, Cv_nominal: float = 1.0):
        self.s = ValveState(coil_class=coil_class, snubber_present=snubber,
                            Cv_effective=Cv_nominal)
        self.Cv_nominal = Cv_nominal

    def step(self, dt: float, t_sim: float, cmd_open: bool,
             T_ambient_C: float = 25.0,
             delta_P_water_hammer_Pa: float = 0.0) -> tuple[float, float]:
        """Один шаг. Возвращает (effective_open_fraction, leak_rate_ml_s).

        delta_P_water_hammer_Pa — гидроудар при close (для water valve)
        """
        s = self.s

        # Edge detect: команда сменилась
        cmd_changed = (cmd_open != s.last_command)
        if cmd_changed:
            s.last_command = cmd_open
            s.last_command_change_t = t_sim
            s.cycle_count += 1

            # 7. Inrush burnout если lift < min и слишком долго energized
            # (катушка пытается двинуть застрявший шток, греется до burnout)
            # — обрабатывается при coil thermal block ниже

            # 8. Inductive kickback при отключении без snubber
            if not cmd_open and not s.snubber_present:
                spike_energy = 0.5 * 0.1 * (s.V_supply * 5)**2 / 1e6  # rough
                s.insulation_age = min(1.0, s.insulation_age + spike_energy * 1e-7)

            # 10. Water hammer at close (для water valves)
            if not cmd_open and delta_P_water_hammer_Pa > 1e5:
                s.seal_wear = min(1.0, s.seal_wear + delta_P_water_hammer_Pa / 5e7)

            if not cmd_open:
                s.closed_since_t = t_sim
            else:
                s.closed_since_t = None

        # 4. Stiction после long-off
        if s.closed_since_t is not None:
            hours_off = (t_sim - s.closed_since_t) / 3600
            if hours_off > self.STICTION_THRESHOLD_HR:
                s.stiction_force_N = (
                    self.STICTION_BUILDUP_PER_HR * (hours_off - self.STICTION_THRESHOLD_HR)
                )

        # 6. Chatter при низком V
        if cmd_open and s.V_supply < 0.85 * 230:
            s.chatter_high_freq = True
        else:
            s.chatter_high_freq = False

        # 9. Terminal corrosion (random walk increase humidity-dependent)
        if random.random() < dt / 86400 / 30:  # ~раз в месяц
            s.contact_resistance_ohm += 0.001

        # Coil heating при energized.
        # Реальная AC solenoid после inrush: I_hold ≈ 50-100mA, P_steady ≈ 15-25W
        # (большая часть power реактивная, не диссипирует).
        # T_coil_steady_state ≈ 60-85 °C при ambient 25 °C.
        if cmd_open and s.coil_health > 0:
            # Pragmatic constant — реальный AC inductance/resistance modeling
            # требует phasor analysis, что излишне для нашего sim
            P_holding_W = 18.0 * (1 - s.insulation_age * 0.5)
            tau_thermal = 90.0  # 1.5 min к steady-state
            thermal_res_K_W = 2.5  # ~K/W to ambient through housing
            T_eq = T_ambient_C + P_holding_W * thermal_res_K_W
            s.T_coil_C += (T_eq - s.T_coil_C) * dt / tau_thermal

            # 5. Coil thermal drop-out (Arrhenius aging)
            T_max = COIL_T_MAX[s.coil_class]
            if s.T_coil_C > T_max:
                aging_factor = 2 ** ((s.T_coil_C - T_max) / 10)
                s.coil_health -= dt / 3600 / 1000 * aging_factor
                s.coil_health = max(0, s.coil_health)
        else:
            tau_cooling = 180.0
            s.T_coil_C += (T_ambient_C - s.T_coil_C) * dt / tau_cooling

        # 11. БКУ heat soak: Cv drifts ×1.5 при T_coil > T_ref
        Cv_factor = 1 + self.HEAT_SOAK_ALPHA * max(0, s.T_coil_C - self.T_REF_C)
        s.Cv_effective = self.Cv_nominal * Cv_factor

        # Effective lift fraction:
        target = 1.0 if cmd_open and s.coil_health > 0.3 else 0.0
        # Stiction может задержать opening:
        if cmd_open and s.stiction_force_N > 2.0:
            target *= 0.0  # not enough force, stuck

        # Sluggish: open_delay растёт со временем
        delay_ms = s.open_delay_ms if cmd_open else s.close_delay_ms
        delay_ms *= (1 + s.seal_wear * 2)  # 3x slower fully worn
        tau_lift = delay_ms / 1000
        if tau_lift > 0:
            alpha = dt / (tau_lift + dt)
            s.lift_fraction += (target - s.lift_fraction) * alpha

        # 1. Dribble — leak при cmd=closed (FKM swell или seat debris)
        leak_when_closed = 0.0
        if not cmd_open:
            base_leak = s.leak_rate_ml_s_when_closed
            if s.seat_debris:
                base_leak += 0.5  # 0.5 mL/s через частицу
            if s.seal_wear > 0.5:
                base_leak += s.seal_wear * 0.2
            leak_when_closed = base_leak

        # 2. Particle bypass — стохастический self-clear после следующего цикла
        if s.seat_debris and cmd_changed and random.random() < 0.3:
            s.seat_debris = False

        return s.lift_fraction, leak_when_closed


# ============================================================================
# DS18B20 (sentinel + counterfeit families, см. 12.11.4/5)
# ============================================================================

class CounterfeitFamily(str, Enum):
    """Известные семейства подделок DS18B20 (cpetrich/counterfeit_DS18B20)."""
    ORIGINAL = "original"
    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"
    C = "C"
    D1 = "D1"


@dataclass
class DS18B20State:
    rom_id: str = "28-00-00-00-00-00-00-00"
    counterfeit: CounterfeitFamily = CounterfeitFamily.ORIGINAL
    noise_sigma_C: float = 0.05
    drift_C_per_year: float = 0.2
    drift_accumulated_C: float = 0.0
    # Counters per-sensor (см. 12.11.4)
    crc_fail_count: int = 0
    sentinel_count: int = 0
    jump_count: int = 0
    last_valid_C: float = 25.0
    # Hangs at zero crossing (некоторые counterfeit families)
    hang_at_zero_crossing: bool = False
    # EMI clustering — флаг что SSR недавно щёлкнул
    last_ssr_switch_t: float = 0.0


class DS18B20:
    """DS18B20 модель с failure modes:
    - Sentinel -127.0 (disconnected)
    - Sentinel 85.0000 exact (power-on reset OR VDD glitch)
    - CRC fails кластеризуются вокруг SSR switching events
    - Counterfeit: 2-10× noise, drift accelerated, hang at 0°C crossing
    - Drift: 0.2-0.5°C/year original, 1-2°C/year counterfeit
    """

    FAMILY_PROFILES = {
        CounterfeitFamily.ORIGINAL: dict(noise=0.05, drift=0.2, zero_hang=False),
        CounterfeitFamily.A1:       dict(noise=0.15, drift=0.8, zero_hang=False),
        CounterfeitFamily.A2:       dict(noise=0.20, drift=1.0, zero_hang=True),
        CounterfeitFamily.B1:       dict(noise=0.10, drift=0.5, zero_hang=False),
        CounterfeitFamily.B2:       dict(noise=0.30, drift=1.5, zero_hang=False),
        CounterfeitFamily.C:        dict(noise=0.50, drift=2.0, zero_hang=True),
        CounterfeitFamily.D1:       dict(noise=0.40, drift=1.8, zero_hang=False),
    }

    def __init__(self, rom_id: str = "28-00-00-00-00-00-00-00",
                 family: CounterfeitFamily = CounterfeitFamily.ORIGINAL):
        self.s = DS18B20State(rom_id=rom_id, counterfeit=family)
        profile = self.FAMILY_PROFILES[family]
        self.s.noise_sigma_C = profile["noise"]
        self.s.drift_C_per_year = profile["drift"]
        self.s.hang_at_zero_crossing = profile["zero_hang"]
        self._disconnected = False
        self._power_glitch_p_per_step = 0.0001  # baseline 0.01% per step

    def read(self, dt: float, t_sim: float, true_T_C: float,
             ssr_switching: bool = False) -> float:
        """Возвращает то, что вернёт реальный DS18B20 (со всеми failure modes).
        true_T_C — физическая температура.
        ssr_switching — если True, повышенный CRC fail probability на этом шаге."""
        s = self.s

        # 1. Disconnected → sentinel -127.0
        if self._disconnected:
            s.sentinel_count += 1
            return -127.0

        # 2. Drift accumulation
        s.drift_accumulated_C += s.drift_C_per_year * (dt / 86400 / 365)

        # 3. Counterfeit zero-crossing hang (some families)
        if s.hang_at_zero_crossing and abs(true_T_C) < 0.5:
            if random.random() < 0.3:
                return s.last_valid_C  # stuck

        # 4. Power-glitch → exactly 85.0000 (распознаваемо точностью)
        if random.random() < self._power_glitch_p_per_step:
            s.sentinel_count += 1
            s.last_ssr_switch_t = t_sim
            return 85.0000

        # 5. CRC fail clustering around SSR switching
        crc_fail_p = 0.00001  # baseline
        if ssr_switching:
            crc_fail_p = 0.05  # 5% chance при switching event
            s.last_ssr_switch_t = t_sim
        if random.random() < crc_fail_p:
            s.crc_fail_count += 1
            return float("nan")  # CRC fail — клиент должен retry

        # 6. Normal read с noise и drift
        noise = random.gauss(0, s.noise_sigma_C)
        reading = true_T_C + s.drift_accumulated_C + noise

        # Jump check (если предыдущее valid сильно отличается)
        if abs(reading - s.last_valid_C) > 20:
            s.jump_count += 1
        s.last_valid_C = reading

        return reading

    def disconnect(self):
        """Имитация обрыва провода."""
        self._disconnected = True

    def reconnect(self):
        self._disconnected = False


# ============================================================================
# SSR (twin-thermal, Fotek counterfeit vs Crydom premium)
# ============================================================================

@dataclass
class SSRState:
    T_junction_C: float = 25.0
    T_heatsink_C: float = 25.0
    is_genuine: bool = True  # Fotek-style fake имеет меньше Cu
    # Failure modes:
    fail_short: bool = False     # пробой → always-on regardless of cmd
    fail_open: bool = False      # обрыв → never-on
    snubber_present: bool = True


class SSR:
    """SSR (Crydom/Fotek class). Thermal model: P_dissipation = I × V_drop.
    Failure modes:
    - Counterfeit Fotek: thermal mass меньше → перегрев быстрее
    - Пробой (fail_short) → I always flows
    - Обрыв (fail_open) → no I
    """

    V_DROP_V = 1.6  # typical SSR forward V drop
    THERMAL_RES_C_PER_W = {True: 0.6, False: 1.5}  # K/W (genuine vs fake)
    THERMAL_TAU_S = {True: 30.0, False: 10.0}  # heatsink size differential

    def __init__(self, is_genuine: bool = True, snubber: bool = True):
        self.s = SSRState(is_genuine=is_genuine, snubber_present=snubber)

    def step(self, dt: float, cmd_on: bool, I_load_A: float,
             T_ambient_C: float = 25.0) -> tuple[bool, bool]:
        """Возвращает (current_flows, switching_event_this_tick).
        switching_event важен для DS18B20 CRC clustering."""
        s = self.s

        # Effective on/off (failure modes override cmd)
        if s.fail_short:
            current_flows = True
        elif s.fail_open:
            current_flows = False
        else:
            current_flows = cmd_on

        # Switching event detect — для EMI coupling в DS18B20
        # Compare with previous state
        prev_state = getattr(s, "_prev_current_flows", False)
        switching_event = current_flows != prev_state
        s._prev_current_flows = current_flows

        # Thermal dissipation
        thermal_res = self.THERMAL_RES_C_PER_W[s.is_genuine]
        tau = self.THERMAL_TAU_S[s.is_genuine]
        if current_flows:
            P_diss = self.V_DROP_V * I_load_A
            T_eq = T_ambient_C + P_diss * thermal_res
            s.T_junction_C += (T_eq - s.T_junction_C) * dt / tau
        else:
            s.T_junction_C += (T_ambient_C - s.T_junction_C) * dt / tau

        s.T_heatsink_C = T_ambient_C + (s.T_junction_C - T_ambient_C) * 0.5

        # Junction overtemperature → пробой в проводящее (latch)
        if s.T_junction_C > 100:
            s.fail_short = True

        return current_flows, switching_event


# ============================================================================
# PZEM-004T (energy meter, 1Hz update via Modbus RTU)
# ============================================================================

@dataclass
class PZEMState:
    """PZEM-004T published values (V, A, W, Wh, Hz, PF)."""
    V_rms: float = 230.0
    I_rms: float = 0.0
    P_W: float = 0.0
    energy_Wh: float = 0.0
    Hz: float = 50.0
    PF: float = 0.95
    crc_error: bool = False
    stuck_last_value: bool = False
    last_published_W: float = 0.0


class PZEM:
    """PZEM-004T: 1Hz Modbus update. Используется как ground-truth power
    measurement — controller сверяет с commanded heater duty (см. 12.13.8 +
    'SSR пробой' detection).

    Failure modes:
    - CRC error (длинный кабель, EMI) → controller должен retry
    - Stuck value (firmware bug на counterfeit) → replays last reading
    - Update lag — 1Hz цикл независимо от sim dt
    """

    UPDATE_INTERVAL_S = 1.0  # PZEM физически обновляется раз в секунду

    def __init__(self, crc_error_p_per_read: float = 0.001):
        self.s = PZEMState()
        self.crc_error_p = crc_error_p_per_read
        self._last_update_t: float = -10.0
        self._buffered_W: float = 0.0
        self._buffered_I: float = 0.0
        self._buffered_V: float = 230.0

    def step(self, dt: float, t_sim: float, instantaneous_P_W: float,
             V_mains: float = 230.0):
        """Внутренний учёт энергии (continuous integration over dt)."""
        self.s.energy_Wh += instantaneous_P_W * dt / 3600
        self._buffered_W = instantaneous_P_W
        self._buffered_V = V_mains
        self._buffered_I = instantaneous_P_W / max(V_mains, 1)

    def read(self, t_sim: float) -> dict | None:
        """Контроллер вызывает раз в Nms. Возвращает dict или None если PZEM
        ещё не обновился с прошлого опроса (1Hz limit) или CRC error."""
        if t_sim - self._last_update_t < self.UPDATE_INTERVAL_S:
            return None
        self._last_update_t = t_sim

        if random.random() < self.crc_error_p:
            self.s.crc_error = True
            return None

        self.s.crc_error = False

        if self.s.stuck_last_value:
            return {
                "V_rms": self.s.V_rms, "I_rms": self.s.I_rms,
                "P_W": self.s.last_published_W,
                "energy_Wh": self.s.energy_Wh,
                "Hz": self.s.Hz, "PF": self.s.PF,
            }

        self.s.V_rms = self._buffered_V
        self.s.I_rms = self._buffered_I
        self.s.P_W = self._buffered_W
        self.s.last_published_W = self._buffered_W
        return {
            "V_rms": self.s.V_rms, "I_rms": self.s.I_rms,
            "P_W": self.s.P_W, "energy_Wh": self.s.energy_Wh,
            "Hz": self.s.Hz, "PF": self.s.PF,
        }


# ============================================================================
# CONTACTOR (coil chatter при low V)
# ============================================================================

@dataclass
class ContactorState:
    enabled: bool = False
    V_coil: float = 230.0
    chatter: bool = False  # buzz при V_coil < 0.85 * V_nominal
    contact_wear: float = 0.0  # 0..1, от arc damage при chatter
    # Burned contacts → contact_resistance растёт


class Contactor:
    """Силовой контактор (KM1 в схеме). Главная failure mode — chatter
    при просадке V на катушке (см. 12.13.8 RU electrical environment)."""

    V_NOMINAL = 230.0
    CHATTER_THRESHOLD_FRAC = 0.85
    DROP_OUT_FRAC = 0.65  # if V < этого — катушка не держит

    def __init__(self):
        self.s = ContactorState()

    def step(self, dt: float, cmd_enable: bool, V_coil: float):
        s = self.s
        s.V_coil = V_coil

        if V_coil < self.DROP_OUT_FRAC * self.V_NOMINAL:
            s.enabled = False
            s.chatter = False
            return

        if cmd_enable:
            if V_coil < self.CHATTER_THRESHOLD_FRAC * self.V_NOMINAL:
                s.chatter = True
                # Arc damage от chatter
                s.contact_wear = min(1.0, s.contact_wear + dt / 3600 * 0.1)
            else:
                s.chatter = False
            s.enabled = True
        else:
            s.enabled = False
            s.chatter = False
