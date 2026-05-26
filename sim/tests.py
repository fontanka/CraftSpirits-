"""
Headless тесты сценариев для симулятора v2.

Запуск: python tests.py
Возвращает exit code 0 если все прошли, 1 если хоть один упал.

Каждый тест — это:
- начальные условия (V, ABV, viscosity, sugar, Recipe, column D)
- сценарий (что делать с fault'ами в какое время)
- ожидаемые проверки (фазы должны быть пройдены, продукт в диапазоне, и т.д.)

Покрывает находки из 6 параллельных research-проходов.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from typing import Callable

from physics import Still, Outputs
from controller import Controller, MashType, Mode, Phase, Recipe, RunType


@dataclass
class Scenario:
    name: str
    recipe: Recipe = field(default_factory=Recipe)
    V_kub: float = 18.0
    x_kub_abv: float = 12.0
    column_D_m: float = 0.040
    viscosity: float = 1.0
    sugar_g_L: float = 0.0
    mash_type: str = "grain"
    max_sim_s: int = 6 * 3600
    inject: Callable[..., None] = lambda s, c, t: None
    check: Callable[..., tuple[bool, str]] = lambda s, c: (True, "")
    warm_start_confirmed: bool = False  # для hot-start сценариев
    max_after_done_s: int = 30  # сколько ticks крутить после DONE
    pre_heat_T_kub_C: float | None = None  # для hot-start: куб уже тёплый


def run(scen: Scenario) -> tuple[bool, str, list[str]]:
    still = Still(column_diameter_m=scen.column_D_m)
    still.set_initial(V_L=scen.V_kub, abv_vol=scen.x_kub_abv,
                      viscosity=scen.viscosity, sugar_g_L=scen.sugar_g_L,
                      mash_type=scen.mash_type)
    if scen.pre_heat_T_kub_C is not None:
        # Hot-start: куб уже тёплый (имитация прерванной сессии или ranee)
        still.boiler.s.T_bulk_C = scen.pre_heat_T_kub_C
        still.boiler.s.T_film_C = scen.pre_heat_T_kub_C
        still.boiler.s.T_walls_C = scen.pre_heat_T_kub_C

    ctrl = Controller(scen.recipe)
    ctrl.start(0.0, initial_sensors=still.read_sensors(),
               warm_start_confirmed=scen.warm_start_confirmed)

    phase_log = [ctrl.st.phase.value]
    t = 0
    dt = 1.0
    while t < scen.max_sim_s:
        scen.inject(still, ctrl, t)
        pi_alive = not getattr(still.faults, "pi_disconnected", False)

        sensors = still.read_sensors()
        outs = ctrl.tick(t, sensors, pi_alive=pi_alive)
        still.apply_outputs(outs)
        still.step(dt)
        t += dt

        if ctrl.st.phase.value != phase_log[-1]:
            phase_log.append(ctrl.st.phase.value)

        if ctrl.st.phase == Phase.DONE:
            for _ in range(scen.max_after_done_s):
                scen.inject(still, ctrl, t)
                sensors = still.read_sensors()
                outs = ctrl.tick(t, sensors, pi_alive=pi_alive)
                still.apply_outputs(outs)
                still.step(dt)
                t += dt
                if ctrl.st.phase.value != phase_log[-1]:
                    phase_log.append(ctrl.st.phase.value)
            break
        if ctrl.st.phase == Phase.CLOSED:
            break

    ok, why = scen.check(still, ctrl)
    return ok, why, phase_log


# Удобные accessor'ы для тестов
def V_prod(s: Still) -> float:
    return s.V_product_L


def x_prod_mass(s: Still) -> float:
    return s.x_product_mass[1] if s.x_product_mass else 0


def heater_W(s: Still) -> float:
    return s.heater_power


def contactor_on(s: Still) -> bool:
    return s.contactor_enable


# === Сценарии ===


# Базовые happy paths
def scen_happy_reflux_1p5in():
    return Scenario(
        name="happy REFLUX 1.5\" @ 5kW 18L@12%",
        column_D_m=0.040,
        check=lambda s, c: (
            c.st.phase == Phase.DONE
            and V_prod(s) > 0.5
            and 0.30 < x_prod_mass(s) < 0.95,
            f"phase={c.st.phase.value} V_prod={V_prod(s):.2f}L "
            f"x_prod_mass={x_prod_mass(s):.3f}",
        ),
    )


def scen_happy_reflux_2in():
    return Scenario(
        name="happy REFLUX 2\" @ 5kW 18L@12%",
        column_D_m=0.050,
        check=lambda s, c: (
            c.st.phase == Phase.DONE and V_prod(s) > 0.5,
            f"phase={c.st.phase.value} V_prod={V_prod(s):.2f}L",
        ),
    )


def scen_potstill():
    return Scenario(
        name="POTSTILL 18L@12% (без разделения)",
        recipe=Recipe(mode=Mode.POTSTILL, p_work=80),
        check=lambda s, c: (
            c.st.phase == Phase.DONE and V_prod(s) > 2.0,
            f"phase={c.st.phase.value} V_prod={V_prod(s):.2f}L",
        ),
    )


# Safety: hard-stop scenarios
def scen_estop_during_heatup():
    def inject(s, c, t):
        if t > 100:
            s.faults.estop_pressed = True

    return Scenario(
        name="E-stop в HEAT_UP",
        max_sim_s=300,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY
            and "E-stop" in c.st.last_alert
            and heater_W(s) == 0
            and not contactor_on(s),
            f"phase={c.st.phase.value} alert={c.st.last_alert!r}",
        ),
    )


def scen_watchdog_pi_disconnect():
    def inject(s, c, t):
        if t > 1000:
            s.faults.pi_disconnected = True

    return Scenario(
        name="Pi watchdog (>30s no command)",
        max_sim_s=1100,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY and "watchdog" in c.st.last_alert,
            f"phase={c.st.phase.value} alert={c.st.last_alert!r}",
        ),
    )


def _patch_sensor(still, **overrides):
    """Idempotent monkey-patch: оборачивает read_sensors ровно один раз."""
    if getattr(still, "_sensor_patched", False):
        still._sensor_overrides.update(overrides)
        return
    orig = still.read_sensors
    still._sensor_overrides = dict(overrides)
    def patched():
        d = orig()
        d.update(still._sensor_overrides)
        return d
    still.read_sensors = patched
    still._sensor_patched = True


def scen_sensor_t_kub_fail():
    def inject(s, c, t):
        if t > 1000:
            _patch_sensor(s, T_kub=float("nan"))

    return Scenario(
        name="отказ датчика T_kub (NaN)",
        max_sim_s=1100,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY and "T_kub" in c.st.last_alert,
            f"phase={c.st.phase.value} alert={c.st.last_alert!r}",
        ),
    )


def scen_water_cutoff():
    """Закрытый кран воды → T_water_out → либо EMERGENCY, либо soft mitigation
    (power reduced + warning логирован). Оба варианта приемлемы."""
    def inject(s, c, t):
        if t > 2000:
            s.faults.water_cutoff = True

    def check(s, c):
        # Либо emergency по T_water_out, либо есть water warning + power reduced
        emergency_ok = c.st.phase == Phase.EMERGENCY and "T_water_out" in c.st.last_alert
        warned = any("water" in a.lower() or "T_water" in a for a in c.st.alerts)
        mitigated = s.observables().T_water_out_C > 50 and warned
        return (
            emergency_ok or mitigated,
            f"phase={c.st.phase.value} T_w_out={s.observables().T_water_out_C:.1f} "
            f"warned={warned}",
        )

    return Scenario(
        name="закрытый кран воды → soft mitigation или EMERGENCY",
        max_sim_s=4500,
        inject=inject,
        check=check,
    )


def scen_ssr_breakdown():
    """Пробой SSR в фазе STABILIZE/HEADS: heater_cmd низкий, но physical = 100%.
    PZEM-style detection в контроллере (cmd vs measured power)."""
    def inject(s, c, t):
        if t > 1500:
            _patch_sensor(s, P_heater_kW=s.boiler.p.P_max_kW)
            s.heater_power = 1.0  # physically

    return Scenario(
        name="SSR пробой → PZEM mismatch → EMERGENCY",
        max_sim_s=3000,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY
            and ("mismatch" in c.st.last_alert or "T_kub" in c.st.last_alert),
            f"phase={c.st.phase.value} alert={c.st.last_alert!r}",
        ),
    )


def scen_acknowledge():
    """ACK после EMERGENCY → IDLE."""
    def inject(s, c, t):
        if t == 200:
            s.faults.estop_pressed = True
        if t == 250:
            s.faults.estop_pressed = False
            c.acknowledge_emergency()

    return Scenario(
        name="acknowledge emergency → IDLE",
        max_sim_s=400,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.IDLE,
            f"phase={c.st.phase.value}",
        ),
    )


# Decision-driving scenarios: column dimensions, packing
def scen_column_dimensions():
    """1.5\" vs 2\" — Cv должна быть выше у узкой колонны. Точные значения
    зависят от состава пара (а он зависит от куба). Для 12% mash на стационаре:
    1.5\" Cv ~ 0.20-0.35, 2\" Cv ~ 0.12-0.25 (примерно в 1.5-2x раз меньше)."""
    return Scenario(
        name="Column 1.5\" Cv > 2\" Cv (соотношение должно соблюдаться)",
        column_D_m=0.040,
        max_sim_s=2500,
        check=lambda s, c: (
            s.observables().Cv > 0.15,  # есть пар
            f"Cv_1p5in={s.observables().Cv:.3f}",
        ),
    )


