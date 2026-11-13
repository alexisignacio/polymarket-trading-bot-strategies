#!/bin/bash
# PolyHFT service manager: bot (paper) + dashboard
#
#   scripts/service.sh start     Start both (background, logs under logs/)
#   scripts/service.sh stop      Graceful stop (SIGINT), removes PID files
#   scripts/service.sh restart   Stop + start
#   scripts/service.sh status    PIDs and dashboard health

cd "$(dirname "$0")/.."

BOT_PID_FILE="bot.pid"
DASH_PID_FILE="dashboard.pid"
BOT_LOG="logs/service.out"
DASH_LOG="logs/dashboard.out"

pid_alive() {
    [ -n "$1" ] && kill -0 "$1" 2>/dev/null
}

read_pid() {
    [ -f "$1" ] && cat "$1" 2>/dev/null | tr -d '[:space:]'
}

# Bots huérfanos: procesos main.py vivos que no son el del pid file. Dos bots
# a la vez corrompen bot_state.json (cada uno sobrescribe el historial del
# otro), así que start/stop los matan siempre.
stray_bot_pids() {
    local owner
    owner=$(read_pid "$BOT_PID_FILE")
    for pid in $(pgrep -f "python main.py --paper" 2>/dev/null); do
        [ "$pid" = "$owner" ] || [ "$pid" = "$$" ] || echo "$pid"
    done
}

kill_strays() {
    local killed=0
    for pid in $(stray_bot_pids); do
        echo "matando bot huérfano (PID $pid)"
        kill -INT "$pid" 2>/dev/null
        killed=1
    done
    if [ "$killed" = "1" ]; then
        sleep 4
    fi
}

start_bot() {
    local pid
    kill_strays
    pid=$(read_pid "$BOT_PID_FILE")
    if pid_alive "$pid"; then
        echo "bot ya corriendo (PID $pid)"
        return 0
    fi
    source venv/bin/activate
    nohup python main.py --paper --log-level INFO > "$BOT_LOG" 2>&1 &
    # The bot writes bot.pid only after init (WS, reconciliation): wait for it
    for _ in $(seq 1 15); do
        sleep 1
        pid=$(read_pid "$BOT_PID_FILE")
        pid_alive "$pid" && break
    done
    if pid_alive "$pid"; then
        echo "bot iniciado (PID $pid, log: $BOT_LOG)"
    else
        echo "ERROR: el bot no arrancó (ver $BOT_LOG)"
        return 1
    fi
}

start_dashboard() {
    local pid
    pid=$(read_pid "$DASH_PID_FILE")
    if pid_alive "$pid"; then
        echo "dashboard ya corriendo (PID $pid)"
        return 0
    fi
    source venv/bin/activate
    nohup python -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000 > "$DASH_LOG" 2>&1 &
    echo $! > "$DASH_PID_FILE"
    for _ in $(seq 1 15); do
        sleep 1
        pid_alive "$(read_pid "$DASH_PID_FILE")" && curl -s --max-time 1 -o /dev/null http://127.0.0.1:8000/ && break
    done
    if pid_alive "$(read_pid "$DASH_PID_FILE")"; then
        echo "dashboard iniciado (PID $(read_pid "$DASH_PID_FILE"), http://127.0.0.1:8000)"
    else
        echo "ERROR: el dashboard no arrancó (ver $DASH_LOG)"
        return 1
    fi
}

stop_pid() {
    local pid
    pid=$(read_pid "$1")
    if ! pid_alive "$pid"; then
        echo "$2 no está corriendo"
        rm -f "$1"
        return 0
    fi
    kill -INT "$pid" 2>/dev/null
    for _ in $(seq 1 15); do
        pid_alive "$pid" || break
        sleep 1
    done
    if pid_alive "$pid"; then
        echo "$2 no respondió a SIGINT, enviando SIGTERM"
        kill "$pid" 2>/dev/null
        sleep 2
    fi
    echo "$2 detenido (PID $pid)"
    rm -f "$1"
}

case "$1" in
    start)
        start_bot
        start_dashboard
        ;;
    stop)
        stop_pid "$BOT_PID_FILE" "bot"
        kill_strays
        stop_pid "$DASH_PID_FILE" "dashboard"
        ;;
    restart)
        stop_pid "$BOT_PID_FILE" "bot"
        stop_pid "$DASH_PID_FILE" "dashboard"
        start_bot
        start_dashboard
        ;;
    status)
        for pair in "bot:$BOT_PID_FILE" "dashboard:$DASH_PID_FILE"; do
            name="${pair%%:*}"
            pid=$(read_pid "${pair#*:}")
            if pid_alive "$pid"; then
                echo "$name: corriendo (PID $pid)"
            else
                echo "$name: detenido"
            fi
        done
        for pid in $(stray_bot_pids); do
            echo "bot huérfano: PID $pid (proceso main.py duplicado — 'stop' lo mata)"
        done
        curl -s --max-time 3 -o /dev/null -w "dashboard HTTP %{http_code}\n" http://127.0.0.1:8000/ || true
        ;;
    *)
        echo "Uso: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
