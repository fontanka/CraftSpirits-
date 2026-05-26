"""
sim/tui.py — terminal UI клиент для sim сервера.

Использует stdlib curses + urllib для HTTP (без deps). Запуск:

    # 1. Запусти server.py в одном терминале:
    python server.py
    # 2. В другом — TUI:
    python tui.py [--host localhost --port 8000]

Управление:
- s     — start session (с текущими config)
- p     — pause/resume
- 1-5   — speed 1×/10×/100×/1000×/max
- a     — acknowledge emergency
- r     — reset
- f     — open fault menu
- q     — quit

Подходит для headless-сред где браузера нет.
"""
from __future__ import annotations

import argparse
import curses
import json
import time
import urllib.error
import urllib.request


def http_get(host: str, port: int, path: str) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}{path}", timeout=2
        ) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None


def http_post(host: str, port: int, path: str, body: dict) -> dict | None:
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None


PHASE_COLORS = {
    "IDLE": 8, "INIT": 8, "PRE_FLIGHT": 8,
    "HEAT_UP": 3, "STABILIZE": 3, "STABILIZE2": 3,
    "HEADS": 4, "BODY": 2, "TAILS": 6,
    "SHUTDOWN": 5, "DONE": 5, "CLOSED": 5,
    "EMERGENCY": 1, "PAUSE": 8,
}


def fmt_time(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.1f}min"
    return f"{s/3600:.1f}h"