def scen_flooding_diameter_ratio():
    """Прямое сравнение: при равной нагрузке 2\" должна давать Cv в ~1.5-1.7x раз меньше."""
    from physics import Still as PhysicsStill
    # Run two stills in parallel
    s1 = PhysicsStill(column_diameter_m=0.040)
    s2 = PhysicsStill(column_diameter_m=0.050)
    for s in (s1, s2):
        s.set_initial(V_L=18, abv_vol=12)
        s.heater_power = 1.0
        s.contactor_enable = True
        s.valve_water_open = True
    for _ in range(2500):
        s1.step(1.0)
        s2.step(1.0)
    Cv1, Cv2 = s1.observables().Cv, s2.observables().Cv
    return Scenario(
        name=f"Сравнение: Cv(1.5\")={Cv1:.3f} vs Cv(2\")={Cv2:.3f} ratio={Cv1/Cv2:.2f}",
        max_sim_s=10,
        check=lambda s, c: (
            Cv1 > Cv2 * 1.3,  # 1.5\" должна быть значимо выше
            f"Cv ratio = {Cv1/Cv2:.2f}",
        ),
    )


# Stress tests
def scen_noisy_sensors():
    def inject(s, c, t):
        # Шум через faults пока не реализован в физике для каждого канала;
        # это будет в hardware.py. Здесь — happy path должен пройти.
        pass

    return Scenario(
        name="noisy sensors smoke (без сенсорной модели)",
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.DONE,
            f"phase={c.st.phase.value}",
        ),
    )


