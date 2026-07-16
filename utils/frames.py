"""
utils/frames.py — Frame recorder for the digital twin.

Every interaction the twin has — chat turn, agent install, rapp open,
state change — is an atomic FRAME. Frames are the unit the dreamcatcher
(Rappter engine, private) reconciles when divergent incarnations of a
twin are assimilated back into the home twin.

The frame envelope is the canonical RAPP/1 §7 frame — EXACTLY eleven keys,
verifiable by the reference kody-w/rapp-1 · rapp.py::verify_frame:

    {
      "spec":         "rapp/1",
      "kind":         "twin.pulse" | "twin.chat" | ...,   ← §7 dotted grammar
      "stream_id":    "rappid:@kody-w/twin:<64-hex>",     ← the identity owns the stream
      "seq":          0, 1, 2, …                          ← contiguous from genesis
      "utc":          "2026-04-28T01:23:45.678Z",         ← fixed millisecond form
      "payload":      { ... },
      "payload_hash": "H('rapp/1:particle', payload)",    ← the particle address
      "frame_hash":   "H('rapp/1:wave', frame-without-frame_hash-and-sig)",  ← the wave address
      "prev":         "<payload_hash of the previous frame, or null at genesis>",  ← §7 chain (particle)
      "prev_wave":    null,                               ← null off the swarm (net:*)
      "sig":          null                                ← owner sig over frame_hash (optional layer)
    }

payload_hash / frame_hash / prev make the log content-addressed and hash-chained
the RAPP way — the same bytes anyone canonicalizes turn into the same addresses,
and a tampered frame breaks the chain at its frame_hash. NB: §7's `prev` links to
the previous frame's PARTICLE (payload_hash), not its wave.

The dreamcatcher's per-incarnation sync metadata (frame_id, local_vt, the
incarnation stream token, and the post-merge `assimilated` flag) lives in a
SIDECAR, keyed by the frame's payload_hash — NOT in the §7 envelope. That keeps
the envelope immutable and exactly eleven keys, and lets `assimilated` be set on
merge without invalidating any frame_hash.

Storage:
    .brainstem_data/frames.jsonl       ← append-only §7 frame log, ONE LINE PER FRAME
    .brainstem_data/frames-meta.jsonl  ← sidecar: {payload_hash, frame_id, local_vt,
                                          incarnation_stream, assimilated}
    .brainstem_data/stream.json        ← per-incarnation stream token, NEVER packed

stream.json is in the egg exclusion list — when a twin egg is summoned onto a new
brainstem, the new brainstem mints its OWN incarnation token (recorded in the
sidecar) but inherits the source's RAPPID (the §7 stream_id). That's the
parallel-omniscience invariant: same twin, different incarnations, frames
attributable to which device produced them via the sidecar.

This module is a pure utility. No Flask. Imported by brainstem.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional


# ── canonical RAPP content-addressing (spec §2/§3), embedded verbatim from
#    the reference implementation kody-w/rapp-1 · rapp.py so this stays a
#    dependency-free utility. Same bytes → same address, everywhere. ──
def _canonical(v) -> str:
    """RFC 8785 JCS over the exact-value domain (no floats). Returns UTF-8 str."""
    if v is None or isinstance(v, bool):
        return json.dumps(v)
    if isinstance(v, int):
        return json.dumps(v)
    if isinstance(v, float):
        raise ValueError("floats require full-JCS number serialization; use ints/strings")
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ",".join(_canonical(x) for x in v) + "]"
    if isinstance(v, dict):
        keys = list(v.keys())
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate keys not allowed in canonical form")
        items = sorted(v.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _canonical(val)
                              for k, val in items) + "}"
    raise ValueError(f"uncanonicalizable type: {type(v).__name__}")


def _H(space: str, v) -> str:
    """Domain-separated content address: sha256(space + \\x0a + canonical(v))."""
    return hashlib.sha256(space.encode("utf-8") + b"\x0a" + _canonical(v).encode("utf-8")).hexdigest()


# Grammar-valid sentinel for a twin that has not minted an identity yet.
# 64 zero-hex can never collide with a real domain-separated mint, and it
# parses under the canonical §6.1 grammar so downstream tools don't choke.
_UNMINTED_RAPPID = "rappid:@anon/unminted:" + "0" * 64

# Resolve paths relative to brainstem root (.../rapp_brainstem/utils/frames.py)
_BRAINSTEM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BRAINSTEM_ROOT, ".brainstem_data")
_FRAMES_LOG = os.path.join(_DATA_DIR, "frames.jsonl")
_FRAMES_META = os.path.join(_DATA_DIR, "frames-meta.jsonl")
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
    if not os.path.exists(_FRAMES_META):
        _vt_counter = 1
        return 1
    last_vt = 0
    try:
        with open(_FRAMES_META, "rb") as f:
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


def _last_frame_field(field: str):
    """Read `field` from the most recent §7 frame in the log, or None at genesis."""
    if not os.path.exists(_FRAMES_LOG):
        return None
    try:
        with open(_FRAMES_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 8192)
            f.seek(size - block)
            tail = f.read().decode("utf-8", "ignore")
        last_line = tail.rstrip("\n").split("\n")[-1] if tail.strip() else ""
        if last_line:
            return json.loads(last_line).get(field)
    except Exception:
        return None
    return None


def _next_seq() -> int:
    """The next §7 seq: last frame's seq + 1, or 0 at genesis (contiguous)."""
    last = _last_frame_field("seq")
    return 0 if last is None else int(last) + 1


def record_frame(kind: str, payload: dict) -> dict:
    """Append a canonical RAPP/1 §7 frame to the log; return the frame written.

    The eleven-key envelope verifies against rapp.py::verify_frame. `prev` links
    to the previous frame's PARTICLE (payload_hash), `seq` is contiguous from the
    genesis (0). Per-incarnation dreamcatcher metadata is written to the sidecar
    (frames-meta.jsonl), keyed by payload_hash — never in the immutable envelope."""
    rappid = _read_identity_rappid() or _UNMINTED_RAPPID   # the §7 stream_id (identity owns the stream)
    incarnation = get_or_create_stream_id()                # per-device token → sidecar, not the envelope
    now = datetime.now(timezone.utc)
    utc = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    with _lock:
        seq = _next_seq()
        prev = _last_frame_field("payload_hash")           # §7: chain on the previous PARTICLE
        local_vt = _next_local_vt()
        payload_hash = _H("rapp/1:particle", payload)
        frame = {
            "spec":         "rapp/1",
            "kind":         kind,
            "stream_id":    rappid,
            "seq":          seq,
            "utc":          utc,
            "payload":      payload,
            "payload_hash": payload_hash,
            "prev":         prev,
            "prev_wave":    None,          # null off the swarm (non-net: stream)
            "sig":          None,          # unsigned; owner sig is a separate layer
        }
        # wave = H over the frame WITHOUT frame_hash AND sig (§7.3)
        pre = {k: frame[k] for k in frame if k not in ("frame_hash", "sig")}
        frame["frame_hash"] = _H("rapp/1:wave", pre)
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_FRAMES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(frame) + "\n")
        # sidecar: dreamcatcher sync metadata, joined to the frame by payload_hash
        meta = {
            "payload_hash":       payload_hash,
            "frame_id":           uuid.uuid4().hex,
            "local_vt":           local_vt,
            "incarnation_stream": incarnation,
            "assimilated":        None,     # set on merge — sidecar keeps the envelope immutable
        }
        with open(_FRAMES_META, "a", encoding="utf-8") as f:
            f.write(json.dumps(meta) + "\n")
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
