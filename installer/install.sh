#!/bin/bash
# install.sh — rappbox console installer.
#
# Installs the brainstem kernel + the bundled cartridges ("Wii Sports")
# at ~/.brainstem/. After install, the console is bootable with
# `bash ~/.brainstem/start.sh` — chat surface at http://localhost:7071.
#
# Usage:
#   curl -fsSL https://kody-w.github.io/rappbox-console/installer/install.sh | bash
#
# Optional: install with the twin expansion pack
#   curl -fsSL https://kody-w.github.io/rappbox-console/installer/install.sh | bash -s -- --with twin

set -e

REPO_URL="https://github.com/kody-w/rappbox-console.git"
INSTALL_DIR="$HOME/.brainstem"
WITH_PACKS=()

# Parse args: --with <pack-name>
while [ $# -gt 0 ]; do
    case "$1" in
        --with)
            shift
            WITH_PACKS+=("$1")
            shift
            ;;
        *)
            echo "[rappbox] unexpected arg: $1"
            shift
            ;;
    esac
done

echo "[rappbox] console install"
echo "  source: $REPO_URL"
echo "  target: $INSTALL_DIR"

# Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[rappbox] $INSTALL_DIR already a git repo — pulling latest"
    git -C "$INSTALL_DIR" pull --ff-only
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "[rappbox] $INSTALL_DIR exists but is not a git repo —"
        echo "          backing up to $INSTALL_DIR.bak.$(date +%s) before reinstall."
        mv "$INSTALL_DIR" "$INSTALL_DIR.bak.$(date +%s)"
    fi
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

chmod +x "$INSTALL_DIR/start.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/installer/install.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/installer/install-expansion-pack.sh" 2>/dev/null || true

# Bootstrap venv + dependencies
echo "[rappbox] bootstrapping venv at $INSTALL_DIR/venv …"
PYTHON_CMD=$(command -v python3.11 || command -v python3.12 || command -v python3.13 || command -v python3)
if [ -z "$PYTHON_CMD" ]; then
    echo "[rappbox] FAIL: no python3 on PATH. Install Python 3.10+ and re-run."
    exit 1
fi
"$PYTHON_CMD" -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# Install requested expansion packs
for pack in "${WITH_PACKS[@]}"; do
    echo "[rappbox] installing expansion pack: $pack"
    bash "$INSTALL_DIR/installer/install-expansion-pack.sh" "$pack"
done

cat <<EOF

[rappbox] ✓ Console installed at $INSTALL_DIR

Bundled cartridges (Wii Sports):
  • ManageMemory       — save typed memories that persist across chats
  • ContextMemory      — recall memories at conversation start
  • HackerNews         — top stories from HN's public Firebase API
  • LearnNewAgent      — meta-agent that creates other agents at runtime

Boot the console:
  bash $INSTALL_DIR/start.sh
  # → chat surface at http://127.0.0.1:7071

Add the twin expansion pack (anytime, after install):
  bash $INSTALL_DIR/installer/install-expansion-pack.sh twin

List available expansion packs:
  ls $INSTALL_DIR/expansion_packs/
EOF
