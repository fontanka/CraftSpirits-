"""
Headless тесты сценариев для симулятора.

Запуск: python tests.py
Возвращает exit code 0 если все прошли, 1 если хоть один упал.

Каждый тест — это:
- начальные условия (V, ABV, Recipe)
- сценарий (что делать с fault'ами в какое время)
- ожидаемые проверки (фазы должны быть пройдены, продукт в диапазоне, и т.д.)
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from typing import Callable

from still import Still
from controller import Controller, Mode, Phase, Recipe


@dataclass
class Scenario:
    name: str
    recipe: Recipe = field(default_factory=Recipe)
    V_kub: float = 18.0
    x_kub_abv: float = 12.0
    max_sim_s: int = 6 * 3600
    inject: Callable[[Still, Controller, float], None] = lambda s, c, t: None
    check: Callable[[Still, Controller], tuple[bool, str]] = lambda s, c: (True, "")


def run(scen: Scenario) -> tuple[bool, str, list[str]]:
    still = Still()
    still.s.V_kub = scen.V_kub
    # Конвертация ABV→массовая доля (грубо)
    abv_vol = scen.x_kub_abv
    v = abv_vol / 100.0
    rho_eth, rho_h2o = 0.789, 0.998
    m_eth = v * rho_eth
    m_h2o = (1 - v) * rho_h2o
    still.s.x_kub_mass = m_eth / (m_eth + m_h2o)
    still.s.T_kub = 22.0
    still.s.T_head = 22.0

    ctrl = Controller(scen.recipe)
    ctrl.start(0.0)
    pi_alive = True

    phase_log = [ctrl.st.phase.value]
    t = 0
    dt = 1.0
    while t < scen.max_sim_s:
        scen.inject(still, ctrl, t)
        # Watchdog test: inject через fault.pi_disconnected → выключает pi_alive
        pi_alive = not still.fault.pi_disconnected

        sensors = still.read_sensors()
        outs = ctrl.tick(t, sensors, pi_alive=pi_alive)
        still.apply_outputs(outs)
        still.step(dt)
        t += dt

        if ctrl.st.phase.value != phase_log[-1]:
            phase_log.append(ctrl.st.phase.value)

        # Завершение только на DONE (EMERGENCY можно ACK и продолжить тест)
        if ctrl.st.phase == Phase.DONE:
            for _ in range(30):
                sensors = still.read_sensors()
                outs = ctrl.tick(t, sensors, pi_alive=pi_alive)
                still.apply_outputs(outs)
                still.step(dt)
                t += dt
            break

    ok, why = scen.check(still, ctrl)
    return ok, why, phase_log


# === Сценарии ===


def scen_happy_path():
    return Scenario(
        name="happy_path REFLUX 18L@12%",
        check=lambda s, c: (
            (
                c.st.phase == Phase.DONE
                and s.s.V_product > 1.0
                and s.s.x_product_mass > 0.30
                and s.s.x_product_mass < 0.95
            ),
            f"phase={c.st.phase.value}, V_prod={s.s.V_product:.2f}L, "
            f"x_prod_mass={s.s.x_product_mass:.3f}",
        ),
    )


def scen_estop_during_heatup():
    def inject(s, c, t):
        if t > 100:
            s.fault.estop_pressed = True

    return Scenario(
        name="estop в HEAT_UP",
        max_sim_s=300,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY
            and "E-stop" in c.st.last_alert
            and s.out.heater_power == 0
            and s.out.contactor_enable is False,
            f"phase={c.st.phase.value}, alert={c.st.last_alert!r}, "
            f"heater={s.out.heater_power}, contactor={s.out.contactor_enable}",
        ),
    )


def scen_watchdog_pi_disconnect():
    def inject(s, c, t):
        if t > 1000:
            s.fault.pi_disconnected = True

    return Scenario(
        name="pi watchdog (отключение Pi)",
        max_sim_s=1100,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY and "watchdog" in c.st.last_alert,
            f"phase={c.st.phase.value}, alert={c.st.last_alert!r}",
        ),
    )


def scen_sensor_t_kub_fail():
    def inject(s, c, t):
        if t > 1000:
            s.fault.sensor_t_kub_fail = True

    return Scenario(
        name="отказ датчика T_kub",
        max_sim_s=1100,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY and "T_kub" in c.st.last_alert,
            f"phase={c.st.phase.value}, alert={c.st.last_alert!r}",
        ),
    )


def scen_water_cutoff():
    def inject(s, c, t):
        if t > 2000:
            s.fault.water_cutoff = True

    return Scenario(
        name="закрытый кран воды (нет потока)",
        max_sim_s=4000,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY and "T_water_out" in c.st.last_alert,
            f"phase={c.st.phase.value}, alert={c.st.last_alert!r}",
        ),
    )


def scen_ssr_breakdown():
    """Пробой SSR в фазе HEAT_UP/STABILIZE: heater_cmd низкий, но физически
    100%. T_kub должна улететь выше t_kub_emergency = 105°C → EMERGENCY.
    Это и есть смысл защиты через контактор + биметалл (имитируем биметалл
    срабатыванием T_kub_emergency)."""
    def inject(s, c, t):
        if t > 1500:  # после выхода на режим, в фазе STABILIZE/HEADS
            s.fault.ssr_heater_stuck_on = True

    return Scenario(
        name="пробой SSR ТЭНа",
        max_sim_s=10800,
        inject=inject,
        check=lambda s, c: (
            c.st.phase == Phase.EMERGENCY
            and ("mismatch" in c.st.last_alert or "T_kub" in c.st.last_alert),
            f"phase={c.st.phase.value}, alert={c.st.last_alert!r}",
        ),
    )


def scen_acknowledge():
    """Тест что после ACK выходим из EMERGENCY в IDLE."""
    def inject(s, c, t):
        if t == 200:
            s.fault.estop_pressed = True
        if t == 250:
            s.fault.estop_pressed = False
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


def scen_potstill():
    return Scenario(
        name="POTSTILL 18L@12%, без разделения",
        recipe=Recipe(mode=Mode.POTSTILL, p_work=80),
        check=lambda s, c: (
            c.st.phase == Phase.DONE and s.s.V_product > 2.0,
            f"phase={c.st.phase.value}, V_prod={s.s.V_product:.2f}L",
        ),
    )


def scen_noisy_sensors():
    def inject(s, c, t):
        if t == 10:
            s.fault.sensor_noise = True

    return Scenario(
        name="noisy sensors — стейт-машина не должна сходить с ума",
        check=lambda s, c: (
            c.st.phase == Phase.DONE,
            f"phase={c.st.phase.value}",
        ),
    )


def scen_pressure_drift():
    def inject(s, c, t):
        if t == 10:
            s.fault.pressure_drift = True

    return Scenario(
        name="дрейф давления — барокоррекция должна стабилизировать",
        check=lambda s, c: (
            c.st.phase == Phase.DONE,
            f"phase={c.st.phase.value}",
        ),
    )


SCENARIOS = [
    scen_happy_path(),
    scen_estop_during_heatup(),
    scen_watchdog_pi_disconnect(),
    scen_sensor_t_kub_fail(),
    scen_water_cutoff(),
    scen_ssr_breakdown(),
    scen_acknowledge(),
    scen_potstill(),
    scen_noisy_sensors(),
    scen_pressure_drift(),
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