def draw(stdscr, snap: dict | None, host: str, port: int, status: str = ""):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 24 or w < 80:
        stdscr.addstr(0, 0, f"Terminal too small ({w}×{h}); need ≥80×24")
        stdscr.refresh()
        return

    # Header
    title = f" CraftSpirits Sim v2 — {host}:{port} "
    stdscr.attron(curses.A_REVERSE)
    stdscr.addstr(0, 0, title.ljust(w - 1))
    stdscr.attroff(curses.A_REVERSE)
    stdscr.addstr(0, w - 25, status[:24].rjust(24))

    if snap is None:
        stdscr.addstr(2, 2, "[ no connection to server ]", curses.color_pair(1))
        stdscr.refresh()
        return

    phase = (snap.get("controller", {}) or {}).get("phase", "IDLE")
    color = curses.color_pair(PHASE_COLORS.get(phase, 8))
    stdscr.addstr(2, 2, f"Phase: ")
    stdscr.attron(color | curses.A_BOLD)
    stdscr.addstr(f" {phase} ")
    stdscr.attroff(color | curses.A_BOLD)

    speed = snap.get("speed", 1)
    paused = snap.get("paused", False)
    t_sim = snap.get("t_sim_s", 0)
    stdscr.addstr(2, 30, f"t_sim={fmt_time(t_sim):<8}")
    stdscr.addstr(2, 48, f"speed={'MAX' if speed == 'max' else f'{speed}×':<8}")
    stdscr.addstr(2, 64, f"[{'PAUSED' if paused else 'running'}]")

    if not snap.get("ready"):
        stdscr.addstr(4, 2, "Session not started. Press 's' to start.")
        stdscr.addstr(h - 2, 2,
                      "[s]tart [p]ause [1-5]speed [a]ck [r]eset [f]ault [q]uit")
        stdscr.refresh()
        return

    # ---- left column: sensors + kub ----
    y = 4
    stdscr.addstr(y, 2, "── SENSORS ─────────────────"); y += 1
    p = snap["physical"]
    for k, v, unit in [
        ("T_kub", p["T_kub"], "°C"),
        ("T_head", p["T_head"], "°C"),
        ("T_water_in", p["T_water_in"], "°C"),
        ("T_water_out", p["T_water_out"], "°C"),
        ("P_heater", p["P_heater_kW"], "kW"),
        ("P_atm", p["P_atm_hPa"], "hPa"),
        ("m_dot vapor", p["m_dot_vapor_gps"], "g/s"),
    ]:
        try:
            v_str = f"{v:7.2f}"
        except (TypeError, ValueError):
            v_str = "    nan"
        stdscr.addstr(y, 2, f"  {k:<14}{v_str} {unit}")
        y += 1

    y += 1
    stdscr.addstr(y, 2, "── KUB ─────────────────────"); y += 1
    stdscr.addstr(y, 2, f"  V       {p['V_kub_L']:7.2f} L"); y += 1
    stdscr.addstr(y, 2, f"  ABV     {p['x_kub_abv']:7.2f} %"); y += 1
    stdscr.addstr(y, 2, f"  boiling {'yes' if p['is_boiling'] else 'no'}"); y += 1
    tags = []
    if p.get("flooded"): tags.append("FLOOD")
    if p.get("weeping"): tags.append("WEEP")
    if p.get("dry_out"): tags.append("DRY")
    if p.get("foam"): tags.append("FOAM")
    if tags:
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(y, 2, "  " + " ".join(tags))
        stdscr.attroff(curses.color_pair(1))

    # ---- middle column: phase + controller + product ----
    mx = 36
    y = 4
    c = snap["controller"]
    stdscr.addstr(y, mx, "── CONTROLLER ──────────────"); y += 1
    stdscr.addstr(y, mx, f"  phase_elapsed {fmt_time(c['phase_elapsed_s'])}"); y += 1
    stdscr.addstr(y, mx, f"  duty          {c['duty_current']:.2f}"); y += 1
    stdscr.addstr(y, mx, f"  n_alerts      {c['n_alerts']}"); y += 1
    if c.get("suspect_sensor"):
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(y, mx, f"  SUSPECT: {c['suspect_sensor']} (×{c['suspect_ratio']:.1f})")
        stdscr.attroff(curses.color_pair(1))
        y += 1

    y += 1
    out = snap["outputs"]
    stdscr.addstr(y, mx, "── OUTPUTS ─────────────────"); y += 1
    bar = "█" * int(out["heater_pct"] / 5) + " " * (20 - int(out["heater_pct"] / 5))
    stdscr.addstr(y, mx, f"  heater   {out['heater_pct']:3d}% [{bar}]"); y += 1
    stdscr.addstr(y, mx,
                  f"  valve_takeoff {'OPEN' if out['valve_takeoff'] else 'closed'}"); y += 1
    stdscr.addstr(y, mx,
                  f"  valve_water   {'OPEN' if out['valve_water'] else 'closed'}"); y += 1
    stdscr.addstr(y, mx,
                  f"  contactor     {'ON' if out['contactor'] else 'off'}"); y += 1

    y += 1
    pr = snap["product"]
    stdscr.addstr(y, mx, "── PRODUCT ─────────────────"); y += 1
    stdscr.addstr(y, mx, f"  active = {pr['active']}"); y += 1
    stdscr.addstr(y, mx, f"  V_heads {pr['V_heads_L']*1000:7.0f} mL"); y += 1
    stdscr.addstr(y, mx, f"  V_body  {pr['V_body_L']:7.2f} L  @ {pr['x_product_abv']:5.1f}%"); y += 1
    stdscr.addstr(y, mx, f"  V_tails {pr['V_tails_L']:7.2f} L"); y += 1

    # ---- right column: hardware + alerts ----
    rx = 70
    y = 4
    h_state = snap["hardware"]
    if h_state.get("enabled"):
        stdscr.addstr(y, rx, "── HARDWARE ──────"); y += 1
        for k, v, unit in [
            ("SSR T_j", h_state.get("ssr_T_j"), "°C"),
            ("Valve Cv", h_state.get("valve_Cv"), ""),
            ("Valve T_coil", h_state.get("valve_T_coil"), "°C"),
            ("Stiction", h_state.get("valve_stiction"), "N"),
            ("CT wear", h_state.get("contactor_wear"), ""),
            ("DS head CRC", h_state.get("ds_T_head_crc"), ""),
            ("DS kub CRC", h_state.get("ds_T_kub_crc"), ""),
        ]:
            try:
                vs = f"{v:6.2f}" if isinstance(v, float) else f"{v:>6}"
            except (TypeError, ValueError):
                vs = "    —"
            stdscr.addstr(y, rx, f"  {k:<12}{vs} {unit}")
            y += 1
        if h_state.get("ssr_fail_short"):
            stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
            stdscr.addstr(y, rx, "  SSR FAIL_SHORT")
            stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)
            y += 1

    y += 1
    stdscr.addstr(y, rx, "── ALERTS ────────"); y += 1
    alerts = c.get("alerts", [])[-(h - y - 4):]
    for a in alerts[-15:]:
        truncated = a[:max(0, w - rx - 4)]
        try:
            stdscr.attron(curses.color_pair(3))
            stdscr.addstr(y, rx, "  " + truncated)
            stdscr.attroff(curses.color_pair(3))
        except curses.error:
            break
        y += 1

    # Footer
    stdscr.addstr(h - 2, 2,
                  "[s]tart [p]ause [1-5]speed [a]ck [r]eset [f]ault [q]uit")
    stdscr.refresh()