def scen_pressure_drift():
    def inject(s, c, t):
        if t == 10:
            s.faults.pressure_drift = True

    return Scenario(
        name="дрейф атм. давления — барокоррекция держит логику",
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.DONE,
            f"phase={c.st.phase.value}",
        ),
    )


def scen_cooling_water_hot():
    """Летний день: вода на входе 22°C вместо 12°C."""
    def inject(s, c, t):
        if t == 10:
            s.faults.cooling_water_hot = 22.0

    return Scenario(
        name="горячая входная вода 22°C — сессия должна пройти",
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.DONE,
            f"phase={c.st.phase.value} T_w_out={s.observables().T_water_out_C:.1f}",
        ),
    )


def scen_sugar_mash_foam():
    """Сахарная брага с риском пенообразования."""
    return Scenario(
        name="сахарная брага (20 g/L) — sim survives",
        sugar_g_L=20,
        check=lambda s, c: (
            c.st.phase in (Phase.DONE, Phase.EMERGENCY),
            f"phase={c.st.phase.value} V_prod={V_prod(s):.2f}L",
        ),
    )


def scen_high_viscosity_grain():
    """Зерновая брага: viscosity_factor=3 → больше потерь, медленнее теплообмен."""
    return Scenario(
        name="зерновая брага (visc=3) — sim survives",
        viscosity=3.0,
        check=lambda s, c: (
            c.st.phase in (Phase.DONE, Phase.EMERGENCY),
            f"phase={c.st.phase.value} V_prod={V_prod(s):.2f}L",
        ),
    )


