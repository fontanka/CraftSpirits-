"""
sim/hardware_tests.py — unit tests для hardware module.

Не зависят от physics.py — проверяют каждый компонент изолированно.
Запуск: python hardware_tests.py
"""
from __future__ import annotations

import statistics

from hardware import (
    Contactor, CoilClass, CounterfeitFamily, DS18B20,
    LevelSensor, SSR, Valve,
)


def _run(name: str, cond: bool, detail: str = ""):
    mark = "✓ PASS" if cond else "✗ FAIL"
    print(f"{mark} | {name}", end="")
    if detail:
        print(f"  ({detail})")
    else:
        print()
    return cond


def test_level_sensor_oxidation_progresses():
    """Окисление накапливается со временем экспозиции."""
    ls = LevelSensor()
    for i in range(86400):  # 1 day
        ls.step(1.0, i, physical_full=False)
    return _run(
        "LevelSensor oxidation 1 day",
        0.9 < ls.s.oxidation_level <= 1.0,
        f"oxidation={ls.s.oxidation_level:.3f}",
    )


def test_valve_heat_soak_Cv_drift():
    """Cv должен дрейфовать ~×1.5 после 2 часов работы (см. 12.11.6 punkt 11)."""
    v = Valve(Cv_nominal=1.0)
    # 2 часа постоянно открыт под напряжением → катушка греется
    for i in range(2 * 3600):
        v.step(1.0, i, cmd_open=True, T_ambient_C=25)
    drift = v.s.Cv_effective / 1.0
    return _run(
        "Valve heat-soak: Cv после 2ч",
        1.3 < drift < 2.0,
        f"Cv_drift={drift:.2f}×",
    )


def test_valve_stiction_after_long_off():
    """После 6 часов закрытого простоя — stiction force накапливается."""
    v = Valve()
    v.step(1.0, 0, cmd_open=False)  # initial close
    # 6 часов без активации
    for i in range(1, 6 * 3600):
        v.step(1.0, i, cmd_open=False)
    return _run(
        "Valve stiction 6h closed",
        v.s.stiction_force_N > 0.5,
        f"stiction={v.s.stiction_force_N:.2f}N",
    )


def test_valve_chatter_low_V():
    """V_supply < 85% → chatter активен."""
    v = Valve()
    v.s.V_supply = 180  # ниже 0.85 × 230 = 195.5
    v.step(1.0, 0, cmd_open=True)
    return _run(
        "Valve chatter at 180V",
        v.s.chatter_high_freq,
        f"chatter={v.s.chatter_high_freq}",
    )


def test_ds18b20_counterfeit_noise():
    """Counterfeit семьи C имеют ~10× σ vs original.
    Узкая полоса валидности (76-81°C) исключает sentinel 85.0000 spikes."""
    ds_real = DS18B20(family=CounterfeitFamily.ORIGINAL)
    ds_fake = DS18B20(family=CounterfeitFamily.C)
    real_vals, fake_vals = [], []
    for i in range(500):
        r = ds_real.read(1.0, i, true_T_C=78.5)
        f = ds_fake.read(1.0, i, true_T_C=78.5)
        # Узкое окно вокруг true value — отбрасываем sentinel/jumps
        if 76 < r < 81:
            real_vals.append(r)
        if 76 < f < 81:
            fake_vals.append(f)
    sigma_real = statistics.pstdev(real_vals)
    sigma_fake = statistics.pstdev(fake_vals)
    ratio = sigma_fake / sigma_real if sigma_real > 0 else 0
    return _run(
        "DS18B20 counterfeit C noise > 5× original",
        ratio > 5,
        f"σ_real={sigma_real:.3f} σ_fake={sigma_fake:.3f} ratio={ratio:.1f}×",
    )


