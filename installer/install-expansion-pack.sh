#!/bin/bash
# install-expansion-pack.sh — copy a single expansion pack's cartridges
# into the brainstem's agents/ dir so they're auto-loaded at next boot.
#
# Idempotent. Never overwrites local data — refuses if a target file
# already exists with different content.
#
# Usage:
#   bash $HOME/.brainstem/installer/install-expansion-pack.sh twin
#   bash $HOME/.brainstem/installer/install-expansion-pack.sh <pack-name>

set -e

BRAINSTEM_HOME="$HOME/.brainstem"
PACKS_DIR="$BRAINSTEM_HOME/expansion_packs"

# Detect the brainstem source dir (where AGENTS_PATH resolves to). The
# rapp-installer puts brainstem.py inside a nested dir (canonically
# ~/.brainstem/src/rapp_brainstem/). Caller may also pre-set
# BRAINSTEM_SRC env to skip auto-detect.
detect_brainstem_src() {
    if [ -n "${BRAINSTEM_SRC:-}" ] && [ -d "$BRAINSTEM_SRC" ]; then
        echo "$BRAINSTEM_SRC"; return
    fi
    local found
    found=$(find "$BRAINSTEM_HOME" -maxdepth 4 -name "brainstem.py" -not -path "*/venv/*" -not -path "*/__pycache__/*" 2>/dev/null | head -1)
    if [ -z "$found" ]; then
        echo "$BRAINSTEM_HOME/src/rapp_brainstem"
    else
        dirname "$found"
    fi
}

BRAINSTEM_SRC="$(detect_brainstem_src)"

PACK="${1:-}"
if [ -z "$PACK" ]; then
    echo "FAIL: usage: $0 <pack-name>"
    echo ""
    echo "Available packs:"
    ls "$PACKS_DIR" 2>/dev/null | sed 's/^/  /'
    exit 1
fi

PACK_DIR="$PACKS_DIR/$PACK"
TARGET_DIR="$BRAINSTEM_SRC/agents"

if [ ! -d "$PACK_DIR" ]; then
    echo "FAIL: expansion pack '$PACK' not found at $PACK_DIR"
    echo ""
    echo "Available packs:"
    ls "$PACKS_DIR" 2>/dev/null | sed 's/^/  /'
    exit 1
fi

if [ ! -d "$TARGET_DIR" ]; then
    echo "FAIL: brainstem agents dir not found at $TARGET_DIR"
    echo "      Run the rappterbox or rapp-installer install first."
    exit 1
fi

count=0
skipped=0
for f in "$PACK_DIR"/*_agent.py; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    target="$TARGET_DIR/$base"

    if [ -f "$target" ]; then
        if cmp -s "$f" "$target"; then
            echo "[rappterbox] $base already installed — identical, skipping"
            skipped=$((skipped + 1))
            continue
        else
            echo "[rappterbox] WARNING: $target already exists with different content — refusing to overwrite (rule: never overwrite local data)"
            echo "          To proceed, delete $target manually and re-run."
            skipped=$((skipped + 1))
            continue
        fi
    fi

    cp "$f" "$target"
    echo "[rappterbox] installed: $base → $target"
    count=$((count + 1))
done

echo ""
echo "[rappterbox] expansion pack '$PACK' installed: $count new, $skipped existing"
echo ""
if [ "$count" -gt 0 ]; then
    echo "Restart the brainstem so the new cartridges load:"
    echo "  bash $BRAINSTEM_SRC/start.sh"
fi