def fault_menu(stdscr, host, port):
    """Show fault sub-menu, return choice or None."""
    faults = [
        ("water_cutoff", None, "cooling water cut"),
        ("cooling_water_hot", 25, "hot inlet 25°C"),
        ("estop", None, "E-stop"),
        ("bimetal_tripped", None, "bimetal trip"),
        ("pi_disconnected", True, "Pi disconnect"),
        ("pressure_drift", None, "atm pressure drift"),
        ("ssr_fail_short", None, "SSR пробой (hw)"),
        ("valve_dribble", 2.0, "valve leak 2 mL/s (hw)"),
        ("valve_T_coil_hot", 90, "valve hot 90°C (hw)"),
        ("sensor_T_kub_disconnect", None, "DS18B20 T_kub off (hw)"),
        ("under_fermented", None, "under-fermented CO2"),
        ("clear", None, "clear soft faults"),
    ]
    sel = 0
    while True:
        stdscr.erase()
        stdscr.addstr(0, 2, "── Fault injection ─ (↑↓ navigate, Enter select, Esc cancel) ──",
                      curses.A_BOLD)
        for i, (k, v, desc) in enumerate(faults):
            y = 2 + i
            line = f"  {desc}" + (f"  ({v})" if v is not None else "")
            if i == sel:
                stdscr.attron(curses.A_REVERSE)
                stdscr.addstr(y, 2, line.ljust(60))
                stdscr.attroff(curses.A_REVERSE)
            else:
                stdscr.addstr(y, 2, line)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(faults)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(faults)
        elif ch in (10, 13):
            k, v, _ = faults[sel]
            http_post(host, port, "/api/fault", {"key": k, "value": v})
            return
        elif ch in (27, ord("q")):
            return


def main_curses(stdscr, args):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_BLUE, -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    curses.init_pair(6, curses.COLOR_CYAN, -1)
    curses.init_pair(7, curses.COLOR_WHITE, -1)
    curses.init_pair(8, 8, -1)  # gray

    stdscr.nodelay(True)
    stdscr.timeout(200)

    status = ""
    last_fetch = 0
    snap = None
    while True:
        now = time.time()
        if now - last_fetch > 0.4:
            new_snap = http_get(args.host, args.port, "/api/snapshot")
            if new_snap is not None:
                snap = new_snap
            last_fetch = now
        draw(stdscr, snap, args.host, args.port, status)
        status = ""

        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch == ord("q"):
            break
        elif ch == ord("s"):
            http_post(args.host, args.port, "/api/start",
                      {"mode": "REFLUX", "V_kub": 18, "x_kub_abv": 12,
                       "mash_type": "grain"})
            status = "started"
        elif ch == ord("p"):
            http_post(args.host, args.port, "/api/pause", {})
            status = "toggled pause"
        elif ch == ord("a"):
            http_post(args.host, args.port, "/api/ack", {})
            status = "ACK emergency"
        elif ch == ord("r"):
            http_post(args.host, args.port, "/api/reset", {})
            status = "reset"
        elif ch == ord("f"):
            fault_menu(stdscr, args.host, args.port)
        elif ch == ord("1"):
            http_post(args.host, args.port, "/api/speed", {"speed": 1})
        elif ch == ord("2"):
            http_post(args.host, args.port, "/api/speed", {"speed": 10})
        elif ch == ord("3"):
            http_post(args.host, args.port, "/api/speed", {"speed": 100})
        elif ch == ord("4"):
            http_post(args.host, args.port, "/api/speed", {"speed": 1000})
        elif ch == ord("5"):
            http_post(args.host, args.port, "/api/speed", {"speed": "max"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    curses.wrapper(main_curses, args)


if __name__ == "__main__":
    main()
