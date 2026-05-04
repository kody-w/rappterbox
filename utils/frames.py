"""
utils/frames.py — Frame recorder for the digital twin.

Every interaction the twin has — chat turn, agent install, rapp open,
state change — is an atomic FRAME. Frames are the unit the dreamcatcher
(Rappter engine, private) reconciles when divergent incarnations of a
twin are assimilated back into the home twin.

Schema:

    {
      "frame_id":   "uuid4",
      "rappid":     "rappid:twin:@kody-w/personal:abc123",
      "stream_id":  "stream-<entropy>",   ← per-incarnation, never packed
      "local_vt":   12345,                ← monotonic counter within stream
      "utc":        "2026-04-28T01:23:45.678Z",
      "kind":       "chat" | "agent_install" | "rapp_open" | ...,
      "payload":    { ... },
      "assimilated": null                 ← set by dreamcatcher on merge
    }

Storage:
    .brainstem_data/frames.jsonl   ← append-only log, ONE LINE PER FRAME
    .brainstem_data/stream.json    ← per-incarnation stream_id, NEVER packed

stream.json is in the egg exclusion list (utils/egg.py _NEVER_PACK_DIRS
+ explicit name) — when a twin egg is summoned onto a new brainstem,
the new brainstem mints its OWN stream_id but inherits the source's
RAPPID. That's the parallel-omniscience invariant: same twin, different
incarnations, frames clearly attributable to which device produced them.

This module is a pure utility. No Flask. Imported by brainstem.py.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

# Resolve paths relative to brainstem root (.../rapp_brainstem/utils/frames.py)
_BRAINSTEM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BRAINSTEM_ROOT, ".brainstem_data")
_FRAMES_LOG = os.path.join(_DATA_DIR, "frames.jsonl")
_STREAM_FILE = os.path.join(_DATA_DIR, "stream.json")
_IDENTITY_FILE = os.path.join(_DATA_DIR, "identity.json")

_lock = threading.Lock()
_vt_counter = None  # cached after first read


def _read_identity_rappid() -> Optional[str]:
    if not os.path.exists(_IDENTITY_FILE):
        return None
    try:
        with open(_IDENTITY_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("twin")
    except Exception:
        return None


def get_or_create_stream_id() -> str:
    """Return this brainstem incarnation's stream_id, minting on first call.

    stream.json is NOT packed in eggs (see utils/egg.py exclusions).
    A summoned twin lands on a brainstem that already minted its own
    stream_id — frames produced on the destination get attributed to
    that destination's stream, not the source's.
    """
    if os.path.exists(_STREAM_FILE):
        try:
            with open(_STREAM_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            sid = data.get("stream_id")
            if isinstance(sid, str) and sid.startswith("stream-"):
                return sid
        except Exception:
            pass
    sid = "stream-" + secrets.token_hex(8)
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_STREAM_FILE, "w", encoding="utf-8") as f:
        json.dump({"stream_id": sid, "minted_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
    return sid


def _next_local_vt() -> int:
    """Monotonic counter within this stream — read tail of frames.jsonl."""
    global _vt_counter
    if _vt_counter is not None:
        _vt_counter += 1
        return _vt_counter
    if not os.path.exists(_FRAMES_LOG):
        _vt_counter = 1
        return 1
    last_vt = 0
    try:
        with open(_FRAMES_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # Read last ~4KB to find the last newline-terminated record
            chunk = 4096 if size > 4096 else size
            f.seek(-chunk, os.SEEK_END)
            tail = f.read().decode("utf-8", errors="replace")
        last_line = tail.rstrip("\n").split("\n")[-1] if tail.strip() else ""
        if last_line:
            try:
                last_vt = int(json.loads(last_line).get("local_vt", 0))
            except Exception:
                last_vt = 0
    except Exception:
        last_vt = 0
    _vt_counter = last_vt + 1
    return _vt_counter


def record_frame(kind: str, payload: dict) -> dict:
    """Append a frame to the log. Returns the frame written (with metadata)."""
    rappid = _read_identity_rappid() or "rappid:twin:@anon/personal:unminted"
    stream_id = get_or_create_stream_id()
    with _lock:
        local_vt = _next_local_vt()
        frame = {
            "frame_id":    uuid.uuid4().hex,
            "rappid":      rappid,
            "stream_id":   stream_id,
            "local_vt":    local_vt,
            "utc":         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z",
            "kind":        kind,
            "payload":     payload,
            "assimilated": None,
        }
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_FRAMES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(frame) + "\n")
    return frame


def read_recent(limit: int = 50) -> list:
    """Return the last `limit` frames (newest last)."""
    if not os.path.exists(_FRAMES_LOG):
        return []
    out = []
    try:
        with open(_FRAMES_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-limit:] if limit and limit > 0 else out


def stream_summary() -> dict:
    """Lightweight summary of this stream's frame log — for /twin/manifest."""
    rappid = _read_identity_rappid()
    stream_id = get_or_create_stream_id()
    if not os.path.exists(_FRAMES_LOG):
        return {
            "rappid":     rappid,
            "stream_id":  stream_id,
            "frame_count": 0,
            "first_utc":  None,
            "last_utc":   None,
        }
    count = 0
    first_utc = None
    last_utc = None
    try:
        with open(_FRAMES_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    fr = json.loads(line)
                except Exception:
                    continue
                count += 1
                if first_utc is None:
                    first_utc = fr.get("utc")
                last_utc = fr.get("utc")
    except Exception:
        pass
    return {
        "rappid":      rappid,
        "stream_id":   stream_id,
        "frame_count": count,
        "first_utc":   first_utc,
        "last_utc":    last_utc,
    }
