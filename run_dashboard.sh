#!/bin/bash

# PolyHFT Dashboard Launcher
# Starts the FastAPI Dashboard web server

cd "$(dirname "$0")"

# Activate virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
fi

echo "=================================================="
echo "📊 PolyHFT Web Dashboard"
echo "=================================================="
echo ""
echo "🚀 Servidor iniciado en:"
echo "👉 http://localhost:8000"
echo "👉 http://127.0.0.1:8000"
echo ""
echo "Presiona Ctrl+C para detener el servidor"
echo "=================================================="
echo ""

# Start uvicorn
exec python3 -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
