"""
sim/physics.py — высокоточная физическая модель куба + колонны + холодильника.

Модули:
- VLE: 5 компонентов (MeOH, EtOH, H2O, 1-PrOH, isoamyl) с уравнением Антуана
- Boiler: two-node (bulk + top film) с puking, foam-over, dry-out
- Column: N theoretical stages с Murphree-эффективностью, hold-up, HETP(Cv)
- Hydrodynamics: F-factor, Cv, Ergun/Leva Δp, oscillations near flooding
- Condenser: total reflux на верху, cooling water balance

Все константы — единицы СИ. Хранение T в °C для удобства.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List


# ============================================================================
# КОМПОНЕНТЫ
# ============================================================================

# i=0 methanol, i=1 ethanol, i=2 water, i=3 1-propanol, i=4 isoamyl alcohol
N_COMP = 5
COMP_NAMES = ["methanol", "ethanol", "water", "1-propanol", "isoamyl"]

# Antoine constants: log10(P_mmHg) = A - B/(C+T_C); valid ~0..150°C
ANTOINE = [
    (8.0809, 1582.27, 239.726),    # methanol BP=64.7
    (8.20417, 1642.89, 230.300),   # ethanol BP=78.37
    (8.07131, 1730.63, 233.426),   # water BP=100.0
    (7.99733, 1569.70, 209.500),   # 1-propanol BP=97.2
    (7.46393, 1314.56, 168.500),   # isoamyl alcohol BP=131.1
]

M_W = [32.04, 46.07, 18.015, 60.10, 88.15]  # g/mol
DH_VAP_J_KG = [1_100_000, 855_000, 2_260_000, 700_000, 504_000]  # latent heat
RHO_LIQ = [792, 789, 998, 803, 810]  # kg/m³ at 20°C
CP_LIQ = [2530, 2440, 4186, 2400, 2380]  # J/(kg·K)

# Boiling points pure components, °C
BP_PURE = [64.7, 78.37, 100.0, 97.2, 131.1]


def P_sat(comp_i: int, T_C: float) -> float:
    """Saturation pressure of pure component i at T_C, in Pascals."""
    A, B, C = ANTOINE[comp_i]
    if T_C < -50 or T_C > 200:
        return 0.0
    P_mmHg = 10 ** (A - B / (C + T_C))
    return P_mmHg * 133.322


def vapor_eq_mol_frac(x_mol: List[float], T_C: float) -> List[float]:
    """Equilibrium vapor mole fractions over liquid (Raoult's law approx)."""
    partials = [x_mol[i] * P_sat(i, T_C) for i in range(N_COMP)]
    P_sum = sum(partials)
    if P_sum < 1e-3:
        return [0.0] * N_COMP
    return [p / P_sum for p in partials]


# Wilson model для etOH-H2O — non-ideal correction (12.11.2).
# Параметры из Gmehling et al, VLE DECHEMA. λ в cal/mol, V в cm³/mol.
# Для этанол(1)-вода(2):
_WILSON_LAMBDA_12 = 276.76 * 4.184   # J/mol (= 1158 J/mol)
_WILSON_LAMBDA_21 = 975.49 * 4.184   # J/mol (= 4081 J/mol)
_WILSON_V = [40.7, 58.68, 18.07, 75.14, 109.18]  # molar volume cm³/mol


def activity_coef_etOH_H2O(x_etOH_mol: float, T_C: float) -> tuple[float, float]:
    """Wilson activity coefficients для бинарной etOH-H2O смеси.
    Возвращает (γ_etOH, γ_H2O). Reproduces azeotrope ~89% mol (95.6 wt%) @ 1 atm.

    Эта функция доступна как опция для более точного VLE расчёта.
    Текущая sim использует Raoult (ideal mixture), что для etOH-H2O даёт
    азеотроп ~89% mol тоже, но при немного другой T (≈0.5°C off). Полная
    интеграция requires Boiler.step refactor — будет в stage 10.
    """
    R = 8.314  # J/(mol·K)
    T_K = T_C + 273.15
    V1, V2 = _WILSON_V[1], _WILSON_V[2]  # etOH, H2O
    L12 = (V2 / V1) * math.exp(-_WILSON_LAMBDA_12 / (R * T_K))
    L21 = (V1 / V2) * math.exp(-_WILSON_LAMBDA_21 / (R * T_K))
    x1 = max(0.001, min(0.999, x_etOH_mol))
    x2 = 1 - x1
    ln_g1 = -math.log(x1 + L12 * x2) + x2 * (
        L12 / (x1 + L12 * x2) - L21 / (x2 + L21 * x1)
    )
    ln_g2 = -math.log(x2 + L21 * x1) - x1 * (
        L12 / (x1 + L12 * x2) - L21 / (x2 + L21 * x1)
    )
    return math.exp(ln_g1), math.exp(ln_g2)


def vapor_eq_mol_frac_nonideal(x_mol: List[float], T_C: float) -> List[float]:
    """Non-ideal VLE: Raoult с activity coefficients для etOH-H2O пары,
    остальные компоненты (methanol, propanol, isoamyl) — ideal mixture.

    Это опциональный path, может быть включён в Boiler.step через флаг
    use_wilson_vle=True (по умолчанию False для backward compat).
    """
    # γ_i для etOH (i=1) и H2O (i=2) через Wilson
    x_etOH = x_mol[1]
    x_H2O = x_mol[2]
    denom = x_etOH + x_H2O
    if denom > 1e-6:
        # эффективная x_etOH в бинарной этанол-вода паре (нормируем)
        x_etOH_bin = x_etOH / denom
        g_etOH, g_H2O = activity_coef_etOH_H2O(x_etOH_bin, T_C)
    else:
        g_etOH, g_H2O = 1.0, 1.0

    gammas = [1.0, g_etOH, g_H2O, 1.0, 1.0]
    partials = [gammas[i] * x_mol[i] * P_sat(i, T_C) for i in range(N_COMP)]
    P_sum = sum(partials)
    if P_sum < 1e-3:
        return [0.0] * N_COMP
    return [p / P_sum for p in partials]


def boiling_T(x_mol: List[float], P_atm_Pa: float = 101325) -> float:
    """Bubble point T (°C) of liquid mixture at given pressure (bisection)."""
    lo, hi = 30.0, 150.0
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        P = sum(x_mol[i] * P_sat(i, mid) for i in range(N_COMP))
        if P > P_atm_Pa:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def mol_to_mass_frac(x_mol: List[float]) -> List[float]:
    masses = [x_mol[i] * M_W[i] for i in range(N_COMP)]
    total = sum(masses)
    return [m / total if total > 0 else 0 for m in masses]


def mass_to_mol_frac(x_mass: List[float]) -> List[float]:
    moles = [x_mass[i] / M_W[i] for i in range(N_COMP)]
    total = sum(moles)
    return [m / total if total > 0 else 0 for m in moles]


def density_mix_liq(x_mass: List[float], T_C: float = 20) -> float:
    """Density of liquid mixture, kg/m³ (volumetric additivity)."""
    inv_sum = sum(x_mass[i] / max(RHO_LIQ[i] * (1 - 0.0008 * (T_C - 20)), 100)
                  for i in range(N_COMP))
    return 1.0 / max(inv_sum, 1e-9)


def cp_mix(x_mass: List[float]) -> float:
    """Specific heat capacity of liquid mixture, J/(kg·K)."""
    return sum(x_mass[i] * CP_LIQ[i] for i in range(N_COMP))


def latent_heat_mix(x_mass: List[float]) -> float:
    """Average latent heat of vaporization, J/kg (mass-weighted)."""
    return sum(x_mass[i] * DH_VAP_J_KG[i] for i in range(N_COMP))


def mass_to_abv_vol(x_mass_eth: float, T_C: float = 20) -> float:
    """Mass fraction of ethanol → volumetric ABV (об.%) — approximate."""
    if x_mass_eth <= 0:
        return 0
    if x_mass_eth >= 1:
        return 100
    rho_e, rho_w = 789, 998
    v_e = x_mass_eth / rho_e
    v_w = (1 - x_mass_eth) / rho_w
    return 100 * v_e / (v_e + v_w)


def abv_vol_to_mass_frac_eth(abv_vol: float) -> float:
    """Volumetric ABV → mass fraction of ethanol."""
    v = abv_vol / 100.0
    rho_e, rho_w = 0.789, 0.998
    m_e = v * rho_e
    m_w = (1 - v) * rho_w
    return m_e / (m_e + m_w)


# Per-mash-type trace impurity profiles (см. 12.13.1).
# Базируется на форумных observations: фруктовая дает ~15× больше methanol
# из-за pectin gestation, зерновая средняя, сахарная самая чистая.
MASH_PROFILES = {
    "sugar":  {"meoh": 0.0005, "proh": 0.010, "iso": 0.015},  # clean
    "grain":  {"meoh": 0.0010, "proh": 0.015, "iso": 0.020},  # baseline
    "fruit":  {"meoh": 0.0150, "proh": 0.020, "iso": 0.025},  # pectin → MeOH
    "mixed":  {"meoh": 0.0020, "proh": 0.015, "iso": 0.020},
}


def initial_mash_composition(abv_vol: float, mash_type: str = "grain") -> List[float]:
    """Realistic mash composition: ethanol + water + trace impurities scaled
    by ethanol content. Trace ratios depend on mash_type (см. MASH_PROFILES)."""
    profile = MASH_PROFILES.get(mash_type, MASH_PROFILES["grain"])
    x_eth = abv_vol_to_mass_frac_eth(abv_vol)
    x_water = 1 - x_eth
    x_meoh = profile["meoh"] * x_eth
    x_proh = profile["proh"] * x_eth
    x_iso = profile["iso"] * x_eth
    x_eth_clean = x_eth - x_meoh - x_proh - x_iso
    return [x_meoh, x_eth_clean, x_water, x_proh, x_iso]


# ============================================================================
# BOILER (two-node: bulk + top film)
# ============================================================================

@dataclass
class BoilerParams:
    V_max_L: float = 25.0
    thermal_mass_walls_J_K: float = 50_000  # стенки + ТЭН
    P_max_kW: float = 5.0
    # Конвективный теплообмен bulk↔film: высокое значение для воды,
    # низкое для густой/вязкой браги (грейн, фрукты, осадок)
    h_bulk_film_W_K: float = 500.0  # W/K, scales with 1/viscosity
    film_volume_frac: float = 0.05  # верхний слой
    heat_loss_W_K: float = 8.0  # к окружающей; с минимальной изоляцией 5-10

@dataclass
class BoilerState:
    V_total_L: float = 18.0
    x_mass: List[float] = field(default_factory=lambda: [0]*N_COMP)
    T_bulk_C: float = 22.0
    T_film_C: float = 22.0
    T_walls_C: float = 22.0  # thermal mass walls
    foam_height_cm: float = 0
    foam_active: bool = False  # пена сейчас выбрасывается в колонну
    probe_fouled_bias_C: float = 0
    probe_fouled_until_t: float = 0
    # параметры браги
    mash_type: str = "grain"  # grain/sugar/fruit/mixed (см. 12.13.1)
    viscosity_factor: float = 1.0  # 1=clean spirit, 3=grain mash, 5=fruit pulp
    sugar_content_g_L: float = 0  # residual sugar — драйвер пенообразования
    is_boiling: bool = False
    P_to_vapor_kW: float = 0
    m_dot_vapor_kg_s: float = 0
    dry_out: bool = False


class Boiler:
    """Two-node thermal model: bulk + top film.
    Film boils first (имитирует stratification), может вызвать puking events.
    Sugar + low V → пенообразование (foam-over)."""

    def __init__(self, params: BoilerParams = None):
        self.p = params or BoilerParams()
        self.s = BoilerState()

    def set_mash(self, V_L: float, abv_vol: float, viscosity: float = 1.0,
                 sugar_g_L: float = 0, mash_type: str = "grain"):
        self.s.V_total_L = V_L
        self.s.x_mass = initial_mash_composition(abv_vol, mash_type)
        self.s.viscosity_factor = max(1.0, viscosity)
        self.s.sugar_content_g_L = sugar_g_L
        self.s.mash_type = mash_type

    def step(self, dt: float, P_heater_W: float, P_atm_Pa: float,
             T_ambient_C: float = 22) -> tuple[float, List[float], float]:
        """Один шаг. Возвращает (m_dot_vapor_kg_s, x_vapor_mass, T_vapor_C).

        Модель: single-node жидкость + walls thermal mass.
        Кипение: T_bulk фиксируется на T_boil, всё избыточное тепло в пар.
        Stratification/puking — стохастический puking event при риск-факторах,
        проявляется как короткий «выброс» состава из bulk в пар (вместо равновесного).
        """
        s, p = self.s, self.p

        # Dry-out: жидкости почти нет
        if s.V_total_L < 0.3:
            s.dry_out = True
            C_walls = p.thermal_mass_walls_J_K
            dT = (P_heater_W - p.heat_loss_W_K * (s.T_bulk_C - T_ambient_C)) * dt / C_walls
            s.T_bulk_C += dT
            s.T_film_C = s.T_bulk_C
            s.is_boiling = False
            s.m_dot_vapor_kg_s = 0
            s.P_to_vapor_kW = 0
            return 0, [0]*N_COMP, s.T_bulk_C

        # Total mass и cp
        rho_L = density_mix_liq(s.x_mass, s.T_bulk_C)
        m_total_kg = s.V_total_L / 1000 * rho_L
        cp = cp_mix(s.x_mass)
        C_liquid = m_total_kg * cp

        # Точка кипения
        x_mol = mass_to_mol_frac(s.x_mass)
        T_boil = boiling_T(x_mol, P_atm_Pa)

        # Тепловые потери
        Q_loss = p.heat_loss_W_K * (s.T_bulk_C - T_ambient_C)
        Q_to_walls = 3.0 * (s.T_bulk_C - s.T_walls_C)

        m_dot_vapor_kg_s = 0
        y_mass = [0]*N_COMP

        if s.T_bulk_C < T_boil - 0.5:
            # Heating phase
            is_boiling = False
            Q_net = P_heater_W - Q_loss - Q_to_walls
            s.T_bulk_C += Q_net * dt / C_liquid if C_liquid > 0 else 0
            s.T_film_C = s.T_bulk_C
        else:
            # Boiling: T_bulk pinned at T_boil, excess heat → vapor
            is_boiling = True
            s.T_bulk_C = T_boil
            s.T_film_C = T_boil
            P_to_vapor = max(0, P_heater_W - Q_loss - Q_to_walls)
            s.P_to_vapor_kW = P_to_vapor / 1000

            # Vapor composition: equilibrium with current куб liquid
            y_mol_eq = vapor_eq_mol_frac(x_mol, T_boil)
            y_mass = mol_to_mass_frac(y_mol_eq)
            # L_vap для vapor mixture (не для liquid! Это разные составы)
            L_vap = latent_heat_mix(y_mass)
            m_dot_vapor_kg_s = P_to_vapor / L_vap if L_vap > 0 else 0

            # ВАЖНО: куб теряет mass только если она реально уходит в продукт.
            # Это решает Still.step (когда valve_takeoff_open). При total reflux —
            # всё конденсируется обратно как флегма и возвращается в колонну,
            # которая в стационаре эквивалентна возврату в куб.
            # Здесь только возвращаем m_dot_vapor и y_mass; mass balance в Still.

        # Walls thermal
        s.T_walls_C += (Q_to_walls - 2.5 * (s.T_walls_C - T_ambient_C)) * dt / p.thermal_mass_walls_J_K

        # Puking: стохастический event при риск-факторах
        # Условие: высокая viscosity (грейн/фрукт), активное кипение, high power gradient
        # Проявляется как kub liquid carryover в пар → состав пара = смесь vapor + bulk
        if is_boiling and s.viscosity_factor > 1.5 and P_heater_W > 3000:
            P_puke_per_min = 0.002 * s.viscosity_factor * (P_heater_W / 5000)
            if random.random() < P_puke_per_min * dt / 60:
                # Puke: 30% liquid carryover for next 5-10 sec
                # Имитируем смешением vapor с bulk composition
                for i in range(N_COMP):
                    y_mass[i] = 0.7 * y_mass[i] + 0.3 * s.x_mass[i]
                # Probe fouling после puke
                s.probe_fouled_bias_C = random.uniform(0.5, 2.5)
                s.probe_fouled_until_t = self.t_sim_s_marker + 300 if hasattr(self, 't_sim_s_marker') else 0

        s.is_boiling = is_boiling
        s.m_dot_vapor_kg_s = m_dot_vapor_kg_s

        # Re-fermentation CO2 outgassing (см. 12.13.5):
        # подмоложенная брага возобновляет ферментацию при медленном прогреве.
        # CO2 пузыри генерируют foam ДО любого кипения, при T_kub 30-50°C.
        # Это для тестирования re-fermentation guard в controller.
        if (getattr(s, "_under_fermented", False)
                and 30 < s.T_bulk_C < 50 and not s.foam_active):
            P_co2_per_min = 0.05 * (s.T_bulk_C - 30) / 20  # пик ~50°C
            if random.random() < P_co2_per_min / 60 * dt:
                s.foam_active = True

        # Foam-over stochastic event
        # Higher probability with: high sugar, high fill ratio, high Q, viscosity
        fill_ratio = s.V_total_L / p.V_max_L
        if is_boiling and not s.foam_active:
            P_foam_per_min = 0.001 * s.sugar_content_g_L * fill_ratio * (P_heater_W/3000) * (s.viscosity_factor)
            P_per_step = P_foam_per_min / 60 * dt
            if random.random() < P_per_step:
                s.foam_active = True

        # If foam active, decays after 60-180 sec
        if s.foam_active and random.random() < dt / 90:
            s.foam_active = False

        # If foam active, the "vapor" composition is contaminated — it's
        # actually liquid foam climbing the column, so y_mass shifts toward x_mass
        if s.foam_active:
            for i in range(N_COMP):
                y_mass[i] = 0.6 * s.x_mass[i] + 0.4 * y_mass[i]

        # Probe fouling: after puking events (Q step changes during low V), bias on top sensor
        # Simplified: random probe foul after foam events
        if s.foam_active and s.probe_fouled_bias_C < 1.0:
            s.probe_fouled_bias_C = random.uniform(0.5, 2.0)
            s.probe_fouled_until_t = 600  # decays over 10 min real time

        s._last_m_dot_vapor = m_dot_vapor_kg_s  # для observables atm_tube_voc
        return m_dot_vapor_kg_s, y_mass, T_boil if is_boiling else s.T_bulk_C


# ============================================================================
# COLUMN (упрощённая lag-модель с volatility factors)
# ============================================================================
#
# Полный MESH N-плитный решатель — overkill для проверки логики стейт-машины.
# Используем lag-модель:
#   y_top_target = equilibrium состав наверху при текущем (x_kub, R, Cv)
#   dy_top/dt = (y_top_target - y_top) / τ_column(Cv, m_dot)
# Volatility ordering между компонентами сохраняется (головы выходят первыми).
# Hold-up в колонне моделируется как буфер: общий объём в плитах = total_holdup_kg.
# ============================================================================

# Относительная летучесть alpha_i = (P_i_sat / P_water_sat) при T_ref=80°C
# Это «эффективность подъёма» каждого компонента в колонне.
# Higher alpha = lighter (rises easier with refluxing).
ALPHA_REL = {
    0: 4.0,   # MeOH — головы
    1: 8.0,   # EtOH — целевая фракция
    2: 1.0,   # water — reference
    3: 3.5,   # 1-propanol — промежуточная фракция (хвосты)
    4: 1.5,   # isoamyl — хвосты
}


@dataclass
class ColumnParams:
    D_m: float = 0.04  # 1.5" inner ≈ 38mm; 2" ≈ 50mm
    H_m: float = 1.0
    packing: str = "SPN"  # SPN, mesh, raschig — игнорируется при column_type='bubble_cap'
    epsilon: float = 0.92
    a_p: float = 1500
    total_holdup_kg: float = 0.25
    N_theoretical_max: int = 20  # максимум при идеальном Cv
    # Тип колонны (stage 10):
    # 'packed' — насадочная (SPN/mesh/raschig), packed bed
    # 'bubble_cap' — колпачковая (e.g. ХД-4 500), N тарелок
    column_type: str = "packed"
    n_plates: int = 4  # для bubble_cap: фиксированное число тарелок
    holdup_per_plate_L: float = 0.05  # bubble_cap: жидкость на каждой тарелке


@dataclass
class ColumnState:
    # composition наверху колонны (то что попадает в обратный холодильник)
    y_top_mass: List[float] = field(default_factory=lambda: [0]*N_COMP)
    # для визуализации: T profile по высоте колонны (linearly interpolated)
    T_profile: List[float] = field(default_factory=lambda: [22.0]*10)
    x_profile: List[List[float]] = field(default_factory=list)  # per-position composition
    # гидродинамика
    Cv: float = 0
    HETP_cm: float = 5.0
    N_eff: int = 15
    flooded: bool = False
    weeping: bool = False
    pre_flood_oscillation: float = 0
    delta_P_Pa: float = 0
    vapor_velocity_m_s: float = 0
    # current effective top composition target (debug)
    y_top_target_mass: List[float] = field(default_factory=lambda: [0]*N_COMP)


class Column:
    """Упрощённая модель колонны с lag-фильтрацией.

    Идея: колонна разделяет компоненты по их летучести. При total reflux (R=∞)
    наверху скапливается самый летучий компонент (MeOH сначала, потом EtOH).
    При finite R, отбор «промывает» колонну, спускающаяся флегма «обеднена»
    относительно vapor вверху.

    Cv, Δp, HETP, weeping/flooding моделируются явно для diagnostics."""

    def __init__(self, params: ColumnParams = None):
        self.p = params or ColumnParams()
        self.s = ColumnState()
        # Initial profile: 10 points along column, all water composition
        self.s.x_profile = [[0,0,1,0,0] for _ in range(10)]
        self.s.T_profile = [22.0]*10

    def cross_area_m2(self) -> float:
        return math.pi * (self.p.D_m / 2) ** 2

    def v_flood_m_s(self) -> float:
        """Flooding vapor velocity. Calibrated against Sherwood-Eckert and forum data:
        1.5" SPN @ 5kW → Cv ~ 0.75 (v=3.6 m/s, v_flood=4.8).
        Bubble cap (медные колпачковые типа ХД-4): v_flood ~1.0-1.5 m/s,
        существенно ниже packed из-за liquid hold-up на тарелках."""
        if self.p.column_type == "bubble_cap":
            return 1.2  # медная колпачковая
        return {"SPN": 4.8, "mesh": 3.5, "raschig": 4.0}.get(self.p.packing, 4.0)

    def HETP_from_Cv(self, Cv: float) -> float:
        """HETP curve: optimum at Cv=0.5-0.7. Для bubble cap каждая тарелка
        — отдельный stage, HETP ≈ plate spacing (typically 100-125 mm), но
        КПД тарелки 0.5-0.7 → эффективный HETP больше."""
        if self.p.column_type == "bubble_cap":
            # plate spacing ХД-4 500: 500mm / 4 plates = ~125mm per plate
            # КПД тарелки ~0.6 → эффективный HETP = spacing / efficiency
            base_HETP_cm = 100 * (self.p.H_m / max(1, self.p.n_plates)) / 0.6
            if Cv < 0.2:
                return base_HETP_cm * 2  # weeping, sieve effect
            elif Cv < 0.85:
                return base_HETP_cm
            else:
                return base_HETP_cm * 3  # near flooding
        if Cv < 0.2:
            return 20
        elif Cv < 0.3:
            return 12
        elif Cv < 0.7:
            return 5
        elif Cv < 0.85:
            return 7
        elif Cv < 1.0:
            return 12
        else:
            return 20

    def step(self, dt: float, m_dot_vapor_kg_s: float, x_kub_mass: List[float],
             R_reflux: float, P_atm_Pa: float, T_kub: float):
        """Один шаг модели колонны.

        m_dot_vapor_kg_s: расход пара снизу
        x_kub_mass: состав жидкости в кубе (5-vector)
        R_reflux: эффективный reflux ratio (L/D). При закрытом клапане → R=∞
        P_atm_Pa: атмосферное давление
        T_kub: T жидкости в кубе (для нижней точки T profile)
        """
        s, p = self.s, self.p

        # 1. Гидродинамика
        if m_dot_vapor_kg_s > 1e-6:
            rho_v = 1.2
            v_vapor = (m_dot_vapor_kg_s / rho_v) / self.cross_area_m2()
            s.Cv = v_vapor / self.v_flood_m_s()
        else:
            v_vapor = 0
            s.Cv = 0
        s.vapor_velocity_m_s = v_vapor
        s.flooded = s.Cv > 1.0
        s.weeping = 0 < s.Cv < 0.3
        s.HETP_cm = self.HETP_from_Cv(s.Cv)
        if p.column_type == "bubble_cap":
            # N_eff = n_plates × КПД (efficiency drops при weeping/flooding)
            eff = 0.6 if 0.3 < s.Cv < 0.85 else (0.3 if s.Cv < 0.3 else 0.4)
            s.N_eff = max(1, int(p.n_plates * eff))
            # ΔP per plate ~600-1500 Pa (зависит от Cv), всего n_plates тарелок
            s.delta_P_Pa = (600 + 900 * s.Cv) * p.n_plates
        else:
            s.N_eff = max(3, int(p.H_m * 100 / s.HETP_cm))
            s.delta_P_Pa = (200 + 800 * s.Cv) * (1 + 2 * s.Cv ** 2) * p.H_m
        s.pre_flood_oscillation = max(0, (s.Cv - 0.7) * 5)

        # 2. Если нет пара — top остывает медленно к ambient
        if m_dot_vapor_kg_s < 1e-7:
            for k in range(N_COMP):
                s.y_top_mass[k] += (0 - s.y_top_mass[k]) * dt / 600
            return s.y_top_mass[:]

        # 3. Если захлёб — состав наверху испорчен (вся флегма ловит пар)
        if s.flooded:
            target = x_kub_mass[:]  # фактически прорыв куба наверх
        else:
            # 4. Target composition based on R and component volatility
            # При R = ∞: наверху ~азеотроп (95.6% EtOH мольная)
            # При R = 0: наверху = состав пара из куба (равновесный с x_kub)
            # Эмпирически: f_sep = R / (R + k_R) где k_R зависит от N_eff
            # Чем больше N_eff (лучше HETP), тем меньше k_R (легче достигается azeotrope)
            k_R = max(0.3, 3.0 / max(1, s.N_eff / 5))

            # При finite R: target = mix of (vapor from kub) and (top equilibrium)
            x_kub_mol = mass_to_mol_frac(x_kub_mass)
            T_kub_eq = boiling_T(x_kub_mol, P_atm_Pa)
            y_kub_mol = vapor_eq_mol_frac(x_kub_mol, T_kub_eq)
            y_kub_mass = mol_to_mass_frac(y_kub_mol)

            # Top "ideal" composition: применяем relative volatility N_eff раз
            # Iterative enrichment toward top
            y_iter = y_kub_mass[:]
            for _ in range(s.N_eff):
                y_mol = mass_to_mol_frac(y_iter)
                # Apply equilibrium and re-condense (each step is one theoretical stage)
                # y_new[i] ∝ alpha[i] * x[i] / Σ
                weighted = [ALPHA_REL[i] * y_mol[i] for i in range(N_COMP)]
                tot = sum(weighted)
                if tot < 1e-9:
                    break
                y_iter_mol = [w / tot for w in weighted]
                y_iter = mol_to_mass_frac(y_iter_mol)

            y_top_ideal = y_iter
            # Foam carryover: when foam active, top contaminated by kub liquid
            # (передаётся из boiler через y_vapor — состав уже искажён)
            # Interpolate by R:
            f_sep = R_reflux / (R_reflux + k_R)
            target = [y_top_ideal[i] * f_sep + y_kub_mass[i] * (1 - f_sep)
                      for i in range(N_COMP)]
            # Normalize
            tot = sum(target)
            if tot > 0:
                target = [t / tot for t in target]

        s.y_top_target_mass = target[:]

        # 5. Lag toward target with time constant τ(Cv, m_dot)
        # τ = hold-up / m_dot, с учётом что больше Cv = больше hold-up
        hold_up_factor = 1 + s.Cv * 2 if s.Cv < 1 else 3
        tau_column = (p.total_holdup_kg * hold_up_factor) / max(1e-6, m_dot_vapor_kg_s)
        tau_column = max(30, min(600, tau_column))  # ограничим разумно
        alpha = dt / (tau_column + dt)
        for k in range(N_COMP):
            s.y_top_mass[k] += (target[k] - s.y_top_mass[k]) * alpha
        # Normalize
        tot = sum(s.y_top_mass)
        if tot > 0:
            s.y_top_mass = [y / tot for y in s.y_top_mass]

        # 6. Compute T profile (cosmetic — linear interpolation kub→top by composition)
        T_top = boiling_T(mass_to_mol_frac(s.y_top_mass), P_atm_Pa)
        for i in range(10):
            frac = i / 9.0
            # Composition along column: interpolate
            x_at = [x_kub_mass[k] * (1-frac) + s.y_top_mass[k] * frac
                    for k in range(N_COMP)]
            sm = sum(x_at)
            if sm > 0:
                x_at = [x / sm for x in x_at]
            s.x_profile[i] = x_at
            s.T_profile[i] = boiling_T(mass_to_mol_frac(x_at), P_atm_Pa)

        # Add pre-flood oscillations to T_bottom (visible on dashboard)
        if s.pre_flood_oscillation > 0:
            s.T_profile[0] += s.pre_flood_oscillation * 0.1 * random.uniform(-1, 1)

        return s.y_top_mass[:]

    def _top_composition(self) -> List[float]:
        return self.s.y_top_mass[:]

    def T_top_C(self) -> float:
        return self.s.T_profile[-1] if self.s.T_profile else 22.0

    def T_bottom_C(self) -> float:
        return self.s.T_profile[0] if self.s.T_profile else 22.0


# ============================================================================
# CONDENSER + UZEL OTBORA (LM)
# ============================================================================

@dataclass
class CondenserState:
    """Stage 15 update: реальная топология имеет ДВА независимых water
    circuits.

    Реальный setup пользователя (ХД/4-375 + дистиллятор ХД/4-2500ПК):
    - Main reflux condenser (дефлегматор unit ХД/4-2500ПК): регулируемый
      water flow. Изменение flow rate = изменение reflux ratio. Это то
      что пользователь крутит вручную для достижения 93% ABV.
    - Product condenser (малый, на выходе клапана узла отбора):
      собственный постоянный water flow, охлаждает product до 20-30°C.
    """
    T_water_in_C: float = 12.0
    # Main reflux condenser (variable flow, основной reflux mechanism)
    T_water_out_main_C: float = 12.0
    water_flow_main_lpm: float = 3.0  # regulated by operator/auto
    water_valve_main_open: bool = False
    Q_to_water_main_W: float = 0
    main_capacity_kW: float = 2.0  # ХД/4-2500ПК
    # Product condenser (constant separate flow)
    T_water_out_product_C: float = 12.0
    water_flow_product_lpm: float = 0.8  # constant separate flow
    Q_to_water_product_W: float = 0
    product_capacity_kW: float = 0.3
    # Aliases для обратной совместимости с существующими scenarios
    T_water_out_C: float = 12.0  # = T_water_out_main_C
    water_flow_lpm: float = 3.0  # = water_flow_main_lpm
    water_valve_open: bool = False  # = water_valve_main_open
    Q_to_water_W: float = 0  # = Q_to_water_main_W
    T_water_after_product_C: float = 12.0  # для UI backward-compat
    # Stage 10: main bypass (вода больше не идёт в main reflux condenser,
    # vapor пробивается без конденсации)
    main_bypass_closed: bool = False
    # Stage 16: cooling topology
    # 'parallel' — main + small condenser независимые circuits (auto-mode БКУ)
    # 'series'   — main → small последовательно по воде И по спирту
    #              (manual mode user'а с непрерывной струйкой без клапана)
    cooling_topology: str = "parallel"


class Condenser:
    """Reflux condenser model. Stage 15 update — ДВА независимых cooling
    circuits (НЕ в series, как было ошибочно ранее):

    1. Main reflux condenser (ХД/4-2500ПК deflegmator):
       - Regulated water flow (variable, set by operator или automation).
       - Это главный regulating control для reflux ratio. Больше воды →
         больше reflux → выше ABV. Меньше воды → vapor breakthrough.
       - main_bypass_closed=True полностью отключает.

    2. Product condenser (малый, на выходе клапана узла отбора):
       - Constant separate water flow (~0.5-1 L/min).
       - Cooling capacity ~0.3 kW (достаточно для takeoff 1-2 L/h).
       - НЕ регулируется в normal operation.
    """

    CP_WATER = 4186.0

    def __init__(self):
        self.s = CondenserState()

    def step(self, dt: float, m_dot_vapor_kg_s: float, x_vapor_mass: List[float],
             water_cutoff: bool = False,
             m_dot_product_kg_s: float = 0.0):
        """Stage 15: 2 НЕЗАВИСИМЫХ cooling water circuits.
        m_dot_vapor_kg_s — пар из колонны → main reflux condenser
        m_dot_product_kg_s — отбор → product condenser (cool to 25°C)
        """
        s = self.s
        # Sync aliases: код может писать в s.water_flow_lpm — пробросим в main
        s.water_flow_main_lpm = s.water_flow_lpm
        s.water_valve_main_open = s.water_valve_open

        # === Main reflux condenser ===
        if m_dot_vapor_kg_s > 0 and not s.main_bypass_closed:
            L_vap = latent_heat_mix(x_vapor_mass)
            s.Q_to_water_main_W = m_dot_vapor_kg_s * L_vap
            s.Q_to_water_main_W = min(s.Q_to_water_main_W, s.main_capacity_kW * 1000)
        else:
            s.Q_to_water_main_W = 0

        if s.water_valve_main_open and not water_cutoff and not s.main_bypass_closed:
            m_dot_w_main = s.water_flow_main_lpm / 60.0
            if m_dot_w_main > 1e-4:
                dT_main = s.Q_to_water_main_W / (m_dot_w_main * self.CP_WATER)
                target = s.T_water_in_C + dT_main
                s.T_water_out_main_C += (target - s.T_water_out_main_C) * dt / 3.0
            else:
                m_in_pipe = 0.3
                s.T_water_out_main_C += s.Q_to_water_main_W * dt / (m_in_pipe * self.CP_WATER)
        else:
            # Bypass or cutoff — нет teploobmena
            m_in_pipe = 0.3
            s.T_water_out_main_C += s.Q_to_water_main_W * dt / (m_in_pipe * self.CP_WATER)

        # === Product condenser (independent constant flow) ===
        if m_dot_product_kg_s > 0:
            cp_prod = cp_mix(x_vapor_mass)
            dT_subcool = 78 - 25  # cool product from ~78°C to ~25°C
            s.Q_to_water_product_W = m_dot_product_kg_s * cp_prod * dT_subcool
            s.Q_to_water_product_W = min(
                s.Q_to_water_product_W, s.product_capacity_kW * 1000
            )
        else:
            s.Q_to_water_product_W = 0

        if not water_cutoff:
            m_dot_w_prod = s.water_flow_product_lpm / 60.0  # constant
            if m_dot_w_prod > 1e-4:
                dT_prod = s.Q_to_water_product_W / (m_dot_w_prod * self.CP_WATER)
                target_prod = s.T_water_in_C + dT_prod
                s.T_water_out_product_C += (target_prod - s.T_water_out_product_C) * dt / 3.0

        # Decay back к inlet T когда нет нагрузки
        if s.Q_to_water_main_W < 10:
            s.T_water_out_main_C += (s.T_water_in_C - s.T_water_out_main_C) * dt / 30
        if s.Q_to_water_product_W < 10:
            s.T_water_out_product_C += (s.T_water_in_C - s.T_water_out_product_C) * dt / 30

        # Cap
        s.T_water_out_main_C = min(s.T_water_out_main_C, 130)
        s.T_water_out_product_C = min(s.T_water_out_product_C, 130)

        # Aliases (для legacy read paths)
        s.T_water_out_C = s.T_water_out_main_C
        s.Q_to_water_W = s.Q_to_water_main_W
        s.T_water_after_product_C = s.T_water_out_product_C


# ============================================================================
# COMPOSITE: full still
# ============================================================================

@dataclass
class Outputs:
    """Команды от контроллера к 'железу'. То, что ESP пишет в GPIO."""
    heater_power: float = 0.0  # 0..1
    valve_takeoff: bool = False
    valve_water: bool = False
    contactor_enable: bool = False
    # Stage 16: proportional water flow control через servo+needle valve.
    # None = не управляем (используется водный клапан on/off через valve_water);
    # float = setpoint L/min для water_servo_valve
    water_flow_main_lpm: float | None = None


@dataclass
class StillFaults:
    """Глобальные fault flags для физики (датчики и клапаны живут в hardware.py)."""
    water_cutoff: bool = False
    foam_force_trigger: bool = False
    pressure_drift: bool = False  # ±10 hPa в час
    cooling_water_hot: float | None = None  # подменяет T_water_in
    column_diameter_override: float | None = None
    # Safety chain inputs (read by controller as sensors)
    estop_pressed: bool = False
    bimetal_tripped: bool = False
    pi_disconnected: bool = False
    # Подмоложенная брага: CO2 outgassing 30-50°C ДО кипения (см. 12.13.5)
    under_fermented: bool = False


def _compute_T_atm_tube(T_water_out_C: float, T_head_C: float,
                         main_bypass_closed: bool, column_flooded: bool,
                         Cv: float) -> float:
    """Атмосферная трубка дефлегматора — exit для несконденсированных
    газов. Алгоритм:
    - Норма: T_water_out + 10°C (немного выше водой охлаждённой стенки)
    - Если main reflux condenser bypassed → быстро ~T_head (vapor проходит
      без конденсации)
    - При flooding → промежуточная температура (часть пара пробивается)
    - При Cv > 0.85 (near-flood) — лёгкое повышение
    """
    base = T_water_out_C + 10
    if main_bypass_closed:
        return T_head_C - 2
    if column_flooded:
        return max(base, T_head_C - 5)
    if Cv > 0.85:
        return base + (Cv - 0.85) * 100  # linearly up до +15°C @ Cv=1.0
    return base


def _compute_atm_tube_voc(y_top_mass: List[float], m_dot_vapor_kg_s: float,
                          Cv: float, T_water_out_C: float,
                          main_bypass_closed: bool, column_flooded: bool,
                          is_boiling: bool) -> float:
    """Концентрация VOC (ethanol equivalent ppm) на атмосферной трубке
    дефлегматора. Модель:
    - В HEAT_UP (no boiling): ~0 ppm — нет пара
    - В STABILIZE (нет отбора, low Cv): немного — fugitive escape ~30 ppm
    - В HEADS (yhead высокий по MeOH/acetaldehyde): ~200-500 ppm
    - В BODY (steady-state etOH): ~50-100 ppm
    - В TAILS (тяжёлые congeners): растёт 100-300 ppm
    - При flooding/bypass: vapor breaks through, 1000-3000 ppm
    - Стохастика ±20% — реальный sensor видит шум

    y_top_mass: composition пара наверху колонны (5 components)
    """
    if not is_boiling or m_dot_vapor_kg_s < 1e-7:
        return random.uniform(5, 15)  # ambient baseline VOC

    # Base: fraction уносится без конденсации = функция от efficiency дефлегматора
    # При Cv ~ 0.5-0.7 efficiency ~99%, escapes ~1% → low ppm
    # При Cv > 0.85 efficiency падает
    base_escape = 0.01  # 1% уноса в норме
    if Cv > 0.85:
        base_escape += (Cv - 0.85) * 0.5  # до 8% @ Cv=1.0
    if column_flooded:
        base_escape = 0.2  # 20% при flooding
    if main_bypass_closed:
        base_escape = 0.5  # 50%+ при отключённом main condenser

    # m_dot_vapor in kg/s. Ethanol mass fraction in vapor at top:
    eth_frac = y_top_mass[1] if len(y_top_mass) > 1 else 0.0
    meoh_frac = y_top_mass[0] if len(y_top_mass) > 0 else 0.0
    propanol_frac = y_top_mass[3] if len(y_top_mass) > 3 else 0.0
    isoamyl_frac = y_top_mass[4] if len(y_top_mass) > 4 else 0.0

    # MeOH более летуч → быстрее уходит через дефлегматор (×1.5)
    # Propanol/isoamyl heavier → ×0.7 (хуже escapes но более persistent)
    voc_mass_escape = (eth_frac + 1.5 * meoh_frac + 0.7 * (propanol_frac + isoamyl_frac))
    # Конвертим в условные ppm на атм. трубке. Calibration: при body steady
    # state (eth_frac ~0.4 в pare, Cv ~0.5) → ~70 ppm
    ppm = base_escape * voc_mass_escape * 7000
    # Холодная вода = больше конденсации, меньше escape
    ppm *= max(0.5, 1.5 - T_water_out_C / 50)
    # Стохастика
    ppm *= random.uniform(0.8, 1.2)
    return max(0, ppm)


@dataclass
class StillObservables:
    """Что выдаёт физика наружу — что «датчики могли бы прочитать».

    Sensor topology реального аппарата ХД/4-375 + БКУ-07М (russsam.ru,
    samogon-i-vodka.ru):
    - T_kub_bulk_C   — DS18B20 в кубе (in mash) — НЕ часть БКУ-07М базового
                      comлекта, но обычно ставится отдельно
    - T_head_C       — DS18B20 в термокармане узла отбора (60mm ID × 120mm)
                      — основной канал БКУ-07М, по нему регулируется отбор
    - T_atm_tube_C   — DS1821 на атмосферной трубке дефлегматора, порог 93°C,
                      аварийная отсечка (выкл. ТЭН при достижении)
    - T_water_in/out — для контроля cooling (опц. в БКУ-07М)

    Уровень голов = level_sensor_heads (контактный щуп БКУ).
    Защита перелива = отдельный щуп в главном приёмнике (БКУ-07М встроена).
    """
    T_kub_bulk_C: float
    T_kub_wall_C: float  # стенка куба — другая T, для dry-out detection
    T_head_C: float
    T_atm_tube_C: float  # DS1821 на атмосферной трубке дефлегматора (93°C trip)
    atm_tube_voc_ppm: float  # ethanol-eq VOC concentration на атм. трубке
    T_water_in_C: float
    T_water_out_C: float
    P_atm_hPa: float
    V_kub_L: float
    V_product_L: float
    V_heads_L: float
    V_body_L: float
    V_tails_L: float
    active_receiver: str
    heads_full_flag: bool  # сигнал датчика уровня
    x_kub_abv: float
    x_head_abv: float
    x_product_abv: float
    x_heads_abv: float
    x_body_abv: float
    x_tails_abv: float
    x_head_mass: List[float]  # full composition for advanced
    x_product_mass: List[float]
    # Hydrodynamics
    Cv: float
    delta_P_Pa: float
    vapor_velocity_m_s: float
    pre_flood_oscillation: float
    flooded: bool
    weeping: bool
    foam_active: bool
    is_boiling: bool
    dry_out: bool
    # Power
    P_heater_kW: float
    P_to_vapor_kW: float
    m_dot_vapor_g_s: float


class Still:
    """Совмещённая модель куб + колонна + холодильник + узел отбора + приёмники."""

    # Объём приёмника голов задаётся ГЛУБИНОЙ ПОГРУЖЕНИЯ ЩУПА в устройстве
    # автоперевода (samogon-i-vodka.ru). Гениально-простая гидравлическая
    # схема без подвижных частей:
    # - Бутылка-приёмник голов герметично закручена крышкой устройства
    # - Длинный тонкий щуп идёт от крышки вниз внутрь бутылки до заданной
    #   глубины — единственная связь бутылки с атмосферой
    # - Капли спирта льются → бутылка «дышит» через щуп, головы собираются
    # - Когда уровень жидкости достигает кончика щупа → щуп под водой →
    #   атмосферный канал перекрыт жидкостью
    # - Новые капли теперь создают избыточное давление в герметичной бутылке
    # - Давление выталкивает спирт через ВТОРОЙ боковой выход на крышке
    # - На этом боковом выходе стоит контактный датчик: он замыкается на
    #   первой же капле, сигнализирует контроллеру «головы закончились»
    # - Контроллер переключает алгоритм клапана отбора (heads duty → body duty);
    #   физически весь дальнейший поток теперь идёт через боковой выход в
    #   отдельную бутылку тела
    #
    # Объём задаётся ТОЛЬКО позицией щупа: глубже = больше объём голов до
    # переключения. 150 мл — типовое значение для grain, для фруктовой ставят
    # 250-300 мл (больше methanol → больше нужно отсечь).
    DEFAULT_HEADS_CUP_VOLUME_L: float = 0.150

    def __init__(self, column_diameter_m: float = 0.04,
                 heater_kW: float = 5.0,
                 column_type: str = "packed",
                 n_plates: int = 4,
                 column_H_m: float = 1.0):
        """Конфигурируемый Still. По умолчанию: 1.5" packed ректификация
        с 5 kW heater. Для ХД/4-375 ККС-М (russsam.ru, медная колпачковая,
        58mm ID, 375mm раб. секция, 5 тарелок, 1" резьба, max 91-92% ABV
        при R>5) типового сетапа с 3 kW ТЭНом:
            Still(column_diameter_m=0.058, heater_kW=3.0,
                  column_type='bubble_cap', n_plates=5, column_H_m=0.375)
        """
        from hardware import LevelSensor
        self.boiler = Boiler()
        self.boiler.p.P_max_kW = heater_kW
        col_params = ColumnParams(
            D_m=column_diameter_m,
            H_m=column_H_m,
            column_type=column_type,
            n_plates=n_plates,
        )
        self.column = Column(col_params)
        self.condenser = Condenser()
        self.faults = StillFaults()
        self.level_sensor_heads = LevelSensor()
        self.t_sim_s = 0.0
        # Product receivers — разделены сифонной механикой
        self.V_heads_L = 0.0
        self.V_body_L = 0.0
        self.V_tails_L = 0.0
        self.x_heads_mass: List[float] = [0]*N_COMP
        self.x_body_mass: List[float] = [0]*N_COMP
        self.x_tails_mass: List[float] = [0]*N_COMP
        # Какой приёмник сейчас активен (механически)
        self.active_receiver: str = "heads"  # heads → body → tails
        self.heads_cup_volume_L: float = self.DEFAULT_HEADS_CUP_VOLUME_L
        self.heads_full_flag: bool = False  # выставляется когда сифон переключился
        # Outputs from controller (heater %, valve states)
        self.heater_power = 0.0  # 0..1
        self.valve_takeoff_open = False
        self.valve_water_open = False
        self.contactor_enable = False
        # Pressure (with optional drift)
        self.P_atm_Pa_base = 101325.0
        # Hardware models (off by default — backward-compat с existing tests)
        self.realistic_hw_enabled = False
        self.ds18b20_T_kub = None
        self.ds18b20_T_kub_wall = None
        self.ds18b20_T_head = None
        self.ds18b20_T_water_in = None
        self.ds18b20_T_water_out = None
        self.ssr_heater = None
        self.valve_takeoff_hw = None
        self.valve_water_hw = None
        self.contactor_hw = None
        # Stage 15: BME680 + pump на атмосферной трубке (за DS1821 93°C trip
        # для thermal protection). Используется для VOC/baseline + atm
        # pressure + ambient T (закрывает 2 из 3 «дыр» сенсоров: давление и
        # phase confirmation).
        self.bme680_atm = None
        # Stage 16: servo + needle valve для proportional water flow control
        # (заменяет ручной кран Гофмана). Auto-instantiated при enable_hw().
        self.water_servo_valve = None
        self._last_ssr_switching = False  # передаётся в DS18B20.read как EMI flag
        self.V_mains = 230.0  # для contactor + valve coil V_supply

    def enable_realistic_hardware(self,
                                   ds_family_T_kub=None,
                                   ds_family_T_head=None,
                                   ds_family_T_water_in=None,
                                   ds_family_T_water_out=None,
                                   ds_family_T_kub_wall=None,
                                   ssr_genuine: bool = True,
                                   valve_takeoff_snubber: bool = True,
                                   valve_water_snubber: bool = True,
                                   valve_takeoff_class=None,
                                   V_mains: float = 230.0,
                                   atm_gas_sensors: bool = False,
                                   water_servo_valve: bool = False):
        """Включает hardware-level моделирование сенсоров и SSR.
        Каждый ds_family_* — CounterfeitFamily enum (None = ORIGINAL).
        atm_gas_sensors=True добавляет MQ-3 + BME680 на атмосферной трубке
        дефлегматора (stage 15).
        Без вызова этого метода поведение Still неизменно — tests/scenarios
        работающие с idealnym readout не ломаются."""
        from hardware import (CoilClass, CounterfeitFamily, Contactor,
                              DS18B20, SSR, Valve, BME680)
        ORIG = CounterfeitFamily.ORIGINAL
        self.ds18b20_T_kub = DS18B20("28-aa-01", ds_family_T_kub or ORIG)
        self.ds18b20_T_kub_wall = DS18B20("28-aa-02", ds_family_T_kub_wall or ORIG)
        self.ds18b20_T_head = DS18B20("28-aa-03", ds_family_T_head or ORIG)
        self.ds18b20_T_water_in = DS18B20("28-aa-04", ds_family_T_water_in or ORIG)
        self.ds18b20_T_water_out = DS18B20("28-aa-05", ds_family_T_water_out or ORIG)
        self.ssr_heater = SSR(is_genuine=ssr_genuine, snubber=True)
        self.valve_takeoff_hw = Valve(
            coil_class=valve_takeoff_class or CoilClass.B,
            snubber=valve_takeoff_snubber,
        )
        self.valve_water_hw = Valve(
            coil_class=CoilClass.B,
            snubber=valve_water_snubber,
        )
        self.contactor_hw = Contactor()
        self.V_mains = V_mains
        # Stage 15: BME680 + pump на атмосферной трубке (за DS1821 для thermal
        # protection). Активный sampling 75 mL/min, response time ~10 сек.
        if atm_gas_sensors:
            self.bme680_atm = BME680(use_bsec=False, pump_lpm=0.075)
            # Baseline калибровка делается одним пробным прогоном в body
            # steady-state. Здесь предзадаём типичное clean-air ~50 kΩ.
            self.bme680_atm.s.R_gas_baseline_ohm = 50000
        else:
            self.bme680_atm = None
        # Stage 16: servo+needle valve для прецизионной регулировки воды
        if water_servo_valve:
            from hardware import ServoNeedleValve
            self.water_servo_valve = ServoNeedleValve(
                flow_max_lpm=5.0, failsafe_open=True,
            )
        else:
            self.water_servo_valve = None
        self.realistic_hw_enabled = True
        return self

    def set_initial(self, V_L: float = 18, abv_vol: float = 12,
                    viscosity: float = 1.0, sugar_g_L: float = 0,
                    mash_type: str = "grain",
                    oborotniy_V_L: float = 0.0,
                    oborotniy_abv: float = 0.0):
        """Initialize boiler. Опционально co-charge oborotniy спирта (см. 12.13.3):
        oborotniy_V_L литров с oborotniy_abv % ABV дополнительно к основной браге.
        Это эмулирует «парковку голов» из прошлых прогонов — повышает initial
        congener load → длиннее head phase."""
        self.boiler.set_mash(V_L, abv_vol, viscosity, sugar_g_L, mash_type)

        # Co-charge oborotniy: mass-weighted average composition
        if oborotniy_V_L > 0 and oborotniy_abv > 0:
            from physics import initial_mash_composition
            # Oborotniy = previously distilled spirit с парковкой голов:
            # сильно концентрированный по methanol/propanol/isoamyl
            # vs fresh mash. Используем «fruit» profile как proxy (worst-case).
            x_oborotniy = initial_mash_composition(oborotniy_abv, "fruit")

            rho_main = 950.0  # approx mash density
            rho_oborotniy = 850.0  # approx high-ABV spirit
            m_main = V_L / 1000 * rho_main
            m_obor = oborotniy_V_L / 1000 * rho_oborotniy
            m_total = m_main + m_obor
            new_x = []
            for i in range(N_COMP):
                new_x.append(
                    (self.boiler.s.x_mass[i] * m_main + x_oborotniy[i] * m_obor)
                    / m_total
                )
            self.boiler.s.x_mass = new_x
            self.boiler.s.V_total_L = V_L + oborotniy_V_L
        self.boiler.s.T_bulk_C = 22.0
        self.boiler.s.T_film_C = 22.0
        self.boiler.s.T_walls_C = 22.0
        self.column = Column(self.column.p)
        self.V_heads_L = 0
        self.V_body_L = 0
        self.V_tails_L = 0
        self.x_heads_mass = [0]*N_COMP
        self.x_body_mass = [0]*N_COMP
        self.x_tails_mass = [0]*N_COMP
        self.active_receiver = "heads"
        self.heads_full_flag = False
        # Per-mash-type heads cup volume (см. 12.13.1):
        # фруктовая — больше methanol → нужно больше выгнать в головы
        cup_volumes = {
            "sugar": 0.100,
            "grain": 0.150,  # baseline
            "fruit": 0.300,  # 2× для pectin → MeOH safety
            "mixed": 0.180,
        }
        self.heads_cup_volume_L = cup_volumes.get(mash_type, 0.150)

    def step(self, dt: float):
        # Propagate under-fermented flag to boiler (для re-fermentation CO2)
        self.boiler.s._under_fermented = self.faults.under_fermented

        # Pressure drift
        P_atm = self.P_atm_Pa_base
        if self.faults.pressure_drift:
            P_atm += 1000 * math.sin(self.t_sim_s / 7200)

        # Heater power: gated by contactor; SSR stuck-on handled in hardware.py
        # Realistic hw path: route через SSR + contactor models с реальным V_mains.
        if self.realistic_hw_enabled:
            self.contactor_hw.step(dt, cmd_enable=self.contactor_enable,
                                   V_coil=self.V_mains)
            contactor_holds = self.contactor_hw.s.enabled
            cmd_on = self.heater_power > 0.5  # на этом уровне sim PWM-pattern
            P_full_W = self.boiler.p.P_max_kW * 1000
            I_full = P_full_W / max(self.V_mains, 1)
            I_load = I_full * self.heater_power  # average current
            flows, switching = self.ssr_heater.step(
                dt, cmd_on=cmd_on and contactor_holds,
                I_load_A=I_load, T_ambient_C=25,
            )
            P_W = self.heater_power * P_full_W if flows else 0
            self._last_ssr_switching = switching
            # Valve hardware step (informational + Cv drift, leak tracking)
            self.valve_takeoff_hw.s.V_supply = self.V_mains
            self.valve_water_hw.s.V_supply = self.V_mains
            self.valve_takeoff_hw.step(dt, self.t_sim_s,
                                       cmd_open=self.valve_takeoff_open)
            self.valve_water_hw.step(dt, self.t_sim_s,
                                     cmd_open=self.valve_water_open)
        else:
            P_W = self.heater_power * self.boiler.p.P_max_kW * 1000 if self.contactor_enable else 0
            self._last_ssr_switching = False

        # Cooling water inlet T (with optional hot day disturbance)
        T_water_in = self.faults.cooling_water_hot if self.faults.cooling_water_hot is not None else 12.0
        self.condenser.s.T_water_in_C = T_water_in
        self.condenser.s.water_valve_open = self.valve_water_open
        # Stage 16: servo step → актуализирует current flow в condenser
        if self.water_servo_valve is not None:
            self.water_servo_valve.step(dt, tap_pressure_bar=2.5)
            self.condenser.s.water_flow_lpm = self.water_servo_valve.read_flow_lpm()
            # Открыт если flow > 0.1 L/min (servo angle > ~3°)
            self.condenser.s.water_valve_open = (
                self.water_servo_valve.read_flow_lpm() > 0.1
            )

        # 1. Boiler step
        m_dot_vapor, y_vapor_mass, T_boil = self.boiler.step(dt, P_W, P_atm)

        # 2. Column step — vapor up, reflux composition = top plate (LM)
        # Effective reflux ratio from valve duty (in LM):
        # When valve closed: all condensate returns → R = ∞
        # When valve fully open: R = 0
        # In practice we don't know duty here (that's controller-side);
        # we approximate by saying: when valve open NOW → R=0, otherwise R=very large
        R_eff = 0.1 if self.valve_takeoff_open else 100.0

        y_top_mass = self.column.step(dt, m_dot_vapor, self.boiler.s.x_mass,
                                       R_eff, P_atm, T_boil)

        # 3. Condenser step — all vapor condenses
        # Product stream goes through small product condenser (если valve открыт)
        m_dot_product = m_dot_vapor if self.valve_takeoff_open else 0.0
        self.condenser.step(dt, m_dot_vapor, y_top_mass,
                            self.faults.water_cutoff,
                            m_dot_product_kg_s=m_dot_product)

        # 4. Узел отбора (LM): если клапан открыт, конденсат уходит в активный
        # приёмник. Устройство автоперевода (samogon-i-vodka.ru): гидрозатвор
        # без подвижных частей. Объём голов = глубина щупа в герметичной
        # бутылке. Когда жидкость достигает кончика щупа → атм. канал
        # перекрыт → давление выталкивает спирт через боковой выход с
        # контактным датчиком → сигнал контроллеру «переключай на body».
        # Дальше body → tails в кубе после T_kub > 95°C.
        # Hardware integration (stage 8): Cv drift и leak_rate влияют на actual flow.
        if self.realistic_hw_enabled:
            # Cv ratio: heat-soak ×1.5-1.9 → пропорционально больше delivered flow
            cv_ratio = (self.valve_takeoff_hw.s.Cv_effective
                        / max(self.valve_takeoff_hw.Cv_nominal, 0.01))
        else:
            cv_ratio = 1.0

        if self.valve_takeoff_open and m_dot_vapor > 0:
            m_dt = m_dot_vapor * dt * cv_ratio  # Cv drift влияет
            rho_prod = density_mix_liq(y_top_mass, 60)
            V_dt_L = m_dt / rho_prod * 1000

            self._add_to_active_receiver(V_dt_L, y_top_mass)

            # Куб теряет соответствующую массу (с составом y_top)
            rho_kub = density_mix_liq(self.boiler.s.x_mass, self.boiler.s.T_bulk_C)
            m_kub_kg = self.boiler.s.V_total_L / 1000 * rho_kub
            if m_kub_kg > m_dt:
                for k in range(N_COMP):
                    eth_before = m_kub_kg * self.boiler.s.x_mass[k]
                    eth_leaving = m_dt * y_top_mass[k]
                    eth_after = max(0, eth_before - eth_leaving)
                    new_total = m_kub_kg - m_dt
                    self.boiler.s.x_mass[k] = eth_after / new_total if new_total > 0 else 0
                tot = sum(self.boiler.s.x_mass)
                if tot > 0:
                    self.boiler.s.x_mass = [x/tot for x in self.boiler.s.x_mass]
                rho_new = density_mix_liq(self.boiler.s.x_mass, self.boiler.s.T_bulk_C)
                self.boiler.s.V_total_L -= m_dt / rho_new * 1000

        # 4b. Valve leak when closed: dribble продолжает капать в receiver
        # даже когда controller просит valve closed (FKM swell / seat debris).
        # Использует ту же composition что и last_y_top — fallback ε если ноль.
        if (self.realistic_hw_enabled and not self.valve_takeoff_open
                and m_dot_vapor > 0):
            leak_ml_s = self.valve_takeoff_hw.s.leak_rate_ml_s_when_closed
            if self.valve_takeoff_hw.s.seat_debris:
                leak_ml_s += 0.5
            if leak_ml_s > 0:
                V_leak_L = leak_ml_s / 1000 * dt
                self._add_to_active_receiver(V_leak_L, y_top_mass)

        # 5. Level sensor (контактный кондуктометрический щуп 70×10mm,
        # 2m cable, russsam.ru датчик наполнения для БКУ-07; жидкость между
        # двумя электродами щупа замыкает контакт → controller видит «полно»).
        # physical_full=True после срабатывания коромысла (переход heads→body).
        physical_full = self.heads_full_flag
        self.level_sensor_heads.step(dt, self.t_sim_s, physical_full)

        # 6. Механическое переключение body→tails: при сильно обеднённом кубе
        # (~ выходе на хвосты по T_kub) оператор/механика переставляет ёмкость.
        # Threshold: T_kub > 95°C — то же что используют реальные операторы.
        if self.active_receiver == "body" and self.boiler.s.T_bulk_C > 95:
            self.active_receiver = "tails"

        # 7. BME680 на атмосферной трубке (за штатным DS1821 thermal trip).
        # Температура в позиции BME680 ≈ T_atm_tube минус 5°C (cooling за
        # счёт расстояния от DS1821 + forced convection помпой).
        if self.bme680_atm is not None:
            o_voc_ppm = _compute_atm_tube_voc(
                y_top_mass=self.column.s.y_top_mass,
                m_dot_vapor_kg_s=getattr(self.boiler.s, '_last_m_dot_vapor', 0.0),
                Cv=self.column.s.Cv,
                T_water_out_C=self.condenser.s.T_water_out_C,
                main_bypass_closed=self.condenser.s.main_bypass_closed,
                column_flooded=self.column.s.flooded,
                is_boiling=self.boiler.s.is_boiling,
            )
            T_at_sensor = _compute_T_atm_tube(
                T_water_out_C=self.condenser.s.T_water_out_C,
                T_head_C=self.column.T_top_C(),
                main_bypass_closed=self.condenser.s.main_bypass_closed,
                column_flooded=self.column.s.flooded,
                Cv=self.column.s.Cv,
            ) - 5  # на 5°C ниже trubki (помпа + distance)
            self.bme680_atm.step(
                dt, self.t_sim_s,
                T_at_sensor_C=T_at_sensor,
                RH_ambient_pct=45,
                P_atm_hPa=self.P_atm_Pa_base / 100,
                ethanol_ppm_eq=o_voc_ppm,
            )

        self.t_sim_s += dt

    def _add_to_active_receiver(self, V_L: float, composition_mass: List[float]):
        """Добавляет жидкость в текущий активный приёмник.
        Когда уровень голов достигает heads_cup_volume_L — жидкость дотянулась
        до кончика щупа устройства автоперевода, перекрыла его, давление в
        герметичной бутылке голов растёт, поток переключается на боковой выход
        с контактным датчиком (см. DEFAULT_HEADS_CUP_VOLUME_L docstring)."""
        if self.active_receiver == "heads":
            new_V = self.V_heads_L + V_L
            if new_V > 0:
                for k in range(N_COMP):
                    self.x_heads_mass[k] = (
                        self.x_heads_mass[k] * self.V_heads_L
                        + composition_mass[k] * V_L
                    ) / new_V
            self.V_heads_L = new_V

            # Сифон срабатывает при заполнении ёмкости голов
            if self.V_heads_L >= self.heads_cup_volume_L and not self.heads_full_flag:
                self.heads_full_flag = True
                self.active_receiver = "body"
        elif self.active_receiver == "body":
            new_V = self.V_body_L + V_L
            if new_V > 0:
                for k in range(N_COMP):
                    self.x_body_mass[k] = (
                        self.x_body_mass[k] * self.V_body_L
                        + composition_mass[k] * V_L
                    ) / new_V
            self.V_body_L = new_V
        else:  # tails
            new_V = self.V_tails_L + V_L
            if new_V > 0:
                for k in range(N_COMP):
                    self.x_tails_mass[k] = (
                        self.x_tails_mass[k] * self.V_tails_L
                        + composition_mass[k] * V_L
                    ) / new_V
            self.V_tails_L = new_V

    def switch_to_tails_receiver(self):
        """Вызывается контроллером при переходе в фазу TAILS — переключает
        активный приёмник на третью ёмкость (в реале — оператор подставляет)."""
        self.active_receiver = "tails"

    # Совместимость со старым API
    @property
    def V_product_L(self) -> float:
        return self.V_heads_L + self.V_body_L + self.V_tails_L

    @property
    def x_product_mass(self) -> List[float]:
        """Усреднённый состав всех приёмников по массе."""
        total_V = self.V_product_L
        if total_V < 1e-9:
            return [0]*N_COMP
        out = [0.0]*N_COMP
        for k in range(N_COMP):
            out[k] = (
                self.x_heads_mass[k] * self.V_heads_L
                + self.x_body_mass[k] * self.V_body_L
                + self.x_tails_mass[k] * self.V_tails_L
            ) / total_V
        return out

    def observables(self) -> StillObservables:
        b = self.boiler.s
        c = self.condenser.s
        col = self.column.s
        return StillObservables(
            T_kub_bulk_C=b.T_bulk_C,
            T_kub_wall_C=b.T_walls_C,
            T_head_C=self.column.T_top_C() + b.probe_fouled_bias_C,
            # T_atm_tube: атмосферная трубка дефлегматора — выход для
            # несконденсированных газов. В норме держится около T_water_out +
            # 5-15°C (пар успевает сконденсироваться полностью). Растёт
            # резко при: потере охлаждения (bypass), захлёбе, прорыве голов.
            # Порог 93°C у БКУ-07М — это именно детект прорыва.
            T_atm_tube_C=_compute_T_atm_tube(
                T_water_out_C=c.T_water_out_C,
                T_head_C=self.column.T_top_C(),
                main_bypass_closed=c.main_bypass_closed,
                column_flooded=col.flooded,
                Cv=col.Cv,
            ),
            atm_tube_voc_ppm=_compute_atm_tube_voc(
                y_top_mass=col.y_top_mass,
                m_dot_vapor_kg_s=getattr(b, '_last_m_dot_vapor', 0.0),
                Cv=col.Cv,
                T_water_out_C=c.T_water_out_C,
                main_bypass_closed=c.main_bypass_closed,
                column_flooded=col.flooded,
                is_boiling=b.is_boiling,
            ),
            T_water_in_C=c.T_water_in_C,
            T_water_out_C=c.T_water_out_C,
            P_atm_hPa=self.P_atm_Pa_base / 100,
            V_kub_L=b.V_total_L,
            V_product_L=self.V_product_L,
            V_heads_L=self.V_heads_L,
            V_body_L=self.V_body_L,
            V_tails_L=self.V_tails_L,
            active_receiver=self.active_receiver,
            heads_full_flag=self.heads_full_flag,
            x_kub_abv=mass_to_abv_vol(b.x_mass[1]) if sum(b.x_mass) > 0 else 0,
            x_head_abv=mass_to_abv_vol(self._top_eth_mass()),
            x_product_abv=mass_to_abv_vol(self.x_product_mass[1]),
            x_heads_abv=mass_to_abv_vol(self.x_heads_mass[1]),
            x_body_abv=mass_to_abv_vol(self.x_body_mass[1]),
            x_tails_abv=mass_to_abv_vol(self.x_tails_mass[1]),
            x_head_mass=self.column._top_composition(),
            x_product_mass=self.x_product_mass[:],
            Cv=col.Cv,
            delta_P_Pa=col.delta_P_Pa,
            vapor_velocity_m_s=col.vapor_velocity_m_s,
            pre_flood_oscillation=col.pre_flood_oscillation,
            flooded=col.flooded,
            weeping=col.weeping,
            foam_active=b.foam_active,
            is_boiling=b.is_boiling,
            dry_out=b.dry_out,
            P_heater_kW=self.heater_power * self.boiler.p.P_max_kW if self.contactor_enable else 0,
            P_to_vapor_kW=b.P_to_vapor_kW,
            m_dot_vapor_g_s=b.m_dot_vapor_kg_s * 1000,
        )

    def _top_eth_mass(self) -> float:
        top = self.column._top_composition()
        return top[1] if len(top) > 1 else 0

    def apply_outputs(self, outs):
        """ESP-аналог: получаем команды от контроллера."""
        self.heater_power = outs.heater_power
        self.valve_takeoff_open = outs.valve_takeoff
        self.valve_water_open = outs.valve_water
        self.contactor_enable = outs.contactor_enable
        # Stage 16: proportional water flow (если controller управляет через
        # servo+needle valve). Иначе fallback на ручную/постоянную регулировку.
        if outs.water_flow_main_lpm is not None:
            if self.water_servo_valve is not None:
                # Convert flow setpoint to servo angle (linear approx)
                pct = (outs.water_flow_main_lpm
                       / self.water_servo_valve.s.flow_max_lpm) * 100
                self.water_servo_valve.set_flow_pct(pct)
                # actual flow updated in step() → передастся в condenser
            else:
                # Servo не установлен — пишем напрямую в condenser
                self.condenser.s.water_flow_lpm = outs.water_flow_main_lpm

    def read_sensors(self) -> dict:
        """То, что 'ESP читает с DS18B20'. С enable_realistic_hardware()
        — пропускаем через DS18B20 модели (sentinels, noise, drift, CRC fails
        кластеризованные около SSR switching events)."""
        o = self.observables()
        if self.realistic_hw_enabled:
            ssr_sw = self._last_ssr_switching
            dt_hint = 1.0  # not strictly tied to step dt; for drift accumulation
            t_kub = self.ds18b20_T_kub.read(dt_hint, self.t_sim_s, o.T_kub_bulk_C, ssr_sw)
            t_kub_wall = self.ds18b20_T_kub_wall.read(dt_hint, self.t_sim_s, o.T_kub_wall_C, ssr_sw)
            t_head = self.ds18b20_T_head.read(dt_hint, self.t_sim_s, o.T_head_C, ssr_sw)
            t_water_in = self.ds18b20_T_water_in.read(dt_hint, self.t_sim_s, o.T_water_in_C, ssr_sw)
            t_water_out = self.ds18b20_T_water_out.read(dt_hint, self.t_sim_s, o.T_water_out_C, ssr_sw)
        else:
            t_kub = o.T_kub_bulk_C
            t_kub_wall = o.T_kub_wall_C
            t_head = o.T_head_C
            t_water_in = o.T_water_in_C
            t_water_out = o.T_water_out_C
        return {
            "T_kub": t_kub,
            "T_kub_wall": t_kub_wall,
            "T_head": t_head,
            "T_atm_tube": o.T_atm_tube_C,
            "atm_tube_voc_ppm": o.atm_tube_voc_ppm,
            "T_water_in": t_water_in,
            "T_water_out": t_water_out,
            "P_atm_hPa": o.P_atm_hPa,
            "is_boiling": o.is_boiling,
            "V_kub": o.V_kub_L,
            "V_product": o.V_product_L,
            "x_kub_abv": o.x_kub_abv,
            "x_head_abv": o.x_head_abv,
            "x_product_abv": o.x_product_abv,
            "Cv": o.Cv,
            "delta_P_Pa": o.delta_P_Pa,
            "duty_avg": 0.0,
            "P_heater_kW": o.P_heater_kW,
            "m_dot_vapor_gps": o.m_dot_vapor_g_s,
            "estop": self.faults.estop_pressed if hasattr(self.faults, 'estop_pressed') else False,
            "bimetal_tripped": self.faults.bimetal_tripped if hasattr(self.faults, 'bimetal_tripped') else False,
            "foam_active": o.foam_active,
            "flooded": o.flooded,
            "weeping": o.weeping,
            "dry_out": o.dry_out,
            # Level sensor signal (debounced) — primary trigger HEADS→STABILIZE2
            "level_heads_full": self.level_sensor_heads.s.debounced_signal,
            "level_heads_raw": self.level_sensor_heads.s.raw_signal,
            "level_oxidation": self.level_sensor_heads.s.oxidation_level,
            "V_heads_L": o.V_heads_L,
            "V_body_L": o.V_body_L,
            "active_receiver": o.active_receiver,
            # Hardware diagnostics (только если включено)
            "ssr_T_junction_C": self.ssr_heater.s.T_junction_C if self.realistic_hw_enabled else None,
            "ssr_fail_short": self.ssr_heater.s.fail_short if self.realistic_hw_enabled else None,
            "valve_takeoff_Cv": self.valve_takeoff_hw.s.Cv_effective if self.realistic_hw_enabled else None,
            "valve_takeoff_T_coil": self.valve_takeoff_hw.s.T_coil_C if self.realistic_hw_enabled else None,
            "valve_takeoff_stiction_N": self.valve_takeoff_hw.s.stiction_force_N if self.realistic_hw_enabled else None,
            "contactor_chatter": self.contactor_hw.s.chatter if self.realistic_hw_enabled else None,
            "contactor_wear": self.contactor_hw.s.contact_wear if self.realistic_hw_enabled else None,
            # Per-sensor CRC/sentinel counters (12.11.4): controller использует
            # их для suspect-sensor detection через SuspectSensorAnalyzer.
            "ds_T_kub_crc_fails": self.ds18b20_T_kub.s.crc_fail_count if self.realistic_hw_enabled else None,
            "ds_T_kub_sentinels": self.ds18b20_T_kub.s.sentinel_count if self.realistic_hw_enabled else None,
            "ds_T_head_crc_fails": self.ds18b20_T_head.s.crc_fail_count if self.realistic_hw_enabled else None,
            "ds_T_head_sentinels": self.ds18b20_T_head.s.sentinel_count if self.realistic_hw_enabled else None,
            "ds_T_water_in_crc_fails": self.ds18b20_T_water_in.s.crc_fail_count if self.realistic_hw_enabled else None,
            "ds_T_water_in_sentinels": self.ds18b20_T_water_in.s.sentinel_count if self.realistic_hw_enabled else None,
            "ds_T_water_out_crc_fails": self.ds18b20_T_water_out.s.crc_fail_count if self.realistic_hw_enabled else None,
            "ds_T_water_out_sentinels": self.ds18b20_T_water_out.s.sentinel_count if self.realistic_hw_enabled else None,
            "ds_T_kub_wall_crc_fails": self.ds18b20_T_kub_wall.s.crc_fail_count if self.realistic_hw_enabled else None,
            "ds_T_kub_wall_sentinels": self.ds18b20_T_kub_wall.s.sentinel_count if self.realistic_hw_enabled else None,
            # Stage 15: BME680 на атмосферной трубке (за DS1821 + помпа)
            "bme680_R_gas": self.bme680_atm.s.R_gas_ohm if self.bme680_atm else None,
            "bme680_voc_index": self.bme680_atm.s.voc_index if self.bme680_atm else None,
            "bme680_T_C": self.bme680_atm.s.T_C if self.bme680_atm else None,
            "bme680_P_hPa": self.bme680_atm.s.P_hPa if self.bme680_atm else None,
            "bme680_damaged": self.bme680_atm.s.damaged if self.bme680_atm else None,
        }
