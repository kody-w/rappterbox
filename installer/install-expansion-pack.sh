#!/bin/bash
# install-expansion-pack.sh — copy a single expansion pack's cartridges
# into ~/.brainstem/agents/ so the brainstem auto-loads them at next boot.
#
# Idempotent. Never overwrites local data — refuses if a target file
# already exists with different content.
#
# Usage:
#   bash $HOME/.brainstem/installer/install-expansion-pack.sh twin
#   bash $HOME/.brainstem/installer/install-expansion-pack.sh <pack-name>

set -e

PACK="${1:-}"
if [ -z "$PACK" ]; then
    echo "FAIL: usage: $0 <pack-name>"
    echo ""
    echo "Available packs:"
    ls "$HOME/.brainstem/expansion_packs/" 2>/dev/null | sed 's/^/  /'
    exit 1
fi

PACK_DIR="$HOME/.brainstem/expansion_packs/$PACK"
TARGET_DIR="$HOME/.brainstem/agents"

if [ ! -d "$PACK_DIR" ]; then
    echo "FAIL: expansion pack '$PACK' not found at $PACK_DIR"
    echo ""
    echo "Available packs:"
    ls "$HOME/.brainstem/expansion_packs/" 2>/dev/null | sed 's/^/  /'
    exit 1
fi

mkdir -p "$TARGET_DIR"

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
    echo "[rappterbox] installed: $base"
    count=$((count + 1))
done

echo ""
echo "[rappterbox] expansion pack '$PACK' installed: $count new, $skipped existing"
echo ""
if [ "$count" -gt 0 ]; then
    echo "Restart the brainstem so the new cartridges load:"
    echo "  bash $HOME/.brainstem/start.sh"
fi
