#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ_DIR="$SCRIPT_DIR"
BACKEND_DIR="$PROJ_DIR/backend"
FRONTEND_DIR="$PROJ_DIR/frontend"
BACKEND_PORT=8001
FRONTEND_PORT=5174

echo "═══════════════════════════════════════"
echo "  Soma Session Picker Launcher"
echo "═══════════════════════════════════════"
echo ""

# Kill existing
PID=$(lsof -ti:$BACKEND_PORT 2>/dev/null || true)
[ -n "$PID" ] && kill $PID 2>/dev/null && echo "  Killed backend (PID $PID)"
PID=$(lsof -ti:$FRONTEND_PORT 2>/dev/null || true)
[ -n "$PID" ] && kill $PID 2>/dev/null && echo "  Killed frontend (PID $PID)"
sleep 1

# Backend
echo ""
echo "[1/3] Starting backend on port $BACKEND_PORT..."
cd "$BACKEND_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -q -r requirements.txt
nohup python3 -m uvicorn main:app --host 0.0.0.0 --port $BACKEND_PORT > /tmp/soma-picker-backend.log 2>&1 &
BACKEND_PID=$!
echo "  Backend PID: $BACKEND_PID"

echo "  Waiting for backend..."
for i in $(seq 1 10); do
    if curl -s "http://127.0.0.1:$BACKEND_PORT/api/health" > /dev/null 2>&1; then
        echo "  Backend is ready!"
        break
    fi
    sleep 0.5
done

# Frontend
echo ""
echo "[2/3] Starting frontend on port $FRONTEND_PORT..."
cd "$FRONTEND_DIR"
nohup npx vite --host 0.0.0.0 --port $FRONTEND_PORT > /tmp/soma-picker-frontend.log 2>&1 &
FRONTEND_PID=$!
echo "  Frontend PID: $FRONTEND_PID"

echo "  Waiting for frontend..."
for i in $(seq 1 15); do
    if curl -s "http://127.0.0.1:$FRONTEND_PORT" > /dev/null 2>&1; then
        echo "  Frontend is ready!"
        break
    fi
    sleep 0.5
done

# Summary
echo ""
echo "═══════════════════════════════════════"
echo "  Soma Session Picker is running!"
echo "═══════════════════════════════════════"
echo ""
echo "  Frontend: http://localhost:$FRONTEND_PORT"
echo "  Backend:  http://localhost:$BACKEND_PORT"
echo ""
echo "  Backend PID: $BACKEND_PID"
echo "  Frontend PID: $FRONTEND_PID"
echo ""
echo "  Logs:"
echo "    Backend:  /tmp/soma-picker-backend.log"
echo "    Frontend: /tmp/soma-picker-frontend.log"
echo ""
