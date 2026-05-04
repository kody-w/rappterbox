#!/bin/bash
# install.sh — rappterbox console installer.
#
# rappterbox = the canonical rapp-installer brainstem + bundled cartridges
# + expansion packs. The brainstem itself comes from the static-ancestor
# rapp-installer (immutable, never changes). rappterbox adds the
# cartridge layer on top.
#
# Usage:
#   curl -fsSL https://kody-w.github.io/rappterbox/installer/install.sh | bash
#
# Optional: install with the twin expansion pack
#   curl -fsSL https://kody-w.github.io/rappterbox/installer/install.sh | bash -s -- --with twin

set -e

RAPP_INSTALLER_URL="https://kody-w.github.io/rapp-installer/install.sh"
RAPPTERBOX_REPO="https://github.com/kody-w/rappterbox.git"
RAPPTERBOX_DIR="${RAPPTERBOX_DIR:-$HOME/.rappterbox}"
BRAINSTEM_HOME="$HOME/.brainstem"

# The rapp-installer clones the full RAPP repo, leaving brainstem.py inside
# a nested dir (canonically ~/.brainstem/src/rapp_brainstem/). Older / flat
# layouts may place it directly at ~/.brainstem/. We auto-detect by
# locating brainstem.py under the install root and using its dir as the
# canonical "brainstem source" — that's where AGENTS_PATH and utils/web/
# resolve to.
detect_brainstem_src() {
    local found
    found=$(find "$BRAINSTEM_HOME" -maxdepth 4 -name "brainstem.py" -not -path "*/venv/*" -not -path "*/__pycache__/*" 2>/dev/null | head -1)
    if [ -z "$found" ]; then
        # Brainstem not installed yet — fall back to canonical rapp-installer path
        echo "$BRAINSTEM_HOME/src/rapp_brainstem"
    else
        dirname "$found"
    fi
}

# Parse args: --with <pack-name>  (collect, install after layering)
WITH_PACKS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --with)
            shift
            WITH_PACKS+=("$1")
            shift
            ;;
        *)
            echo "[rappterbox] unexpected arg: $1"
            shift
            ;;
    esac
done

echo "[rappterbox] ─────────────────────────────────────────────────"
echo "[rappterbox] Step 1 / 2: install the brainstem via the static-"
echo "[rappterbox]            ancestor rapp-installer."
echo "[rappterbox] ─────────────────────────────────────────────────"
curl -fsSL "$RAPP_INSTALLER_URL" | bash

# Now that the brainstem is on disk, locate where it actually lives.
BRAINSTEM_SRC="$(detect_brainstem_src)"
if [ ! -d "$BRAINSTEM_SRC" ]; then
    echo "[rappterbox] FAIL: brainstem source dir not found after install (expected at $BRAINSTEM_SRC)"
    exit 1
fi
echo "[rappterbox] brainstem source detected at: $BRAINSTEM_SRC"

echo ""
echo "[rappterbox] ─────────────────────────────────────────────────"
echo "[rappterbox] Step 2 / 2: layer rappterbox cartridges + expansion"
echo "[rappterbox]            packs onto the brainstem."
echo "[rappterbox] ─────────────────────────────────────────────────"

# Sync rappterbox repo (cached at ~/.rappterbox/ between install runs)
if [ -d "$RAPPTERBOX_DIR/.git" ]; then
    echo "[rappterbox] updating $RAPPTERBOX_DIR …"
    git -C "$RAPPTERBOX_DIR" pull --ff-only --quiet
else
    if [ -d "$RAPPTERBOX_DIR" ]; then
        mv "$RAPPTERBOX_DIR" "$RAPPTERBOX_DIR.bak.$(date +%s)"
    fi
    echo "[rappterbox] cloning $RAPPTERBOX_REPO → $RAPPTERBOX_DIR"
    git clone --depth 1 --quiet "$RAPPTERBOX_REPO" "$RAPPTERBOX_DIR"
fi

# Layer 1: bundled "Wii Sports" cartridges (in case rapp-installer ships
# without them, or with older versions). Never overwrites local data —
# refuses if a target file exists with different content.
mkdir -p "$BRAINSTEM_SRC/agents"
new_count=0
skipped_count=0
for f in "$RAPPTERBOX_DIR/agents/"*_agent.py; do
    [ -f "$f" ] || continue
    name="$(basename "$f")"
    target="$BRAINSTEM_SRC/agents/$name"
    if [ -f "$target" ]; then
        if cmp -s "$f" "$target"; then
            skipped_count=$((skipped_count + 1))
        else
            echo "[rappterbox]   ⚠ $name exists with different content — skipping (rule: never overwrite local data)"
            skipped_count=$((skipped_count + 1))
        fi
    else
        cp "$f" "$target"
        new_count=$((new_count + 1))
    fi
done
echo "[rappterbox] cartridges: $new_count new, $skipped_count already-present"

# Layer 2: expansion packs live at the install root (NOT under src/) — they're
# not auto-loaded by the brainstem, just held until the user installs one.
mkdir -p "$BRAINSTEM_HOME/expansion_packs"
cp -R "$RAPPTERBOX_DIR/expansion_packs/." "$BRAINSTEM_HOME/expansion_packs/" 2>/dev/null || true
echo "[rappterbox] expansion packs available at $BRAINSTEM_HOME/expansion_packs/"

# Layer 3: the expansion-pack installer helper script (at install root for
# ergonomics — users invoke it via ~/.brainstem/installer/...)
mkdir -p "$BRAINSTEM_HOME/installer"
cp "$RAPPTERBOX_DIR/installer/install-expansion-pack.sh" "$BRAINSTEM_HOME/installer/install-expansion-pack.sh"
chmod +x "$BRAINSTEM_HOME/installer/install-expansion-pack.sh"
# Pass the detected brainstem src to the helper via env var
export BRAINSTEM_SRC

# Layer 4: the rappterbox dashboard (Xbox-360-style console UI)
if [ -f "$RAPPTERBOX_DIR/console.html" ]; then
    mkdir -p "$BRAINSTEM_SRC/utils/web"
    cp "$RAPPTERBOX_DIR/console.html" "$BRAINSTEM_SRC/utils/web/console.html"
    echo "[rappterbox] dashboard available at /web/console.html when the brainstem is running"
fi

# Apply requested expansion packs (--with foo --with bar)
for pack in "${WITH_PACKS[@]}"; do
    echo ""
    echo "[rappterbox] installing expansion pack: $pack"
    bash "$BRAINSTEM_HOME/installer/install-expansion-pack.sh" "$pack"
done

cat <<EOF

[rappterbox] ─────────────────────────────────────────────────
[rappterbox] ✓ Console installed at $BRAINSTEM_SRC
[rappterbox] ─────────────────────────────────────────────────

Bundled cartridges (Wii Sports — already in agents/):
  • ManageMemory       — save typed memories that persist across chats
  • ContextMemory      — recall saved memories at conversation start
  • HackerNews         — top stories from HN's public Firebase API
  • LearnNewAgent      — meta-cartridge: generates new agents at runtime

Available expansion packs:
$(ls "$BRAINSTEM_HOME/expansion_packs/" 2>/dev/null | sed 's/^/  • /')

Boot the console:
  bash $BRAINSTEM_SRC/start.sh
  # → chat surface at http://127.0.0.1:7071
  # → dashboard at  http://127.0.0.1:7071/web/console.html

Add an expansion pack (anytime):
  bash $BRAINSTEM_HOME/installer/install-expansion-pack.sh <pack-name>

Specification:
  https://github.com/kody-w/rappterbox/blob/main/SPEC.md

EOF
