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
import re
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional


# ── canonical RAPP content-addressing (spec §4/§5), embedded verbatim from
#    the reference implementation kody-w/rapp-1 · rapp.py at rev-17
#    (f6bafe76735ba73510518810c8bc8cd133dcf527; `canonical`/`H` renamed
#    `_canonical`/`_H`) so this stays a dependency-free utility.
#    Same bytes → same address, everywhere. ──
# §4 (b), RFC 7493 §2.1: surrogate code points and the 66 noncharacters are outside I-JSON.
_NOT_IJSON_CHAR = re.compile(
    "[\ud800-\udfff\ufdd0-\ufdef"
    + "".join(chr(plane << 16 | 0xFFFE) + chr(plane << 16 | 0xFFFF) for plane in range(17))
    + "]"
)


def _ijson_string(s):
    """A §4 string or member name in JCS form; refuses a surrogate or a noncharacter (§4 (b))."""
    bad = _NOT_IJSON_CHAR.search(s)
    if bad:
        raise ValueError(
            f"string holds U+{ord(bad.group()):04X}, a surrogate or noncharacter outside I-JSON (§4 (b))"
        )
    return json.dumps(s, ensure_ascii=False)


def _number_to_string(x):
    """ECMA-262 Number::toString of a finite binary64 value: the RFC 8785 §3.2.2.3 number form."""
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("NaN and infinities are outside the §4 domain")
    if x == 0:
        return "0"                          # both zeros; -0 serializes as 0
    # repr() is the shortest digit string that round-trips (nearest, ties to even), the
    # digits Number::toString picks; only the layout differs, so re-lay it out here.
    mantissa, _, exponent = repr(abs(x)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    n = len(whole) + int(exponent or 0) - (len(whole) + len(fraction) - len(digits))
    digits = digits.rstrip("0")
    k = len(digits)                         # value = 0.digits * 10**n
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * -n + digits
    else:
        text = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if n > 0 else "-") + str(abs(n - 1))
    return ("-" if x < 0 else "") + text


def _canonical(v):
    """RFC 8785 JCS over the §4 I-JSON domain. Returns the canonical form as a str (encode as UTF-8)."""
    if v is None or isinstance(v, bool):
        return json.dumps(v)
    if isinstance(v, int):
        if abs(v) <= 2**53 - 1:
            return json.dumps(v)
        # §4 (c): a number is a binary64 value; an int outside +/-(2^53-1) is admitted only
        # when it is one exactly (2**53 is, 2**53 + 1 is not), and then serializes as JCS does.
        try:
            as_binary64 = float(v)
        except OverflowError:
            as_binary64 = None
        if as_binary64 != v:
            raise ValueError("int is not exactly representable as binary64 (§4 (c)); carry it as a string")
        return _number_to_string(as_binary64)
    if isinstance(v, float):
        return _number_to_string(v)
    if isinstance(v, str):
        return _ijson_string(v)
    if isinstance(v, list):
        return "[" + ",".join(_canonical(x) for x in v) + "]"
    if isinstance(v, dict):
        if not all(isinstance(k, str) for k in v):
            raise ValueError("member names must be strings")
        # RFC 8785 orders member names by UTF-16 code units; plain sorted()
        # is code-POINT order and diverges for non-BMP keys.
        keys = sorted(v.keys(), key=lambda k: k.encode("utf-16-be", "surrogatepass"))
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate keys")
        return "{" + ",".join(_ijson_string(k) + ":" + _canonical(v[k]) for k in keys) + "}"
    raise ValueError(f"non-I-JSON value: {type(v)}")


# §5 (rev-17 E-7): every tag belongs to exactly one function; any other tag is refused.
_H_SPACES = frozenset({"rapp/1:particle", "rapp/1:wave", "rapp/1:egg-manifest",
                       "rapp/1:sealed-aad", "rapp/1:sealed-key-request"})
_HB_SPACES = frozenset({"rapp/1:egg", "rapp/1:rappid", "rapp/1:grail", "rapp/1:seal"})


def _H(space, v):
    if not (isinstance(space, str) and space in _H_SPACES):
        raise ValueError(f"§5: H (a value hash) is used only with the tags {sorted(_H_SPACES)}; refused {space!r}")
    return hashlib.sha256(space.encode() + b"\x0a" + _canonical(v).encode("utf-8")).hexdigest()


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