def test_ds18b20_disconnect_sentinel():
    """Отключённый сенсор возвращает -127.0."""
    ds = DS18B20()
    ds.disconnect()
    val = ds.read(1.0, 0, true_T_C=78.5)
    return _run(
        "DS18B20 disconnect → -127.0",
        val == -127.0,
        f"reading={val}",
    )


def test_ds18b20_ssr_emi_clustering():
    """SSR switching event повышает CRC fail probability в 5000×."""
    ds = DS18B20()
    crc_fails = 0
    for i in range(100):
        v = ds.read(1.0, i, true_T_C=78.5, ssr_switching=True)
        if v != v:  # NaN check
            crc_fails += 1
    # 5% chance × 100 ticks → ожидаем ~5 fails (стохастика)
    return _run(
        "DS18B20 CRC fails кластерятся при SSR switching",
        crc_fails >= 1,
        f"crc_fails={crc_fails}/100",
    )


def test_ssr_genuine_vs_counterfeit_thermal():
    """Fotek fake перегревается значительно сильнее genuine при той же нагрузке."""
    ssr_real = SSR(is_genuine=True)
    ssr_fake = SSR(is_genuine=False)
    # 5 минут на 22A (5 kW load @ 230V)
    for i in range(300):
        ssr_real.step(1.0, cmd_on=True, I_load_A=22)
        ssr_fake.step(1.0, cmd_on=True, I_load_A=22)
    return _run(
        "SSR Fotek fake T_j > genuine + 20°C",
        ssr_fake.s.T_junction_C > ssr_real.s.T_junction_C + 20,
        f"genuine T_j={ssr_real.s.T_junction_C:.1f} "
        f"fake T_j={ssr_fake.s.T_junction_C:.1f}",
    )


def test_ssr_overheat_fail_short():
    """SSR junction > 100°C → пробой в проводящее (latch).
    Fake SSR + overcurrent 35A → перегрев."""
    ssr = SSR(is_genuine=False)
    for i in range(1800):
        ssr.step(1.0, cmd_on=True, I_load_A=35)
    return _run(
        "SSR overheat → fail_short latch",
        ssr.s.fail_short,
        f"T_j={ssr.s.T_junction_C:.1f} fail_short={ssr.s.fail_short}",
    )


def test_contactor_chatter_at_low_V():
    """Catt < 0.85 × 230 = 195.5V → chatter активен И contact_wear накапливается."""
    c = Contactor()
    for i in range(3600):
        c.step(1.0, cmd_enable=True, V_coil=180)
    return _run(
        "Contactor 1h@180V → chatter + wear",
        c.s.chatter and c.s.contact_wear > 0.05,
        f"chatter={c.s.chatter} wear={c.s.contact_wear:.3f}",
    )


def test_contactor_drop_out_at_very_low_V():
    """V_coil < 65% → катушка не держит, contactor отпускает."""
    c = Contactor()
    c.step(1.0, cmd_enable=True, V_coil=140)
    return _run(
        "Contactor drop-out at 140V",
        not c.s.enabled,
        f"enabled={c.s.enabled}",
    )


TESTS = [
    test_level_sensor_oxidation_progresses,
    test_valve_heat_soak_Cv_drift,
    test_valve_stiction_after_long_off,
    test_valve_chatter_low_V,
    test_ds18b20_counterfeit_noise,
    test_ds18b20_disconnect_sentinel,
    test_ds18b20_ssr_emi_clustering,
    test_ssr_genuine_vs_counterfeit_thermal,
    test_ssr_overheat_fail_short,
    test_contactor_chatter_at_low_V,
    test_contactor_drop_out_at_very_low_V,
]


if __name__ == "__main__":
    print(f"Running {len(TESTS)} hardware unit tests…\n")
    passed = sum(1 for t in TESTS if t())
    print(f"\n{passed}/{len(TESTS)} hardware tests passed", end="")
    if passed == len(TESTS):
        print(" ✓")
    else:
        print(f"  ({len(TESTS)-passed} FAILED)")
