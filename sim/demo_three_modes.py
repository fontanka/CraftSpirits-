"""
sim/demo_three_modes.py — сравнение 3 стратегий регулировки отбора на
одной ХД/4-375 setup (15L grain @ 14% ABV, 3 kW):

1. auto_pwm     — БКУ-07М style: parallel condensers, PWM клапан спирта,
                  constant water flow. Что было в стандартном setup.
2. manual_series — User's manual mode: series condensers (вода + спирт),
                  continuous takeoff (без клапана), smooth water control.
                  93% ABV руками.
3. hybrid       — User's идея upgrade: parallel + PWM takeoff + smooth
                  water control через servo+needle. Best of both worlds.

Запуск: python demo_three_modes.py
"""
from __future__ import annotations

import random

from physics import Still
from controller import Controller, Mode, Phase, Recipe


def run_mode(mode_name: str, **recipe_kwargs) -> dict:
    random.seed(42)
    s = Still(
        column_diameter_m=0.058, heater_kW=3.0,
        column_type="bubble_cap", n_plates=5, column_H_m=0.375,
    )
    use_servo = recipe_kwargs.pop("use_servo", False)
    topology = recipe_kwargs.pop("topology", "parallel")
    s.enable_realistic_hardware(atm_gas_sensors=True, water_servo_valve=use_servo)
    s.condenser.s.cooling_topology = topology
    s.set_initial(V_L=15, abv_vol=14, mash_type="grain")

    recipe = Recipe(mode=Mode.REFLUX)
    recipe.heater_max_kW = 3.0
    for k, v in recipe_kwargs.items():
        setattr(recipe, k, v)
    ctrl = Controller(recipe)
    ctrl.start(0.0, initial_sensors=s.read_sensors())

    abv_samples, body_t = [], None
    for i in range(20000):
        sensors = s.read_sensors()
        outs = ctrl.tick(i, sensors)
        s.apply_outputs(outs)
        s.step(1.0)
        if ctrl.st.phase == Phase.BODY:
            if body_t is None:
                body_t = i
            abv_samples.append(s.observables().x_product_abv)
        if ctrl.st.phase in (Phase.DONE, Phase.CLOSED, Phase.EMERGENCY):
            break

    avg = sum(abv_samples) / max(len(abv_samples), 1) if abv_samples else 0
    return {
        "mode": mode_name,
        "phase": ctrl.st.phase.value,
        "session_h": i / 3600,
        "V_body": s.V_body_L,
        "x_body": s.observables().x_product_abv,
        "avg_abv_body": avg,
        "alerts": len(ctrl.st.alerts),
    }


def main():
    print("ХД/4-375 + 3 kW + 15 L @ 14% grain — сравнение управлений\n")
    results = []

    # 1. Классический БКУ-07М auto: PWM, parallel, constant water
    results.append(run_mode(
        "auto_pwm (БКУ)",
        takeoff_mode="pwm", water_control_mode="constant", topology="parallel",
        use_servo=False,
    ))

    # 2. Manual mode: continuous, series, smooth water
    results.append(run_mode(
        "manual_series (руки)",
        takeoff_mode="continuous", water_control_mode="smooth_pid",
        topology="series", use_servo=True,
    ))

    # 3. Hybrid (твоя upgrade идея): PWM + smooth water
    results.append(run_mode(
        "hybrid (servo+ШИМ)",
        takeoff_mode="smooth", water_control_mode="smooth_pid",
        topology="parallel", use_servo=True,
    ))

    fmt = "{:<22} {:>10} {:>8} {:>8} {:>10} {:>9}"
    print(fmt.format("mode", "phase", "h", "V_body", "ABV_avg", "alerts"))
    print("-" * 75)
    for r in results:
        print(fmt.format(
            r["mode"], r["phase"], f"{r['session_h']:.2f}",
            f"{r['V_body']:.2f}", f"{r['avg_abv_body']:.2f}%", str(r["alerts"]),
        ))
    print()
    print("Sim даёт качественную картинку. Точные числа требуют калибровки")
    print("bubble-cap dynamics против реальных замеров на железе.")


if __name__ == "__main__":
    main()
