"""
FastAPI сервер: крутит физику + контроллер на background-таске,
пушит состояние в UI по WebSocket ~10 Hz, принимает команды (старт/пауза/
скорость/fault-injection) по HTTP.
"""
from __future__ import annotations

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
from still import FaultInjection, Outputs, Still, StillState


class SimEngine:
    def __init__(self):
        self.still = Still()
        self.controller = Controller()
        self.speed = 10.0  # 10x по умолчанию — иначе скучно
        self.paused = False
        self.pi_alive = True  # имитация связи Pi ↔ ESP
        self.history: list[dict] = []  # для графиков (T_kub, T_head, time)
        self.history_max = 1800  # 1800 точек

    def reset(self):
        self.still.reset()
        self.controller = Controller()
        self.history.clear()
        self.paused = False
        self.pi_alive = True

    def start_session(self, recipe: Recipe):
        self.controller.start(self.still.t_sim_s, recipe)

    def step(self, dt: float):
        if self.paused:
            return
        sensors = self.still.read_sensors()
        outs = self.controller.tick(self.still.t_sim_s, sensors, pi_alive=self.pi_alive)
        self.still.apply_outputs(outs)
        self.still.step(dt)

        # История для графика (раз в ~5 sim-секунд)
        if not self.history or self.still.t_sim_s - self.history[-1]["t"] > 5:
            self.history.append({
                "t": self.still.t_sim_s,
                "T_kub": self.still.s.T_kub,
                "T_head": self.still.s.T_head,
                "T_water_out": self.still.s.T_water_out,
                "duty": self.still.s.duty_avg,
                "P_kW": self.still.s.P_heater_kW,
                "phase": self.controller.st.phase.value,
            })
            if len(self.history) > self.history_max:
                self.history = self.history[-self.history_max:]

    def snapshot(self) -> dict:
        s = self.still.s
        sensors = self.still.read_sensors()
        ctrl = self.controller
        return {
            "t_sim_s": self.still.t_sim_s,
            "speed": self.speed,
            "paused": self.paused,
            "pi_alive": self.pi_alive,
            # физика
            "physical": {
                "T_kub": s.T_kub,
                "T_head": s.T_head,
                "T_water_in": s.T_water_in,
                "T_water_out": s.T_water_out,
                "V_kub_L": s.V_kub,
                "V_product_L": s.V_product,
                "x_kub_abv": sensors["x_kub_abv"],
                "x_head_abv": sensors["x_head_abv"],
                "x_product_abv": sensors["x_product_abv"],
                "is_boiling": s.is_boiling,
                "P_heater_kW": s.P_heater_kW,
                "P_to_vapor_kW": s.P_to_vapor_kW,
                "m_dot_vapor_gps": s.m_dot_vapor_gps,
                "duty_avg": s.duty_avg,
                "P_atm_hPa": s.P_atm_Pa / 100,
            },
            # выходы железа
            "outputs": {
                "heater_power": self.still.out.heater_power,
                "valve_takeoff": self.still.out.valve_takeoff,
                "valve_water": self.still.out.valve_water,
                "contactor_enable": self.still.out.contactor_enable,
            },
            # контроллер
            "controller": {
                "phase": ctrl.st.phase.value,
                "phase_elapsed_s": self.still.t_sim_s - ctrl.st.phase_started_at,
                "duty_current": ctrl.duty_current,
                "alerts": ctrl.st.alerts[-10:],  # последние 10
                "session_elapsed_s": (
                    self.still.t_sim_s - ctrl.session_started_at
                    if ctrl.session_started_at is not None
                    else 0
                ),
            },
            # неисправности
            "faults": {
                "sensor_t_kub_fail": self.still.fault.sensor_t_kub_fail,
                "sensor_t_head_fail": self.still.fault.sensor_t_head_fail,
                "sensor_t_water_out_fail": self.still.fault.sensor_t_water_out_fail,
                "valve_takeoff_stuck_open": self.still.fault.valve_takeoff_stuck_open,
                "valve_takeoff_stuck_closed": self.still.fault.valve_takeoff_stuck_closed,
                "ssr_heater_stuck_on": self.still.fault.ssr_heater_stuck_on,
                "water_cutoff": self.still.fault.water_cutoff,
                "estop_pressed": self.still.fault.estop_pressed,
                "bimetal_tripped": self.still.fault.bimetal_tripped,
                "pi_disconnected": not self.pi_alive,
            },
            # история для графика
            "history": self.history[-360:],  # последние 30 минут sim-time при 5с шаге
        }


