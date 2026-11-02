"""Web Dashboard Server for PolyHFT Trading Bot"""

import os
import signal
import subprocess
import time
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
STATE_FILE = BASE_DIR / "bot_state.json"
PID_FILE = BASE_DIR / "bot.pid"
LOGS_DIR = BASE_DIR / "logs"
TRADE_LOG = LOGS_DIR / "trades.log"
BOT_LOG = LOGS_DIR / "bot.log"
TEMPLATES_DIR = BASE_DIR / "dashboard" / "templates"

app = FastAPI(title="PolyHFT Dashboard", version="1.0.0")


def _check_same_origin(request: Request) -> None:
    """Reject cross-origin writes (CSRF): browser fetches from other sites
    always carry an Origin header; same-origin or CLI requests pass."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("host", "")
    if not host or host not in origin:
        raise HTTPException(status_code=403, detail="Cross-origin requests are not allowed")

# Bot process management
bot_process: Optional[subprocess.Popen] = None
bot_start_time: Optional[float] = None


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists (signal 0 probe)"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def is_bot_running() -> bool:
    """Check if bot process is active (our tracked process or PID file)"""
    global bot_process
    if bot_process is not None:
        if bot_process.poll() is None:
            return True
        bot_process = None

    pid = get_bot_pid()
    return pid is not None


def get_bot_pid() -> Optional[int]:
    """Get active bot PID from our tracked process or the PID file.

    Never scans for foreign processes: a pgrep on 'python.*main.py' would
    match unrelated projects and stop_bot would kill them.
    """
    if bot_process is not None and bot_process.poll() is None:
        return bot_process.pid
    try:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            if _pid_alive(pid):
                return pid
            PID_FILE.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass
    return None


def load_config() -> Dict[str, Any]:
    """Load config from YAML"""
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: Dict[str, Any]) -> None:
    """Save config to YAML (backup + atomic replace)"""
    backup = CONFIG_PATH.with_suffix(".yaml.bak")
    try:
        if CONFIG_PATH.exists():
            backup.write_text(CONFIG_PATH.read_text())
        tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
        with open(tmp, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        tmp.replace(CONFIG_PATH)
    except Exception:
        if backup.exists():
            CONFIG_PATH.write_text(backup.read_text())
        raise


def load_state() -> Dict[str, Any]:
    """Load bot state from bot_state.json"""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}


@app.get("/", response_class=HTMLResponse)
def get_index():
    """Serve Dashboard HTML"""
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Dashboard template not found</h1>", status_code=404)
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/status")
def get_status():
    """Bot health and status"""
    running = is_bot_running()
    pid = get_bot_pid() if running else None
    cfg = load_config()

    uptime_sec = int(time.time() - bot_start_time) if (running and bot_start_time) else 0

    return {
        "status": "online" if running else "offline",
        "is_running": running,
        "pid": pid,
        "uptime_seconds": uptime_sec,
        "paper_trading": cfg.get("paper_trading", True),
        "websocket_enabled": cfg.get("websocket", {}).get("enabled", False),
        "initial_capital": cfg.get("risk", {}).get("initial_capital", 100.0),
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/metrics")
def get_metrics():
    """P&L and portfolio metrics"""
    state = load_state()
    cfg = load_config()
    initial_cap = float(cfg.get("risk", {}).get("initial_capital", 100.0))

    profitability = state.get("profitability", {})
    realized_pnl = float(
        state.get("positions", {}).get("closed_pnl", profitability.get("total_pnl", 0.0))
    )
    total_trades = int(profitability.get("total_trades", 0))

    # StateManager stores positions as {'open_positions': [...], ...}
    positions = state.get("positions", {})
    if isinstance(positions, dict):
        open_positions = positions.get("open_positions", [])
    else:
        open_positions = positions  # legacy/empty

    unrealized_pnl = 0.0
    total_exposure = 0.0

    for pos in open_positions:
        size = float(pos.get("size", 0.0))  # USD notional at entry
        total_exposure += size

    current_portfolio = initial_cap + realized_pnl + unrealized_pnl

    return {
        "initial_capital": initial_cap,
        "current_capital": round(current_portfolio, 2),
        "total_pnl": round(realized_pnl + unrealized_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl_pct": round(((realized_pnl + unrealized_pnl) / initial_cap) * 100, 2) if initial_cap > 0 else 0.0,
        "total_exposure": round(total_exposure, 2),
        "open_positions_count": len(open_positions),
        "total_trades": total_trades,
        "win_rate": round(float(profitability.get("win_rate", 0.0)), 1)
    }


@app.get("/api/positions")
def get_positions():
    """Active open positions"""
    state = load_state()
    positions = state.get("positions", {})
    if isinstance(positions, dict):
        return positions.get("open_positions", [])
    return positions or []


@app.get("/api/trades")
def get_recent_trades():
    """Recent trades from logs/trades.log or state"""
    trades = []
    if TRADE_LOG.exists():
        try:
            with open(TRADE_LOG, "r", encoding="utf-8") as f:
                lines = f.readlines()[-50:]  # last 50 lines
                for line in reversed(lines):
                    line = line.strip()
                    if line:
                        trades.append({"raw": line})
        except Exception:
            pass

    return {
        "trades": trades
    }


@app.get("/api/strategies")
def get_strategies():
    """List all strategies and their config"""
    cfg = load_config()
    strategies = cfg.get("strategies", {})
    results = []

    descriptions = {
        "single_arbitrage": "Arbitraje simple cuando YES + NO < $1.00",
        "market_making": "Market making continuo dinámico por bandas [BETA]",
        "spread_scalping": "Scalping rápido en mercados de alta liquidez",
        "tail_end": "Trading de alta probabilidad en etapas finales (<7 días)"
    }

    for name, data in strategies.items():
        if isinstance(data, dict):
            results.append({
                "id": name,
                "name": name.replace("_", " ").title(),
                "description": descriptions.get(name, "Estrategia algorítmica"),
                "enabled": data.get("enabled", False),
                "params": {k: v for k, v in data.items() if k not in ["enabled", "beta"]}
            })

    return results


@app.post("/api/strategies/toggle")
def toggle_strategy(request: Request, payload: Dict[str, Any]):
    """Toggle strategy enabled status (surgical YAML edit, keeps comments)"""
    _check_same_origin(request)
    strat_id = payload.get("id")
    enabled = payload.get("enabled")

    if not strat_id or enabled is None:
        raise HTTPException(status_code=400, detail="Missing id or enabled")
    if not strat_id.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid strategy id")

    cfg = load_config()
    strategies = cfg.get("strategies") or {}
    if strat_id not in strategies:
        raise HTTPException(status_code=404, detail="Strategy not found")

    new_value = "true" if bool(enabled) else "false"
    import re
    text = CONFIG_PATH.read_text()
    # Flip only the 'enabled:' line inside the strategy's own block
    pattern = re.compile(
        rf"(^[ \t]+{re.escape(strat_id)}:[ \t]*\n(?:[ \t]+[^\n]*\n)*?[ \t]+enabled:[ \t]*)\w+",
        re.MULTILINE,
    )
    updated, count = pattern.subn(rf"\g<1>{new_value}", text, count=1)
    if count == 0:
        raise HTTPException(status_code=404, detail="Strategy block not found in config")

    backup = CONFIG_PATH.with_suffix(".yaml.bak")
    backup.write_text(text)
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(updated)
    tmp.replace(CONFIG_PATH)
    return {"success": True, "id": strat_id, "enabled": bool(enabled)}


@app.post("/api/bot/start")
def start_bot(request: Request):
    _check_same_origin(request)
    """Start the bot process in paper mode"""
    global bot_process, bot_start_time

    if is_bot_running():
        return {"success": False, "message": "Bot already running"}

    python_bin = BASE_DIR / "venv" / "bin" / "python3"
    if not python_bin.exists():
        python_bin = "python3"

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = open(BOT_LOG, "a")

    try:
        bot_process = subprocess.Popen(
            [str(python_bin), "main.py", "--paper", "--log-level", "INFO"],
            cwd=str(BASE_DIR),
            stdout=out_file,
            stderr=out_file
        )
        bot_start_time = time.time()
        PID_FILE.write_text(str(bot_process.pid))
        return {"success": True, "pid": bot_process.pid, "message": "Bot started"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bot/stop")
def stop_bot(request: Request):
    _check_same_origin(request)
    """Stop the bot process cleanly"""
    global bot_process, bot_start_time

    pid = get_bot_pid()
    if not pid:
        bot_process = None
        bot_start_time = None
        return {"success": False, "message": "Bot is not running"}

    try:
        os.kill(pid, signal.SIGINT)
        time.sleep(1)
        # Check if still running, then SIGTERM
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

        bot_process = None
        bot_start_time = None
        PID_FILE.unlink(missing_ok=True)
        return {"success": True, "message": "Bot stopped cleanly"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs")
def get_logs(kind: str = "trades", limit: int = 100):
    """Get log lines"""
    limit = max(1, min(int(limit), 1000))
    target_file = TRADE_LOG if kind == "trades" else BOT_LOG
    if not target_file.exists():
        return {"lines": []}

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
            return {"lines": [line.strip() for line in lines]}
    except Exception:
        return {"lines": []}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.server:app", host="127.0.0.1", port=8000, reload=True)
