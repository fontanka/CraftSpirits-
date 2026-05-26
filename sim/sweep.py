"""
sim/sweep.py — batch harness для статистических прогонов sim v2.

Запускает N сессий с randomized:
- начальные условия (V, ABV, mash_type, viscosity, sugar)
- fault injections (probability per fault)
- hardware variants (counterfeit families, Fotek vs Crydom)

Собирает KPI: % completed normal, % EMERGENCY, average product yield,
distribution времени session, какие alerts чаще всего.

Использование:
    python sweep.py --n 500
    python sweep.py --n 100 --json results.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from physics import Still
from controller import Controller, Recipe, Phase, MashType, Mode
from hardware import CounterfeitFamily, CoilClass


@dataclass
class SweepConfig:
    """Что варьируем в одном прогоне."""
    n_runs: int = 100
    max_sim_s: int = 6 * 3600
    seed_base: int = 42
    enable_hardware: bool = True


@dataclass
class SingleRunResult:
    seed: int
    V_kub: float
    x_kub_abv: float
    mash_type: str
    final_phase: str
    sim_time_s: float
    V_heads_L: float
    V_body_L: float
    V_tails_L: float
    x_body_abv: float
    suspect_sensor: str | None
    n_alerts: int
    last_alert: str
    ssr_T_j_peak_C: float | None
    ssr_fail_short: bool | None
    counterfeit_T_head: str | None
    counterfeit_T_kub: str | None
    ssr_genuine: bool | None
    V_mains: float | None


MASH_TYPES = ["sugar", "grain", "fruit", "mixed"]
COUNTERFEIT_FAMILIES = list(CounterfeitFamily)


def random_config(rng: random.Random, enable_hardware: bool) -> dict:
    """Randomized initial conditions + fault profile."""
    return {
        "V_kub": rng.uniform(15, 25),
        "x_kub_abv": rng.uniform(6, 18),
        "viscosity": rng.uniform(1.0, 3.5),
        "sugar_g_L": rng.choice([0, 0, 0, 10, 25, 50]),
        "mash_type": rng.choice(MASH_TYPES),
        "oborotniy_V_L": rng.choice([0, 0, 0, 0.5, 1.0, 2.0]),
        "oborotniy_abv": rng.uniform(40, 70) if rng.random() < 0.2 else 0,
        # Fault injection probabilities (per session)
        "pressure_drift": rng.random() < 0.3,
        "cooling_water_hot_C": rng.uniform(15, 25) if rng.random() < 0.2 else None,
        "under_fermented": rng.random() < 0.15,
        # Hardware variants
        "counterfeit_T_head": (
            rng.choice(COUNTERFEIT_FAMILIES)
            if enable_hardware and rng.random() < 0.3
            else CounterfeitFamily.ORIGINAL
        ),
        "counterfeit_T_kub": (
            rng.choice(COUNTERFEIT_FAMILIES)
            if enable_hardware and rng.random() < 0.3
            else CounterfeitFamily.ORIGINAL
        ),
        "ssr_genuine": (
            False if enable_hardware and rng.random() < 0.25 else True
        ),
        "V_mains": rng.uniform(195, 245) if enable_hardware else 230,
    }


def run_one(seed: int, max_sim_s: int, enable_hardware: bool) -> SingleRunResult:
    """Один прогон с заданным seed."""
    rng = random.Random(seed)
    cfg = random_config(rng, enable_hardware)

    # Sim-side random тоже seed-ируем для детерминизма
    random.seed(seed)

    still = Still(column_diameter_m=0.040)
    still.set_initial(
        V_L=cfg["V_kub"], abv_vol=cfg["x_kub_abv"],
        viscosity=cfg["viscosity"], sugar_g_L=cfg["sugar_g_L"],
        mash_type=cfg["mash_type"],
        oborotniy_V_L=cfg["oborotniy_V_L"],
        oborotniy_abv=cfg["oborotniy_abv"],
    )
    if cfg["pressure_drift"]:
        still.faults.pressure_drift = True
    if cfg["cooling_water_hot_C"] is not None:
        still.faults.cooling_water_hot = cfg["cooling_water_hot_C"]
    if cfg["under_fermented"]:
        still.faults.under_fermented = True

    if enable_hardware:
        still.enable_realistic_hardware(
            ds_family_T_head=cfg["counterfeit_T_head"],
            ds_family_T_kub=cfg["counterfeit_T_kub"],
            ssr_genuine=cfg["ssr_genuine"],
            V_mains=cfg["V_mains"],
        )

    ctrl = Controller(Recipe())
    ctrl.start(0.0, initial_sensors=still.read_sensors())

    t = 0
    dt = 1.0
    peak_ssr_T_j = 0.0

    while t < max_sim_s:
        sensors = still.read_sensors()
        if enable_hardware and sensors.get("ssr_T_junction_C") is not None:
            peak_ssr_T_j = max(peak_ssr_T_j, sensors["ssr_T_junction_C"])
        outs = ctrl.tick(t, sensors)
        still.apply_outputs(outs)
        still.step(dt)
        t += dt
        if ctrl.st.phase in (Phase.DONE, Phase.CLOSED, Phase.EMERGENCY):
            # Дай ещё немного для post-DONE state machine
            for _ in range(30):
                sensors = still.read_sensors()
                outs = ctrl.tick(t, sensors)
                still.apply_outputs(outs)
                still.step(dt)
                t += dt
            break

    return SingleRunResult(
        seed=seed,
        V_kub=cfg["V_kub"], x_kub_abv=cfg["x_kub_abv"],
        mash_type=cfg["mash_type"],
        final_phase=ctrl.st.phase.value,
        sim_time_s=t,
        V_heads_L=still.V_heads_L,
        V_body_L=still.V_body_L,
        V_tails_L=still.V_tails_L,
        x_body_abv=_estimate_abv(still.x_body_mass),
        suspect_sensor=ctrl.suspect_sensor,
        n_alerts=len(ctrl.st.alerts),
        last_alert=ctrl.st.last_alert[:80] if ctrl.st.last_alert else "",
        ssr_T_j_peak_C=peak_ssr_T_j if enable_hardware else None,
        ssr_fail_short=(
            still.ssr_heater.s.fail_short if enable_hardware else None
        ),
        counterfeit_T_head=cfg["counterfeit_T_head"].value if enable_hardware else None,
        counterfeit_T_kub=cfg["counterfeit_T_kub"].value if enable_hardware else None,
        ssr_genuine=cfg["ssr_genuine"] if enable_hardware else None,
        V_mains=cfg["V_mains"] if enable_hardware else None,
    )


def _estimate_abv(x_mass: list[float]) -> float:
    """Приблизительный ABV из mass fractions (etOH = index 1 в N_COMP=4 системе)."""
    if not x_mass or len(x_mass) < 2:
        return 0.0
    etOH_mass = x_mass[1]
    # mass fraction → volume fraction (rough: ρ_etOH=0.789, ρ_H2O=1.0)
    if etOH_mass <= 0:
        return 0.0
    h2o_mass = sum(x_mass) - etOH_mass
    V_etOH = etOH_mass / 0.789
    V_h2o = h2o_mass / 1.0
    return 100 * V_etOH / (V_etOH + V_h2o) if (V_etOH + V_h2o) > 0 else 0.0


def aggregate(results: list[SingleRunResult]) -> dict:
    n = len(results)
    by_phase: dict[str, int] = {}
    for r in results:
        by_phase[r.final_phase] = by_phase.get(r.final_phase, 0) + 1

    completed_normal = by_phase.get("DONE", 0) + by_phase.get("CLOSED", 0)
    emergency = by_phase.get("EMERGENCY", 0)

    def avg(lst):
        return sum(lst) / max(len(lst), 1)

    return {
        "n_runs": n,
        "by_phase": by_phase,
        "completed_normal_pct": 100 * completed_normal / n,
        "emergency_pct": 100 * emergency / n,
        "avg_sim_time_h": avg([r.sim_time_s / 3600 for r in results]),
        "avg_V_body_L": avg([r.V_body_L for r in results]),
        "avg_V_heads_L": avg([r.V_heads_L for r in results]),
        "avg_n_alerts": avg([r.n_alerts for r in results]),
        "n_suspect_detected": sum(1 for r in results if r.suspect_sensor),
        "n_ssr_fail_short": sum(1 for r in results if r.ssr_fail_short),
        "ssr_T_j_peak_max_C": max(
            (r.ssr_T_j_peak_C or 0 for r in results), default=0
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="Number of runs")
    ap.add_argument("--max-time", type=int, default=6*3600,
                    help="Max sim seconds per run")
    ap.add_argument("--no-hardware", action="store_true")
    ap.add_argument("--json", type=str, default=None, help="Write per-run JSON")
    ap.add_argument("--seed-base", type=int, default=42)
    args = ap.parse_args()

    print(f"Running {args.n} sims (max {args.max_time}s each, "
          f"hw={'off' if args.no_hardware else 'on'})…")

    results: list[SingleRunResult] = []
    t_start = time.time()
    for i in range(args.n):
        seed = args.seed_base + i
        r = run_one(seed, args.max_time, not args.no_hardware)
        results.append(r)
        if (i + 1) % max(1, args.n // 20) == 0:
            elapsed = time.time() - t_start
            eta = elapsed * (args.n - i - 1) / (i + 1)
            print(f"  [{i+1}/{args.n}] {r.final_phase:10s} "
                  f"V_body={r.V_body_L:.2f}L  "
                  f"({elapsed:.0f}s elapsed, ETA {eta:.0f}s)")

    print()
    summary = aggregate(results)
    print("=== Summary ===")
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"  {k:30s} {v:.2f}")
        else:
            print(f"  {k:30s} {v}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({
                "summary": summary,
                "results": [asdict(r) for r in results],
            }, f, indent=2, default=str)
        print(f"\nFull results → {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