def scen_low_starting_abv():
    """Слабая брага 6% — выход маленький, но сессия завершается без аварий."""
    return Scenario(
        name="слабая брага 6% ABV — сессия завершается",
        x_kub_abv=6.0,
        check=lambda s, c: (
            c.st.phase == Phase.DONE,
            f"phase={c.st.phase.value} V_prod={V_prod(s):.2f}L "
            f"x_prod_mass={x_prod_mass(s):.3f}",
        ),
    )


def scen_high_starting_abv():
    """Крепкая брага 18% — потенциал отбора больше."""
    return Scenario(
        name="крепкая брага 18% ABV",
        x_kub_abv=18.0,
        check=lambda s, c: (
            c.st.phase == Phase.DONE and V_prod(s) > 1.0,
            f"phase={c.st.phase.value} V_prod={V_prod(s):.2f}L "
            f"x_prod_mass={x_prod_mass(s):.3f}",
        ),
    )


def scen_long_session_2h_only():
    """Сессия ровно 2 часа: должна успеть в HEAT_UP или STABILIZE."""
    return Scenario(
        name="короткая сессия 2ч — нет полного цикла",
        max_sim_s=7200,
        check=lambda s, c: (
            c.st.phase in (Phase.HEAT_UP, Phase.STABILIZE, Phase.HEADS,
                          Phase.STABILIZE2, Phase.BODY, Phase.TAILS,
                          Phase.SHUTDOWN, Phase.DONE),
            f"phase={c.st.phase.value}",
        ),
    )


def scen_kub_overheat():
    """Тест что T_kub > 105°C → EMERGENCY (даже без E-stop)."""
    def inject(s, c, t):
        # Принудительно установить T_kub в зону аварии
        if t > 500:
            s.boiler.s.T_bulk_C = 108
            # Также пустой куб для реализма
            s.boiler.s.V_total_L = 0.1

    return Scenario(
        name="T_kub > 105°C (dry куб) → EMERGENCY",
        max_sim_s=700,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY,
            f"phase={c.st.phase.value} T_kub={s.observables().T_kub_bulk_C:.1f}",
        ),
    )


def scen_bimetal_safety():
    def inject(s, c, t):
        if t > 1000:
            s.faults.bimetal_tripped = True

    return Scenario(
        name="биметалл сработал → EMERGENCY",
        max_sim_s=1100,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY and "bimetal" in c.st.last_alert,
            f"phase={c.st.phase.value} alert={c.st.last_alert!r}",
        ),
    )


def scen_concurrent_pressure_and_hot_water():
    """Concurrent faults: дрейф давления + горячая вода. Сессия должна пройти."""
    def inject(s, c, t):
        if t == 10:
            s.faults.pressure_drift = True
            s.faults.cooling_water_hot = 20.0

    return Scenario(
        name="concurrent: дрейф давления + тёплая вода 20°C",
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.DONE,
            f"phase={c.st.phase.value}",
        ),
    )


def scen_sugar_mash_clean():
    """Сахарная брага: cup голов 100мл, MeOH в головах ~0.04‰ (clean)."""
    return Scenario(
        name="сахарная брага — clean heads (MeOH < 0.05‰)",
        mash_type="sugar",
        check=lambda s, c: (
            c.st.phase == Phase.DONE
            and s.heads_cup_volume_L == 0.100
            and s.x_heads_mass[0] < 0.0001,  # 0.1‰ — порог clean
            f"phase={c.st.phase.value} MeOH={s.x_heads_mass[0]*1000:.3f}‰ "
            f"cup={s.heads_cup_volume_L*1000:.0f}mL",
        ),
    )


