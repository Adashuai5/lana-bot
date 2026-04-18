from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flask import Flask, Response, jsonify, render_template, request
from lana_bot.data.binance_futures import fetch_mark_price
from lana_bot.risk.stop_loss import unrealized_pnl_usdt
from lana_bot.state.positions import list_positions

app = Flask(__name__, template_folder=str(ROOT / "templates"))

JOURNAL     = ROOT / "data" / "journal.ndjson"
CANDIDATES  = ROOT / "data" / "candidates" / "latest.json"
LOG_FILE    = ROOT / "logs" / "cycle.log"
CYCLE_LABEL    = "com.lanabot.cycle"
FASTSCAN_LABEL = "com.lanabot.fastscan"
MONITOR_LABEL  = "com.lanabot.monitor"
PLIST_CYCLE    = Path.home() / "Library/LaunchAgents/com.lanabot.cycle.plist"
PLIST_FASTSCAN = Path.home() / "Library/LaunchAgents/com.lanabot.fastscan.plist"
PLIST_MONITOR  = Path.home() / "Library/LaunchAgents/com.lanabot.monitor.plist"


def _bot_running() -> bool:
    return subprocess.run(["launchctl", "list", CYCLE_LABEL],
                          capture_output=True).returncode == 0


@app.get("/")
def index():
    return render_template("dashboard.html")


@app.get("/api/status")
def api_status():
    return jsonify({"running": _bot_running(), "ts": int(time.time())})


@app.get("/api/positions")
def api_positions():
    result = []
    for pos in list_positions():
        d = asdict(pos)
        try:
            mark = fetch_mark_price(pos.symbol)
            d["mark_price"] = mark
            d["unrealized_pnl_usdt"] = round(unrealized_pnl_usdt(pos, mark), 2)
            d["pnl_pct"] = round((mark - pos.entry_price) / pos.entry_price * 100 * pos.leverage, 2)
        except Exception:
            d.update(mark_price=None, unrealized_pnl_usdt=None, pnl_pct=None)
        result.append(d)
    return jsonify(result)


@app.get("/api/candidates")
def api_candidates():
    if not CANDIDATES.exists():
        return jsonify({"candidates": [], "generated_at_ms": None})
    return jsonify(json.loads(CANDIDATES.read_text()))


@app.get("/api/journal")
def api_journal():
    if not JOURNAL.exists():
        return jsonify([])
    n = min(int(request.args.get("n", 20)), 500)
    lines = JOURNAL.read_text().strip().splitlines()
    return jsonify([json.loads(l) for l in reversed(lines[-n:])])


@app.get("/api/fast-scan")
def api_fast_scan():
    state_file = ROOT / "data" / "fast_scan_state.json"
    if not state_file.exists():
        return jsonify({"enabled": False})
    try:
        s = json.loads(state_file.read_text())
        s["enabled"] = True
        return jsonify(s)
    except Exception:
        return jsonify({"enabled": False})


@app.get("/api/square-status")
def api_square_status():
    from lana_bot.data.binance_square import get_square_status
    return jsonify(get_square_status())


@app.get("/api/exit-stats")
def api_exit_stats():
    stats = {"hard_sl": 0, "trailing_tp": 0, "time_stop": 0, "signal_decay": 0, "total_closes": 0}
    if not JOURNAL.exists():
        return jsonify(stats)
    for line in JOURNAL.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("event") != "close":
            continue
        stats["total_closes"] += 1
        trigger = rec.get("exit_trigger", "signal_decay")
        if trigger in stats:
            stats[trigger] += 1
    return jsonify(stats)


@app.post("/api/bot/collect")
def bot_collect():
    import threading
    def run():
        subprocess.run(["uv", "run", "python", "scripts/collect.py"],
                       capture_output=True, cwd=str(ROOT))
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/bot/cycle")
def bot_cycle():
    import threading
    def run():
        subprocess.run(["bash", "scripts/cycle.sh"],
                       capture_output=True, cwd=str(ROOT))
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/positions/close")
def api_close_position():
    from lana_bot.execution import get_client
    symbol = (request.json or {}).get("symbol")
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400
    try:
        client = get_client()
        client.close(symbol)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/bot/start")
def bot_start():
    errors = []
    for plist in [PLIST_CYCLE, PLIST_FASTSCAN, PLIST_MONITOR]:
        if not plist.exists():
            errors.append(f"{plist.name} not found")
            continue
        r = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
        if r.returncode != 0:
            errors.append(r.stderr.strip())
    return jsonify({"ok": len(errors) == 0, "errors": errors})


@app.post("/api/bot/stop")
def bot_stop():
    errors = []
    for plist in [PLIST_CYCLE, PLIST_FASTSCAN, PLIST_MONITOR]:
        if not plist.exists():
            continue
        r = subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, text=True)
        if r.returncode != 0:
            errors.append(r.stderr.strip())
    return jsonify({"ok": True, "errors": errors})


@app.get("/stream/logs")
def stream_logs():
    def generate():
        proc = subprocess.Popen(
            ["tail", "-n", "50", "-f", str(LOG_FILE)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        try:
            for line in proc.stdout:
                yield f"data: {line.rstrip()}\n\n"
        finally:
            proc.terminate()
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
