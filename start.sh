#!/bin/bash
set -e
cd "$(dirname "$0")"

BRAINSTEM_HOME="$HOME/.brainstem"
VENV_PYTHON="$BRAINSTEM_HOME/venv/bin/python"

# Use venv if available; create it if missing
if [ ! -x "$VENV_PYTHON" ]; then
    echo "Setting up virtual environment..."
    PYTHON_CMD=$(command -v python3.11 || command -v python3.12 || command -v python3.13 || command -v python3)
    "$PYTHON_CMD" -m venv "$BRAINSTEM_HOME/venv" 2>/dev/null || {
        echo "Failed to create venv — run the installer: curl -fsSL https://kody-w.github.io/RAPP/installer/install.sh | bash"
        exit 1
    }
fi

# Install deps if needed
if ! "$VENV_PYTHON" -c "import flask, requests, dotenv" 2>/dev/null; then
    echo "Installing dependencies..."
    "$BRAINSTEM_HOME/venv/bin/pip" install -r requirements.txt -q
fi

# Create .env from example if missing
if [ ! -f .env ]; then
    cp .env.example .env 2>/dev/null || true
fi

# Force UTF-8 for all open() calls regardless of OS locale (PEP 540).
# Belt-and-suspenders — the kernel passes encoding="utf-8" explicitly,
# but this catches any agent or body_function that does a bare open().
export PYTHONUTF8=1

# Launch via the boot wrapper so body_functions and /web mount are
# wired in additively. The wrapper runs the canonical kernel verbatim
# (Constitution Article XXXIII §4 — kernel stays untouched) and
# injects body_function dispatch right before the server starts.
# Falls back to running the kernel directly if boot.py is missing
# (older organism layouts).
if [ -f boot.py ]; then
    exec "$VENV_PYTHON" boot.py
else
    exec "$VENV_PYTHON" brainstem.py
fi
