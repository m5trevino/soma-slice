#!/usr/bin/env bash
# Session Picker, foreground edition.
#
# start.sh backgrounds uvicorn and Vite with nohup and then exits, so its
# window cannot own the process lifetime. This variant keeps both servers in
# the foreground of the current window: closing the window (or Ctrl+C) stops
# both. Same ports, same venv, same logs as start.sh.
set -uo pipefail

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJ_DIR/backend"
FRONTEND_DIR="$PROJ_DIR/frontend"
BACKEND_PORT="${BACKEND_PORT:-8001}"
FRONTEND_PORT="${FRONTEND_PORT:-5174}"

BACK_PID=""
FRONT_PID=""

cleanup() {
    if [ -n "$BACK_PID" ]; then kill "$BACK_PID" 2>/dev/null || true; fi
    if [ -n "$FRONT_PID" ]; then kill "$FRONT_PID" 2>/dev/null || true; fi
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

# Clear stale listeners on our two ports only.
for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    stale="$(lsof -ti:"$port" 2>/dev/null || true)"
    if [ -n "$stale" ]; then
        # shellcheck disable=SC2086
        kill $stale 2>/dev/null || true
    fi
done

if [ ! -x "$BACKEND_DIR/venv/bin/python" ]; then
    echo "  creating backend venv..."
    python3 -m venv "$BACKEND_DIR/venv"
    "$BACKEND_DIR/venv/bin/pip" install -q -r "$BACKEND_DIR/requirements.txt"
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    echo "  installing frontend dependencies..."
    ( cd "$FRONTEND_DIR" && npm install --silent )
fi

# Build the consolidated sessions (deduped chains) so the picker has data to
# show. First run consolidates the whole vault; later runs skip if already
# built (delete lineage/consolidated/ to force a rebuild).
CONSOLIDATED_DIR="/media/storage/dendritic123/chat-logs/lineage/consolidated"
if [ ! -d "$CONSOLIDATED_DIR" ] || [ -z "$(ls -A "$CONSOLIDATED_DIR" 2>/dev/null)" ]; then
    echo "  consolidating session chains (first run — can take a minute)..."
    python3 "$PROJ_DIR/../tools/consolidate_chains.py" --all --min-members 2 \
        --target /media/storage/dendritic123/chat-logs/lineage 2>&1 | tail -10
else
    echo "  consolidated sessions ready ($(find "$CONSOLIDATED_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l) chains)"
fi

echo "  backend   http://localhost:$BACKEND_PORT"
echo "  frontend  http://localhost:$FRONTEND_PORT"
echo "  close this window, or press Ctrl+C, to stop both"
echo

( cd "$BACKEND_DIR" && exec ./venv/bin/python -m uvicorn main:app \
    --host 127.0.0.1 --port "$BACKEND_PORT" ) &
BACK_PID=$!

( cd "$FRONTEND_DIR" && exec npx vite --host 127.0.0.1 --port "$FRONTEND_PORT" ) &
FRONT_PID=$!

# Exit as soon as either server stops; the trap tears down the other.
wait -n