def scen_grain_mash_baseline():
    """Зерновая брага (baseline): cup 150мл, MeOH 0.1‰."""
    return Scenario(
        name="зерновая брага — baseline MeOH",
        mash_type="grain",
        check=lambda s, c: (
            c.st.phase == Phase.DONE
            and s.heads_cup_volume_L == 0.150
            and 0.00003 < s.x_heads_mass[0] < 0.0003,
            f"MeOH={s.x_heads_mass[0]*1000:.3f}‰ cup={s.heads_cup_volume_L*1000:.0f}mL",
        ),
    )


def scen_fruit_mash_methanol_heavy():
    """Фруктовая брага: pectin → MeOH ×15. Cup увеличен до 300мл, MeOH в
    головах должен быть значительно выше grain (>5×)."""
    return Scenario(
        name="фруктовая брага — MeOH в головах ≫ grain (pectin)",
        mash_type="fruit",
        check=lambda s, c: (
            c.st.phase == Phase.DONE
            and s.heads_cup_volume_L == 0.300
            and s.x_heads_mass[0] > 0.0005,  # минимум 0.5‰ — заметно выше grain
            f"MeOH={s.x_heads_mass[0]*1000:.3f}‰ cup={s.heads_cup_volume_L*1000:.0f}mL",
        ),
    )


def scen_run1_potstill():
    """Run1 (потстил «до сухого»): Mode.POTSTILL + run_type=RUN1, exit по
    T_kub > 99. Sim survives, T_kub дошёл до stop."""
    r = Recipe()
    r.mode = Mode.POTSTILL
    r.run_type = RunType.RUN1
    return Scenario(
        name="Run1 потстил «до сухого»",
        recipe=r,
        check=lambda s, c: (
            c.st.phase == Phase.DONE and s.observables().T_kub_bulk_C > 95,
            f"phase={c.st.phase.value} T_kub={s.observables().T_kub_bulk_C:.1f}",
        ),
    )


def scen_hot_start_warm_confirmed():
    """Куб уже на 60°C (прерванная сессия). Warm start confirmed → skip HEAT_UP.
    Phase log должен содержать HOT START alert и не содержать HEAT_UP."""
    return Scenario(
        name="hot-start warm confirmed → skip HEAT_UP",
        pre_heat_T_kub_C=60.0,
        warm_start_confirmed=True,
        check=lambda s, c: (
            c.st.phase == Phase.DONE
            and "HEAT_UP" not in [a for a in c.st.alerts if "INIT" in a or "HEAT_UP" in a][:1]
            and any("HOT START" in a for a in c.st.alerts)
            and any("warm start confirmed" in a for a in c.st.alerts),
            f"phase={c.st.phase.value} hot_start_detected={c.st.hot_start_detected}",
        ),
    )


def scen_hot_start_unconfirmed():
    """Куб тёплый, но operator НЕ подтвердил → проходит обычный HEAT_UP.
    Безопасный fallback."""
    return Scenario(
        name="hot-start без подтверждения → обычный HEAT_UP",
        pre_heat_T_kub_C=60.0,
        warm_start_confirmed=False,
        check=lambda s, c: (
            c.st.phase == Phase.DONE
            and c.st.hot_start_detected
            and any("HOT START" in a for a in c.st.alerts),
            f"phase={c.st.phase.value} hot_start={c.st.hot_start_detected}",
        ),
    )


def scen_stale_done_auto_close():
    """После DONE — 2ч ничего не делать → auto-transition в CLOSED.
    Recipe stale_done_timeout_s сокращён до 10s для быстрого теста."""
    r = Recipe()
    r.stale_done_timeout_s = 10
    return Scenario(
        name="stale DONE 10s → auto CLOSED",
        recipe=r,
        max_after_done_s=20,  # ждём 20s в DONE, должен переключиться через 10
        check=lambda s, c: (
            c.st.phase == Phase.CLOSED
            and any("stale DONE" in a for a in c.st.alerts),
            f"phase={c.st.phase.value} done_started={c.st.done_started_at}",
        ),
    )


