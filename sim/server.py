"""
sim/server.py — FastAPI сервер для v2 sim.

Крутит физику + контроллер на background-таске, пушит state в UI через
WebSocket ~10 Hz, принимает команды (start/pause/speed/inject) по HTTP.

Использование:
    pip install -r requirements.txt  (fastapi, uvicorn, pydantic)
    python server.py [--port 8000]

UI:
- Browser: http://localhost:8000/
- TUI: python tui.py [--host localhost --port 8000]

Подходит для v2: boiler+column+condenser+hardware (15-var valves, DS18B20
с counterfeit, SSR thermal, contactor chatter). Поддерживает realtime
и fast-forward (1× / 10× / 100× / max).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from controller import Controller, Mode, Phase, Recipe
from hardware import CounterfeitFamily, CoilClass
from physics import Still


class SimEngine:
    """Background sim engine. Один Still + Controller, управляется через
    play/pause/speed/inject."""

    def __init__(self):
        self.still: Still | None = None
        self.controller: Controller | None = None
        self.speed: float = 10.0  # 10× default — иначе скучно ждать
        self.max_speed: bool = False
        self.paused: bool = True  # стартует на паузе до /api/start
        self.pi_alive: bool = True
        self.history: list[dict] = []
        self.history_max = 1800  # ~30 min @ 1Hz dump
        self._last_history_t: float = -1e9
        self._cfg = {
            "V_kub": 18.0, "x_kub_abv": 12.0, "mash_type": "grain",
            "viscosity": 1.0, "sugar_g_L": 0.0,
            "oborotniy_V_L": 0.0, "oborotniy_abv": 0.0,
            "column_D_m": 0.040,
            # Stage 10: configurable hardware
            "heater_kW": 5.0,        # 1.5 для ХД-4 500 setup
            "column_type": "packed", # 'packed' | 'bubble_cap'
            "n_plates": 4,           # для bubble_cap
            "column_H_m": 1.0,       # 0.5 для ХД-4 500
            "takeoff_mode": "pwm",   # 'pwm' | 'smooth' (stage 11)
        }
        self._hw_opts: dict = {}
        self._recipe = Recipe()
        self._alerts_seen_count = 0

    # ---- lifecycle ----

    def configure(self, **cfg):
        for k, v in cfg.items():
            if k in self._cfg:
                self._cfg[k] = v

    def configure_hw(self, **opts):
        self._hw_opts = dict(opts) if opts else {}

    def new_session(self, mode: str = "REFLUX"):
        """Создать новый Still + Controller с текущим _cfg и _hw_opts."""
        self.still = Still(
            column_diameter_m=self._cfg["column_D_m"],
            heater_kW=self._cfg.get("heater_kW", 5.0),
            column_type=self._cfg.get("column_type", "packed"),
            n_plates=int(self._cfg.get("n_plates", 4)),
            column_H_m=self._cfg.get("column_H_m", 1.0),
        )
        self.still.set_initial(
            V_L=self._cfg["V_kub"],
            abv_vol=self._cfg["x_kub_abv"],
            viscosity=self._cfg["viscosity"],
            sugar_g_L=self._cfg["sugar_g_L"],
            mash_type=self._cfg["mash_type"],
            oborotniy_V_L=self._cfg["oborotniy_V_L"],
            oborotniy_abv=self._cfg["oborotniy_abv"],
        )
        if self._hw_opts:
            kwargs = {}
            for k, v in self._hw_opts.items():
                if k.startswith("ds_family_") and isinstance(v, str):
                    try:
                        kwargs[k] = CounterfeitFamily(v)
                    except ValueError:
                        kwargs[k] = CounterfeitFamily.ORIGINAL
                elif k == "valve_takeoff_class" and isinstance(v, str):
                    try:
                        kwargs[k] = CoilClass(v)
                    except ValueError:
                        pass
                else:
                    kwargs[k] = v
            self.still.enable_realistic_hardware(**kwargs)

        self._recipe = Recipe(mode=Mode(mode))
        # Sync heater rating into Recipe so PZEM-style cmd-vs-meas check
        # uses correct max power
        self._recipe.heater_max_kW = self._cfg.get("heater_kW", 5.0)
        # Stage 11: takeoff mode (pwm | smooth)
        self._recipe.takeoff_mode = self._cfg.get("takeoff_mode", "pwm")
        self.controller = Controller(self._recipe)
        self.controller.start(0.0, initial_sensors=self.still.read_sensors())
        self.history.clear()
        self._last_history_t = -1e9
        self._alerts_seen_count = 0
        self.paused = False

    def reset(self):
        self.still = None
        self.controller = None
        self.history.clear()
        self.paused = True

    def step(self, dt: float):
        if self.paused or self.still is None or self.controller is None:
            return
        sensors = self.still.read_sensors()
        outs = self.controller.tick(self.still.t_sim_s, sensors,
                                    pi_alive=self.pi_alive)
        self.still.apply_outputs(outs)
        self.still.step(dt)

        # History dump каждые 5 sim-сек
        if self.still.t_sim_s - self._last_history_t >= 5:
            self._last_history_t = self.still.t_sim_s
            o = self.still.observables()
            r = self.still.read_sensors()
            self.history.append({
                "t": self.still.t_sim_s,
                "T_kub": o.T_kub_bulk_C,
                "T_head": o.T_head_C,
                "T_water_out": o.T_water_out_C,
                "P_kW": o.P_heater_kW,
                "V_body_L": self.still.V_body_L,
                "V_heads_L": self.still.V_heads_L,
                "phase": self.controller.st.phase.value,
                "ssr_T_j": r.get("ssr_T_junction_C"),
                "valve_Cv_ratio": (
                    self.still.valve_takeoff_hw.s.Cv_effective /
                    max(self.still.valve_takeoff_hw.Cv_nominal, 0.01)
                    if self.still.realistic_hw_enabled else None
                ),
            })
            if len(self.history) > self.history_max:
                self.history = self.history[-self.history_max:]

    def snapshot(self) -> dict:
        if self.still is None or self.controller is None:
            return {
                "ready": False, "paused": True,
                "speed": "max" if self.max_speed else self.speed,
                "config": self._cfg, "hw_options": self._hw_opts,
                "history": [],
            }
        s = self.still
        ctrl = self.controller
        o = s.observables()
        r = s.read_sensors()
        return {
            "ready": True,
            "paused": self.paused,
            "pi_alive": self.pi_alive,
            "speed": "max" if self.max_speed else self.speed,
            "t_sim_s": s.t_sim_s,
            "config": self._cfg,
            "hw_options": self._hw_opts,
            "physical": {
                "T_kub": o.T_kub_bulk_C,
                "T_kub_wall": o.T_kub_wall_C,
                "T_head": o.T_head_C,
                "T_atm_tube": o.T_atm_tube_C,
                "T_water_in": o.T_water_in_C,
                "T_water_out": o.T_water_out_C,
                "T_water_after_product": s.condenser.s.T_water_after_product_C,
                "main_bypass_closed": s.condenser.s.main_bypass_closed,
                "Q_main_W": s.condenser.s.Q_to_water_W,
                "Q_product_W": s.condenser.s.Q_to_water_product_W,
                "column_type": s.column.p.column_type,
                "n_plates": s.column.p.n_plates,
                "N_eff": s.column.s.N_eff,
                "Cv": s.column.s.Cv,
                "delta_P_Pa": s.column.s.delta_P_Pa,
                "V_kub_L": o.V_kub_L,
                "x_kub_abv": o.x_kub_abv,
                "x_head_abv": o.x_head_abv,
                "x_product_abv": o.x_product_abv,
                "is_boiling": o.is_boiling,
                "P_heater_kW": o.P_heater_kW,
                "m_dot_vapor_gps": o.m_dot_vapor_g_s,
                "P_atm_hPa": o.P_atm_hPa,
                "flooded": o.flooded,
                "weeping": o.weeping,
                "dry_out": o.dry_out,
                "foam": o.foam_active,
            },
            "product": {
                "V_heads_L": s.V_heads_L,
                "V_body_L": s.V_body_L,
                "V_tails_L": s.V_tails_L,
                "active": o.active_receiver,
                "x_head_abv": o.x_head_abv,
                "x_product_abv": o.x_product_abv,
            },
            "outputs": {
                "heater_pct": int(s.heater_power * 100),
                "valve_takeoff": s.valve_takeoff_open,
                "valve_water": s.valve_water_open,
                "contactor": s.contactor_enable,
            },
            "controller": {
                "phase": ctrl.st.phase.value,
                "phase_elapsed_s": s.t_sim_s - ctrl.st.phase_started_at,
                "duty_current": ctrl.duty_current,
                "alerts": ctrl.st.alerts[-12:],
                "n_alerts": len(ctrl.st.alerts),
                "last_alert": ctrl.st.last_alert,
                "suspect_sensor": ctrl.suspect_sensor,
                "suspect_ratio": ctrl.suspect_analyzer.last_ratio,
            },
            "hardware": {
                "enabled": s.realistic_hw_enabled,
                "ssr_T_j": r.get("ssr_T_junction_C"),
                "ssr_fail_short": r.get("ssr_fail_short"),
                "valve_Cv": r.get("valve_takeoff_Cv"),
                "valve_T_coil": r.get("valve_takeoff_T_coil"),
                "valve_stiction": r.get("valve_takeoff_stiction_N"),
                "contactor_chatter": r.get("contactor_chatter"),
                "contactor_wear": r.get("contactor_wear"),
                "ds_T_head_crc": r.get("ds_T_head_crc_fails"),
                "ds_T_head_sent": r.get("ds_T_head_sentinels"),
                "ds_T_kub_crc": r.get("ds_T_kub_crc_fails"),
                "ds_T_kub_sent": r.get("ds_T_kub_sentinels"),
            },
            "faults": {
                "pressure_drift": s.faults.pressure_drift,
                "water_cutoff": s.faults.water_cutoff,
                "cooling_water_hot": s.faults.cooling_water_hot,
                "estop": s.faults.estop_pressed,
                "bimetal": s.faults.bimetal_tripped,
                "under_fermented": s.faults.under_fermented,
                "pi_disconnected": not self.pi_alive,
            },
            "history": self.history[-600:],
        }


engine = SimEngine()


async def physics_loop():
    """Background asyncio task. Один tick = sim_dt sim-секунд физики +
    sleep wall_clock based on speed."""
    SUB_DT = 1.0  # sim seconds per substep — устойчиво для нашей физики
    while True:
        if engine.paused or not engine.still:
            await asyncio.sleep(0.05)
            continue
        if engine.max_speed:
            # Жарим без сна — 50 substeps за turn для batch processing
            for _ in range(50):
                engine.step(SUB_DT)
            await asyncio.sleep(0)  # отдать loop control
        else:
            # 1 sim-sec в 1/speed wall-sec
            engine.step(SUB_DT)
            await asyncio.sleep(SUB_DT / max(engine.speed, 0.1))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(physics_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


# ---- HTTP API ----

class StartReq(BaseModel):
    mode: str = "REFLUX"
    V_kub: float | None = None
    x_kub_abv: float | None = None
    mash_type: str | None = None
    viscosity: float | None = None
    sugar_g_L: float | None = None
    oborotniy_V_L: float | None = None
    oborotniy_abv: float | None = None
    column_D_m: float | None = None
    # Stage 10: hardware spec (ХД-4 500: heater_kW=1.5, bubble_cap, 4 plates, 0.5m)
    heater_kW: float | None = None
    column_type: str | None = None
    n_plates: int | None = None
    column_H_m: float | None = None
    takeoff_mode: str | None = None
    hw: dict | None = None


@app.post("/api/start")
async def start(req: StartReq):
    cfg = {k: v for k, v in req.dict().items() if v is not None and k in engine._cfg}
    engine.configure(**cfg)
    if req.hw is not None:
        engine.configure_hw(**req.hw)
    engine.new_session(req.mode)
    return {"ok": True}


@app.post("/api/pause")
async def pause():
    engine.paused = not engine.paused
    return {"ok": True, "paused": engine.paused}


@app.post("/api/stop")
async def stop():
    if engine.controller:
        engine.controller.request_stop(
            engine.still.t_sim_s if engine.still else 0
        )
    return {"ok": True}


@app.post("/api/reset")
async def reset():
    engine.reset()
    return {"ok": True}


@app.post("/api/ack")
async def ack():
    if engine.controller and engine.still:
        engine.controller.acknowledge_emergency()
    return {"ok": True}


class SpeedReq(BaseModel):
    speed: float | str  # "max" или число


@app.post("/api/speed")
async def set_speed(req: SpeedReq):
    if isinstance(req.speed, str) and req.speed.lower() == "max":
        engine.max_speed = True
    else:
        try:
            v = float(req.speed)
            engine.max_speed = False
            engine.speed = max(0.1, min(10000.0, v))
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad speed"}
    return {"ok": True, "speed": "max" if engine.max_speed else engine.speed}


class FaultReq(BaseModel):
    key: str
    value: float | bool | str | None = None


@app.post("/api/fault")
async def inject_fault(req: FaultReq):
    if not engine.still:
        return {"ok": False, "error": "no session"}
    f = engine.still.faults
    k = req.key
    v = req.value
    if k == "pi_disconnected":
        engine.pi_alive = not bool(v) if v is not None else False
        return {"ok": True}
    if k == "water_cutoff":
        f.water_cutoff = bool(v) if v is not None else True
    elif k == "bimetal_tripped":
        f.bimetal_tripped = True
    elif k == "estop":
        f.estop_pressed = True
    elif k == "pressure_drift":
        f.pressure_drift = bool(v) if v is not None else True
    elif k == "cooling_water_hot":
        f.cooling_water_hot = float(v) if v is not None else 25.0
    elif k == "under_fermented":
        f.under_fermented = True
    elif k == "valve_dribble":
        if engine.still.realistic_hw_enabled:
            engine.still.valve_takeoff_hw.s.leak_rate_ml_s_when_closed = float(v or 1.0)
            engine.still.valve_takeoff_hw.s.seat_debris = True
    elif k == "ssr_fail_short":
        if engine.still.realistic_hw_enabled:
            engine.still.ssr_heater.s.fail_short = True
    elif k == "valve_T_coil_hot":
        if engine.still.realistic_hw_enabled:
            engine.still.valve_takeoff_hw.s.T_coil_C = float(v or 85)
    elif k == "V_mains":
        if engine.still.realistic_hw_enabled:
            engine.still.V_mains = float(v or 230)
    elif k == "sensor_T_kub_disconnect":
        if engine.still.realistic_hw_enabled:
            engine.still.ds18b20_T_kub.disconnect()
    elif k == "main_bypass":
        # Stage 10: cut water flow to main reflux condenser
        # (для ХД setup — bypass mode)
        engine.still.condenser.s.main_bypass_closed = bool(v) if v is not None else True
    elif k == "clear":
        # Сбросить inject-able faults (не all — некоторые latch)
        f.water_cutoff = False
        f.cooling_water_hot = None
        f.pressure_drift = False
        engine.pi_alive = True
    else:
        return {"ok": False, "error": f"unknown fault {k}"}
    return {"ok": True}


@app.get("/api/snapshot")
async def get_snapshot():
    return engine.snapshot()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            snap = engine.snapshot()
            await websocket.send_json(snap)
            await asyncio.sleep(0.2)  # 5Hz UI update
    except WebSocketDisconnect:
        return


# ---- static files ----

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    import uvicorn
    uvicorn.run("server:app", host=args.host, port=args.port,
                reload=False, log_level="warning")


if __name__ == "__main__":
    main()
