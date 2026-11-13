#!/usr/bin/env bash
# Launcher for the Soma engine.
# Usage:
#   ./run.sh                        # Launch web picker → run pipeline (default)
#   ./run.sh --queue file.json      # Use existing queue, skip picker
#   ./run.sh --source /path         # Direct mode (legacy)
#   ./run.sh --dashboard            # Dashboard only
#   ./run.sh --curses               # Interactive curses TUI (a-d, 1-8, 9, r, o, s, g)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$DIR/turn-key:$DIR/soma:${PYTHONPATH:-}"

# The venv carries the TurnKey SDK (editable install of ~/turn-key). Running
# under system python only worked while the SDK happened to be on sys.path.
if [ -x "$DIR/.venv/bin/python" ]; then
  PY="$DIR/.venv/bin/python"
else
  echo "run.sh: $DIR/.venv is missing; falling back to system python3" >&2
  PY=python3
fi

# If curses launcher requested
for arg in "$@"; do
  case "$arg" in
    --curses)
      exec "$PY" "$DIR/soma/curses_launcher.py" "$@"
      ;;
    --source|--queue)
      cd "$DIR/soma"
      exec "$PY" run_engine.py "$@"
      ;;
  esac
done

cd "$DIR"
exec "$PY" run_sessions.py "$@"