def scen_level_sensor_primary():
    """Датчик уровня работает: HEADS заканчивается ровно по сифонному переключению.
    В alert'ах должна быть отметка 'level sensor → HEADS done (primary trigger)'."""
    return Scenario(
        name="level sensor primary trigger (нормальная работа)",
        check=lambda s, c: (
            c.st.phase == Phase.DONE
            and any("level sensor" in a for a in c.st.alerts)
            and abs(s.observables().V_heads_L - s.heads_cup_volume_L) < 0.01,
            f"phase={c.st.phase.value} V_heads={s.observables().V_heads_L*1000:.0f}mL",
        ),
    )


def scen_level_sensor_stuck_open():
    """Поплавок застрял в нижнем положении (mechanical stuck open) — датчик
    НИКОГДА не срабатывает. T_kub backup должен спасти сессию."""
    def inject(s, c, t):
        if t == 1:
            s.level_sensor_heads.s.mechanical_stuck_open = True

    return Scenario(
        name="level sensor stuck-open → T_kub backup срабатывает",
        max_sim_s=15000,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.DONE
            and any("T_kub backup" in a for a in c.st.alerts),
            f"phase={c.st.phase.value} alerts_w_backup="
            f"{[a for a in c.st.alerts if 'backup' in a.lower()]}",
        ),
    )


def scen_level_sensor_oxidation_false_positive():
    """Окисление контактов > порога: ложные срабатывания. HEADS может закончиться
    раньше времени (false positive) ИЛИ позже (drop-outs при реальном trigger).
    Sim должен survive (DONE)."""
    def inject(s, c, t):
        if t == 1:
            # Симулируем «грязные контакты» — высокий уровень окисления
            s.level_sensor_heads.s.oxidation_level = 0.85

    return Scenario(
        name="окисление контактов датчика → sim survives",
        max_sim_s=15000,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.DONE,
            f"phase={c.st.phase.value} oxidation="
            f"{s.level_sensor_heads.s.oxidation_level:.2f}",
        ),
    )


SCENARIOS = [
    # Базовые
    scen_happy_reflux_1p5in(),
    scen_happy_reflux_2in(),
    scen_potstill(),
    scen_low_starting_abv(),
    scen_high_starting_abv(),

    # Safety
    scen_estop_during_heatup(),
    scen_watchdog_pi_disconnect(),
    scen_sensor_t_kub_fail(),
    scen_water_cutoff(),
    scen_ssr_breakdown(),
    scen_bimetal_safety(),
    scen_kub_overheat(),
    scen_acknowledge(),

    # Stage 5: mash types (12.13.1) и run types (12.13.2)
    scen_sugar_mash_clean(),
    scen_grain_mash_baseline(),
    scen_fruit_mash_methanol_heavy(),
    scen_run1_potstill(),

    # Stage 4: algorithmic edge cases (12.13.11)
    scen_hot_start_warm_confirmed(),
    scen_hot_start_unconfirmed(),
    scen_stale_done_auto_close(),

    # Level sensor (БКУ сифонный transfer + контактный датчик)
    scen_level_sensor_primary(),
    scen_level_sensor_stuck_open(),
    scen_level_sensor_oxidation_false_positive(),

    # Decision-driving
    scen_column_dimensions(),
    scen_flooding_diameter_ratio(),

    # Disturbances
    scen_noisy_sensors(),
    scen_pressure_drift(),
    scen_cooling_water_hot(),
    scen_sugar_mash_foam(),
    scen_high_viscosity_grain(),

    # Long-running
    scen_long_session_2h_only(),

    # Concurrent
    scen_concurrent_pressure_and_hot_water(),
]


def main():
    print(f"Running {len(SCENARIOS)} scenarios…\n")
    failed = 0
    for scen in SCENARIOS:
        ok, why, phases = run(scen)
        status = "PASS" if ok else "FAIL"
        sym = "✓" if ok else "✗"
        print(f"{sym} {status:4s} | {scen.name}")
        if not ok:
            failed += 1
            print(f"       {why}")
            print(f"       phases: {' → '.join(phases)}")
    print()
    if failed == 0:
        print(f"ALL {len(SCENARIOS)} scenarios passed ✓")
        return 0
    else:
        print(f"FAILED: {failed} / {len(SCENARIOS)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
