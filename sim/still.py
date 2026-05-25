"""
Физическая модель куба + колонны + обратного холодильника + узла отбора (LM).

Не претендует на точный CFD/VLE. Достаточно для валидации стейт-машины:
куб греется, кипит, колонна обогащает пар по этанолу, при открытом клапане
узла отбора жидкость уходит в продукт, барокоррекция работает,
тепловой баланс по воде охлаждения замыкается.

Единицы СИ внутри (kg, J, K, Pa), но T хранится в °C для удобства.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def antoine_water(T_C: float) -> float:
    """Давление насыщенного пара воды (Pa). Antoine, 1..100°C."""
    A, B, C = 8.07131, 1730.63, 233.426  # mmHg form
    P_mmHg = 10 ** (A - B / (C + T_C))
    return P_mmHg * 133.322


def antoine_ethanol(T_C: float) -> float:
    """Давление насыщенного пара этанола (Pa). Antoine, 20..150°C."""
    A, B, C = 8.20417, 1642.89, 230.300
    P_mmHg = 10 ** (A - B / (C + T_C))
    return P_mmHg * 133.322


def boiling_T(x_eth_mol: float, P_atm: float = 101325) -> float:
    """T кипения бинарной смеси этанол-вода (мольная доля). Возвращает °C."""
    x_eth_mol = max(0.0, min(1.0, x_eth_mol))
    lo, hi = 70.0, 105.0
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        P_sum = x_eth_mol * antoine_ethanol(mid) + (1 - x_eth_mol) * antoine_water(mid)
        if P_sum > P_atm:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def vapor_eth_mol(x_eth_mol: float, T_C: float) -> float:
    """Мольная доля этанола в паре над жидкостью x_eth_mol при T_C."""
    P_eth = antoine_ethanol(T_C) * x_eth_mol
    P_h2o = antoine_water(T_C) * (1 - x_eth_mol)
    if P_eth + P_h2o < 1e-9:
        return 0.0
    return P_eth / (P_eth + P_h2o)


def mol_to_mass(x_mol: float) -> float:
    """Мольная доля этанола → массовая доля."""
    M_eth, M_h2o = 46.07, 18.015
    n_eth = x_mol
    n_h2o = 1 - x_mol
    return (n_eth * M_eth) / (n_eth * M_eth + n_h2o * M_h2o + 1e-12)


def mass_to_mol(x_mass: float) -> float:
    """Массовая доля → мольная."""
    M_eth, M_h2o = 46.07, 18.015
    n_eth = x_mass / M_eth
    n_h2o = (1 - x_mass) / M_h2o
    return n_eth / (n_eth + n_h2o + 1e-12)


def mass_to_abv_vol(x_mass: float, T_C: float = 20) -> float:
    """Массовая доля → объёмная (об. %). Грубо: ρ_eth=789, ρ_h2o=998 при 20°C."""
    rho_eth, rho_h2o = 789.0, 998.0
    if x_mass <= 0:
        return 0.0
    if x_mass >= 1:
        return 100.0
    rho_mix = 1.0 / (x_mass / rho_eth + (1 - x_mass) / rho_h2o)
    v_eth = x_mass / rho_eth
    v_h2o = (1 - x_mass) / rho_h2o
    return 100.0 * v_eth / (v_eth + v_h2o)


def density_mix(x_mass: float, T_C: float = 80) -> float:
    """Плотность смеси, kg/m³. Простая модель: линейка по объёму."""
    rho_eth = 789.0 - 0.85 * (T_C - 20)
    rho_h2o = 998.0 - 0.4 * (T_C - 20)
    if x_mass <= 0:
        return rho_h2o
    if x_mass >= 1:
        return rho_eth
    return 1.0 / (x_mass / rho_eth + (1 - x_mass) / rho_h2o)


def cp_mix(x_mass: float) -> float:
    """Теплоёмкость, J/(kg·K). Этанол ~2440, вода ~4186."""
    return 2440 * x_mass + 4186 * (1 - x_mass)


def latent_heat(x_mass: float, T_C: float) -> float:
    """Теплота испарения, J/kg. Этанол ~841kJ/kg, вода ~2260kJ/kg при кипении."""
    L_eth = 841_000
    L_h2o = 2_260_000 - 2200 * (T_C - 100)
    return L_eth * x_mass + L_h2o * (1 - x_mass)


@dataclass
class StillState:
    """Текущее состояние физики (всё, что меняется во времени)."""

    # Куб
    V_kub: float = 18.0  # литры жидкости в кубе
    T_kub: float = 22.0  # °C
    x_kub_mass: float = 0.12  # массовая доля этанола (брага 12% обычно)

    # Колонна (упрощённо — один параметр сглаживания)
    T_head: float = 22.0
    x_head_mass: float = 0.0
    duty_avg: float = 0.0  # экспоненциально сглаженный duty cycle клапана

    # Охлаждение
    T_water_in: float = 12.0
    T_water_out: float = 12.0
    water_flow_lpm: float = 3.0  # L/min, выставлен вручную

    # Атмосфера
    P_atm_Pa: float = 101325.0  # 1013.25 hPa

    # Приёмник продукта
    V_product: float = 0.0
    x_product_mass: float = 0.0

    # Производные показатели (вычисляются)
    is_boiling: bool = False
    P_heater_kW: float = 0.0
    P_to_vapor_kW: float = 0.0
    m_dot_vapor_gps: float = 0.0  # g/s генерируемого пара


@dataclass
class StillParams:
    """Параметры железа — не меняются в течение сессии."""

    P_max_kW: float = 5.0
    V_kub_max_L: float = 25.0
    kub_thermal_mass_kJ_K: float = 5.0  # включая стенки куба

    # Колонна
    column_tau_s: float = 90.0  # время инерции колонны (флегма, насадка)
    column_max_separation: float = 0.97  # макс. достижимая массовая доля этанола

    # Охлаждение
    cp_water: float = 4186.0
    rho_water: float = 1000.0


@dataclass
class FaultInjection:
    """Что сломать для проверки safety-логики."""

    sensor_t_kub_fail: bool = False  # вернёт NaN
    sensor_t_head_fail: bool = False
    sensor_t_water_out_fail: bool = False
    valve_takeoff_stuck_open: bool = False
    valve_takeoff_stuck_closed: bool = False
    ssr_heater_stuck_on: bool = False  # пробой триака
    water_cutoff: bool = False  # клапан вроде открыт, а воды нет (закрылся кран)
    estop_pressed: bool = False
    bimetal_tripped: bool = False
    pi_disconnected: bool = False  # имитация падения Pi → ESP watchdog


@dataclass
class Outputs:
    """Команды от контроллера к 'железу'. То, что ESP пишет в GPIO."""

    heater_power: float = 0.0  # 0..1
    valve_takeoff: bool = False
    valve_water: bool = False
    contactor_enable: bool = False


class Still:
    """
    Физическая модель куба+колонны+узла отбора.
    step(dt) продвигает время на dt секунд.
    """

    def __init__(self, params: StillParams | None = None):
        self.p = params or StillParams()
        self.s = StillState()
        self.fault = FaultInjection()
        self.out = Outputs()
        self.t_sim_s = 0.0

    def reset(self, **kwargs):
        self.s = StillState(**kwargs)
        self.fault = FaultInjection()
        self.out = Outputs()
        self.t_sim_s = 0.0

    def step(self, dt: float):
        """Один шаг интегрирования (Эйлер, dt секунд sim-time)."""
        s, p = self.s, self.p

        # Применяем faults к командам
        heater_cmd = self.out.heater_power
        valve_takeoff_cmd = self.out.valve_takeoff
        contactor = self.out.contactor_enable

        if self.fault.estop_pressed or self.fault.bimetal_tripped:
            contactor = False  # safety chain физически разрывает
        if self.fault.ssr_heater_stuck_on:
            heater_cmd = 1.0  # пробой — мощность всегда 100%
        if self.fault.valve_takeoff_stuck_open:
            valve_takeoff_cmd = True
        if self.fault.valve_takeoff_stuck_closed:
            valve_takeoff_cmd = False

        # Мощность на ТЭН: только если контактор включён
        P_heater_W = heater_cmd * p.P_max_kW * 1000.0 if contactor else 0.0
        s.P_heater_kW = P_heater_W / 1000.0

        # === Тепловой баланс куба ===
        x_kub_mol = mass_to_mol(s.x_kub_mass)
        T_boil = boiling_T(x_kub_mol, s.P_atm_Pa)

        if s.T_kub < T_boil - 0.3:
            # Нагрев без кипения
            s.is_boiling = False
            m_kub = s.V_kub * density_mix(s.x_kub_mass, s.T_kub) / 1000.0  # kg
            C = m_kub * cp_mix(s.x_kub_mass) + p.kub_thermal_mass_kJ_K * 1000
            # Теплопотери в окружающую среду (упрощённо)
            Q_loss = 25 * (s.T_kub - 22)  # W, ~25 Вт/К
            dT = (P_heater_W - Q_loss) * dt / C
            s.T_kub += dT
            s.P_to_vapor_kW = 0
            s.m_dot_vapor_gps = 0
        else:
            # Кипение — вся (полезная) мощность идёт в пар
            s.is_boiling = True
            s.T_kub = T_boil  # фиксируем

            # Тепло на испарение (за вычетом потерь)
            Q_loss = 25 * (s.T_kub - 22)
            P_to_vapor = max(0, P_heater_W - Q_loss)
            s.P_to_vapor_kW = P_to_vapor / 1000.0
            L_vap = latent_heat(s.x_kub_mass, T_boil)
            m_dot_vapor = P_to_vapor / L_vap  # kg/s
            s.m_dot_vapor_gps = m_dot_vapor * 1000

            # Состав пара над кубом (равновесие)
            x_vapor_kub_mol = vapor_eth_mol(x_kub_mol, T_boil)

            # === Обогащение в колонне ===
            # Сглаживаем duty_avg по времени с τ = column_tau (колонна реагирует медленно)
            instant_duty = 1.0 if valve_takeoff_cmd else 0.0
            alpha = dt / (p.column_tau_s + dt)
            s.duty_avg += (instant_duty - s.duty_avg) * alpha

            # Эффективный reflux ratio R = (1-duty)/duty
            duty_clamped = max(0.001, min(0.999, s.duty_avg))
            R_eff = (1 - duty_clamped) / duty_clamped

            # Степень обогащения как функция R: больше R → ближе к азеотропу
            # Эмпирическая формула: f = R / (R + k), k ≈ 1.5 — настраивается по колонне
            f_sep = R_eff / (R_eff + 1.5)

            # Целевая мольная доля наверху
            x_head_mol_target = x_vapor_kub_mol + (
                mass_to_mol(p.column_max_separation) - x_vapor_kub_mol
            ) * f_sep
            x_head_mol_target = max(0.0, min(0.99, x_head_mol_target))

            # T_head с инерцией (колонна разогревается медленно)
            T_head_target = boiling_T(x_head_mol_target, s.P_atm_Pa)
            tau_head = 30.0
            s.T_head += (T_head_target - s.T_head) * dt / tau_head
            s.x_head_mass = mol_to_mass(x_head_mol_target)

            # === Поток продукта (только когда клапан открыт И не stuck closed) ===
            if valve_takeoff_cmd:
                # Жидкость с обратного холодильника = весь конденсат пара
                # Если клапан открыт — уходит в продукт
                rho_prod = density_mix(s.x_head_mass, 60)  # ~60°C после доохлаждения
                V_dt_L = (m_dot_vapor / rho_prod) * 1000 * dt  # литры
                if s.V_product + V_dt_L > 0:
                    s.x_product_mass = (
                        s.x_product_mass * s.V_product + s.x_head_mass * V_dt_L
                    ) / (s.V_product + V_dt_L)
                s.V_product += V_dt_L

                # Куб теряет столько же массы (в основном этанол, потому что забираем сверху)
                m_dt_kg = m_dot_vapor * dt
                rho_kub = density_mix(s.x_kub_mass, s.T_kub)
                V_dt_kub_L = m_dt_kg / rho_kub * 1000
                if s.V_kub > V_dt_kub_L:
                    m_kub_before = s.V_kub * rho_kub / 1000
                    eth_before = m_kub_before * s.x_kub_mass
                    eth_taken = m_dt_kg * s.x_head_mass
                    s.V_kub -= V_dt_kub_L
                    m_kub_after = s.V_kub * rho_kub / 1000
                    if m_kub_after > 0.01:
                        s.x_kub_mass = max(0, (eth_before - eth_taken) / m_kub_after)

        # === Тепловой баланс охлаждающей воды ===
        # Весь пар конденсируется на обратном холодильнике, отдавая теплоту воде
        if s.is_boiling:
            Q_to_water = s.P_to_vapor_kW * 1000  # W
        else:
            Q_to_water = 0

        # Поток воды
        if self.out.valve_water and not self.fault.water_cutoff:
            m_dot_water_kgs = s.water_flow_lpm / 60.0  # L/min → kg/s
        else:
            m_dot_water_kgs = 0.0

        if m_dot_water_kgs > 0.001:
            dT_water = Q_to_water / (m_dot_water_kgs * p.cp_water)
            target_T_water_out = s.T_water_in + dT_water
            # Инерция холодильника
            tau_water = 5.0
            s.T_water_out += (target_T_water_out - s.T_water_out) * dt / tau_water
        else:
            # Нет потока, вода в холодильнике быстро греется
            small_mass_water_kg = 0.3
            dT = Q_to_water * dt / (small_mass_water_kg * p.cp_water)
            s.T_water_out += dT
            # Если кипит, T_water_out быстро уйдёт за 100, физически нереально — clamp
            s.T_water_out = min(s.T_water_out, 130)

        # Слегка остужаем выход к входу при отсутствии нагрузки
        if Q_to_water < 10:
            s.T_water_out += (s.T_water_in - s.T_water_out) * dt / 20

        self.t_sim_s += dt

    def read_sensors(self) -> dict:
        """Возвращает 'показания датчиков' с учётом fault-injection.
        Это то, что 'ESP читает с DS18B20'."""
        s = self.s
        sensors = {
            "T_kub": s.T_kub if not self.fault.sensor_t_kub_fail else float("nan"),
            "T_head": s.T_head if not self.fault.sensor_t_head_fail else float("nan"),
            "T_water_in": s.T_water_in,
            "T_water_out": s.T_water_out
            if not self.fault.sensor_t_water_out_fail
            else float("nan"),
            "P_atm_hPa": s.P_atm_Pa / 100.0,
            "is_boiling": s.is_boiling,
            "V_kub": s.V_kub,
            "V_product": s.V_product,
            "x_kub_abv": mass_to_abv_vol(s.x_kub_mass),
            "x_head_abv": mass_to_abv_vol(s.x_head_mass),
            "x_product_abv": mass_to_abv_vol(s.x_product_mass),
            "duty_avg": s.duty_avg,
            "P_heater_kW": s.P_heater_kW,
            "m_dot_vapor_gps": s.m_dot_vapor_gps,
            "estop": self.fault.estop_pressed,
            "bimetal_tripped": self.fault.bimetal_tripped,
        }
        return sensors

    def apply_outputs(self, outs: Outputs):
        """ESP-аналог: получаем команды от Pi (или внутреннего контроллера)."""
        self.out = outs