engine = SimEngine()


async def physics_loop():
    """Крутит физику. dt sim = wall_dt * speed."""
    last_wall = time.monotonic()
    while True:
        now = time.monotonic()
        wall_dt = now - last_wall
        last_wall = now
        sim_dt = wall_dt * engine.speed
        # Несколько шагов с малым dt для устойчивости интегрирования
        n_substeps = max(1, int(sim_dt))
        sub_dt = sim_dt / n_substeps
        for _ in range(n_substeps):
            engine.step(sub_dt)
        await asyncio.sleep(0.1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(physics_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


class StartReq(BaseModel):
    mode: str = "REFLUX"
    V_kub: float = 18.0
    x_kub_abv: float = 12.0  # начальная крепость браги, об. %
    # Параметры рецепта — опционально override
    p_work: float | None = None
    duty_heads: float | None = None
    duty_body: float | None = None
    duty_tails: float | None = None
    pwm_period_s: float | None = None
    t_stable_s: int | None = None


def abv_vol_to_mass(abv_vol: float) -> float:
    """Об.% → массовая доля. Грубо."""
    v = abv_vol / 100.0
    rho_eth, rho_h2o = 0.789, 0.998
    m_eth = v * rho_eth
    m_h2o = (1 - v) * rho_h2o
    return m_eth / (m_eth + m_h2o)


@app.post("/api/start")
async def start(req: StartReq):
    engine.reset()
    engine.still.s.V_kub = req.V_kub
    engine.still.s.x_kub_mass = abv_vol_to_mass(req.x_kub_abv)
    # Стартовая T куба — холодная
    engine.still.s.T_kub = 22.0
    engine.still.s.T_head = 22.0
    engine.still.s.T_water_out = engine.still.s.T_water_in

    recipe = Recipe(mode=Mode(req.mode))
    if req.p_work is not None:
        recipe.p_work = req.p_work
    if req.duty_heads is not None:
        recipe.duty_heads = req.duty_heads
    if req.duty_body is not None:
        recipe.duty_body = req.duty_body
    if req.duty_tails is not None:
        recipe.duty_tails = req.duty_tails
    if req.pwm_period_s is not None:
        recipe.pwm_period_s = req.pwm_period_s
    if req.t_stable_s is not None:
        recipe.t_stable_s = req.t_stable_s

    engine.start_session(recipe)
    return {"ok": True}


@app.post("/api/stop")
async def stop():
    engine.controller.request_stop(engine.still.t_sim_s)
    return {"ok": True}


@app.post("/api/ack")
async def ack_emergency():
    engine.controller.acknowledge_emergency()
    return {"ok": True}


@app.post("/api/reset")
async def reset():
    engine.reset()
    return {"ok": True}


@app.post("/api/pause")
async def pause():
    engine.paused = not engine.paused
    return {"ok": True, "paused": engine.paused}


class SpeedReq(BaseModel):
    speed: float


@app.post("/api/speed")
async def set_speed(req: SpeedReq):
    engine.speed = max(0.1, min(1000.0, req.speed))
    return {"ok": True, "speed": engine.speed}


class FaultReq(BaseModel):
    key: str
    value: bool


@app.post("/api/fault")
async def set_fault(req: FaultReq):
    if req.key == "pi_disconnected":
        engine.pi_alive = not req.value
    elif hasattr(engine.still.fault, req.key):
        setattr(engine.still.fault, req.key, req.value)
    else:
        return {"ok": False, "error": f"unknown fault {req.key}"}
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            snap = engine.snapshot()
            await websocket.send_json(snap)
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        return


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
