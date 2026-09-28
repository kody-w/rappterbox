"""
utils/egg.py — Brainstem Egg Cartridge format (brainstem-egg/2.0)

Eggs are how a brainstem's contents become portable. A `.egg` is a zip
archive with a typed manifest and a file tree that mirrors the brainstem
layout. Pack one on machine A; unpack it on machine B; the digital
organism (agent set, memory, chat tabs, rapps, state) shows up intact.

The egg is THE local-first guarantee. Without it, the brainstem is locked
to one disk. With it, the brainstem is a runtime that hosts whatever life
you point at it — your twin, your work-self, a shared team brain — and
that life is yours, in your hands, in a single file you control.

Four cartridge types share one format:

    rapplication   one agent + one ui + one service + one state scope
    twin           all agents + cross-agent memory + chat tabs
    snapshot       full brainstem dump (agents + services + ui + data)
    swarm          a converged multi-agent singleton (existing rapp_store
                   shape; preserved here for catalog compatibility)

The unpacker dispatches on `type`. The pack/unpack logic is generic over
a path-mapping table; each type just declares which paths to include.

────────────────────────────────────────────────────────────────────────
Egg layout on disk (after `unzip foo.egg`):

    foo.egg
    ├── manifest.json     {"schema":"brainstem-egg/2.0", "type":"twin", ...}
    ├── agents/<file>.py
    ├── services/<file>.py
    ├── rapp_ui/<id>/...
    └── data/<...>        (mirrors .brainstem_data/, secrets removed)

────────────────────────────────────────────────────────────────────────
Backward compatibility:

  rapp-egg/1.0 (the legacy single-rapp format used by binder) is still
  accepted by `unpack()`. The legacy reader extracts agent.py, service.py,
  ui/* and state/* into the appropriate locations, exactly as the binder
  did. Old eggs round-trip without conversion.

────────────────────────────────────────────────────────────────────────
Excluded from packing (always):

  - .copilot_token, .copilot_session, voice.zip   (auth secrets)
  - venv/, __pycache__/, .pytest_cache/           (environment artifacts)
  - .brainstem_data/private/                      (explicit no-share)
  - .DS_Store, Thumbs.db                          (OS noise)

This module is a pure utility — no Flask, no service registration. It is
imported by both the binder service (legacy compat) and brainstem.py
(/agents/import auto-detect, /rapps/export/* endpoints).
"""

from __future__ import annotations

import base64
import decimal
import hashlib
import io
import json
import os
import re
import secrets
import struct
import time
import unicodedata
import uuid
import zipfile
import zlib
from typing import Optional

# ── Paths (resolved relative to this file's brainstem root) ─────────────
# utils/egg.py lives at .../rapp_brainstem/utils/egg.py — two dirname
# walks reach the brainstem root.
_BRAINSTEM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENTS_DIR = os.path.join(_BRAINSTEM_ROOT, "agents")
_SERVICES_DIR = os.path.join(_BRAINSTEM_ROOT, "utils", "services")
_DATA_DIR = os.path.join(_BRAINSTEM_ROOT, ".brainstem_data")
_UI_BASE_DIR = os.path.join(_DATA_DIR, "rapp_ui")

EGG_SCHEMA_V2 = "brainstem-egg/2.0"
EGG_SCHEMA_V2_1 = "brainstem-egg/2.1"  # variant-repo aware (carries source pointer + brainstem pin)
EGG_SCHEMA_V1 = "rapp-egg/1.0"  # legacy binder format

# ── §9 rapp/1-egg packing (stdlib-only, inlined from kody-w/rapp-1 · rapp.py at rev-17,
#    f6bafe76735ba73510518810c8bc8cd133dcf527; names prefixed _egg_/_EGG_) ──
EGG_SCHEMA = "rapp/1-egg"
_EGG_MAX_CANONICAL = 1024 * 1024  # §4 (d): the ceiling on a canonical form
_EGG_NOT_IJSON_CHAR = re.compile(
    "[\ud800-\udfff\ufdd0-\ufdef"
    + "".join(chr(plane << 16 | 0xFFFE) + chr(plane << 16 | 0xFFFF) for plane in range(17))
    + "]"
)


def _egg_ijson_string(s):
    """A §4 string or member name in JCS form; refuses a surrogate or a noncharacter (§4 (b))."""
    bad = _EGG_NOT_IJSON_CHAR.search(s)
    if bad:
        raise ValueError(
            f"string holds U+{ord(bad.group()):04X}, a surrogate or noncharacter outside I-JSON (§4 (b))"
        )
    return json.dumps(s, ensure_ascii=False)


def _egg_number_to_string(x):
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


def _egg_canonical(v):
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
        return _egg_number_to_string(as_binary64)
    if isinstance(v, float):
        return _egg_number_to_string(v)
    if isinstance(v, str):
        return _egg_ijson_string(v)
    if isinstance(v, list):
        return "[" + ",".join(_egg_canonical(x) for x in v) + "]"
    if isinstance(v, dict):
        if not all(isinstance(k, str) for k in v):
            raise ValueError("member names must be strings")
        # RFC 8785 orders member names by UTF-16 code units; plain sorted()
        # is code-POINT order and diverges for non-BMP keys.
        keys = sorted(v.keys(), key=lambda k: k.encode("utf-16-be", "surrogatepass"))
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate keys")
        return "{" + ",".join(_egg_ijson_string(k) + ":" + _egg_canonical(v[k]) for k in keys) + "}"
    raise ValueError(f"non-I-JSON value: {type(v)}")


def _egg_json_number(token):
    """§4 (c): parse a number token as its nearest binary64 d; refuse unless d is finite and
    Number::toString(d) denotes exactly the token's value (so 0.1 passes, 0.10000000000000001 does not)."""
    d = float(token)                                   # correctly rounded, ties to even; overlong -> +/-inf
    if d != d or d in (float("inf"), float("-inf")):
        raise ValueError(f"number token {token[:40]} is not a finite binary64 value (§4 (c))")
    try:
        same = decimal.Decimal(token) == decimal.Decimal(_egg_number_to_string(d))
    except ArithmeticError:
        # An exponent beyond decimal's range. d is finite, so it is a zero, and the token
        # denotes the same value iff every digit of its significand is zero.
        same = not any(c in "123456789" for c in token.lower().partition("e")[0])
    if not same:
        raise ValueError(f"number token {token[:40]} does not survive the binary64 round trip (§4 (c))")
    return d


def _egg_json_int(token):
    if token == "-0":
        return -0.0          # E-9: -0 is not an integer token a field rule may take for 0; _egg_canonical(-0.0) is "0"
    d = _egg_json_number(token)  # refuses 9007199254740993 and overlong tokens before int() runs
    value = int(token)
    # 10**23 passes §4 (c) (its d prints as "1e+23") but is not d; the value parsed is d itself.
    return value if value == d else int(d)


def _egg_names_ok(value):
    """§4 (rev-17 E-5, E-6): a producer treats every `payload` member name, at any depth, as a new string.

    It refuses (never normalizes) a name that is not NFC or that holds a code point unassigned
    (General_Category Cn) in the Unicode version this Python implements."""
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for name, item in current.items():
                if not isinstance(name, str):
                    raise ValueError("payload member names must be strings")
                if not unicodedata.is_normalized("NFC", name):
                    raise ValueError(f"payload member name is not NFC (§4): {name!r}")
                if any(unicodedata.category(char) == "Cn" for char in name):
                    raise ValueError(f"payload member name holds an unassigned code point (§4): {name!r}")
                stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)


_EGG_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}


def _egg_path_valid(path):
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or path != unicodedata.normalize("NFC", path)
        or re.match(r"^[A-Za-z]:", path)
    ):
        return False
    parts = path.split("/")
    for part in parts:
        if (
            part in ("", ".", "..")
            or part.endswith((" ", "."))
            or ":" in part
            or any(ord(char) < 32 for char in part)
            or part.split(".", 1)[0].upper() in _EGG_WINDOWS_RESERVED
        ):
            return False
    return True


_EGG_ZIP_LOCAL = struct.Struct("<IHHHHHIIIHH")          # 30-octet local file header
_EGG_ZIP_CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")  # 46-octet central directory header
_EGG_ZIP_END = struct.Struct("<IHHHHIIH")               # 22-octet end-of-central-directory record
_EGG_ZIP_VERSION = 0x0014                               # §9.1: version needed 20, version made by 0x0014
_EGG_ZIP_FLAGS = 0x0800                                 # §9.1: UTF-8 name; no data descriptor, no encryption
_EGG_ZIP_DOS_TIME, _EGG_ZIP_DOS_DATE = 0x0000, 0x0021       # §9.1: 1980-01-01 00:00:00
_EGG_ZIP_MAX_ENTRIES = 0xFFFE                           # 0xFFFF is the ZIP64 marker
_EGG_ZIP_MAX_FIELD = 0xFFFFFFFE                         # 0xFFFFFFFF is the ZIP64 marker


def _egg_zip_pack(entries):
    """§9.1 container writer: [(name, octets)] in entry order -> bytes, every header field as pinned.

    Refuses (ValueError) an archive that would need ZIP64: more than 65,534 entries, or any
    size or offset above 0xFFFFFFFE."""
    if len(entries) > _EGG_ZIP_MAX_ENTRIES:
        raise ValueError("egg needs more than 65,534 ZIP entries; ZIP64 is not a §9.1 container")
    local, central, offset = [], [], 0
    for name, data in entries:
        encoded = name.encode("utf-8")
        if len(encoded) > 0xFFFF:
            raise ValueError(f"ZIP entry name exceeds 65,535 octets: {name!r}")
        if len(data) > _EGG_ZIP_MAX_FIELD or offset > _EGG_ZIP_MAX_FIELD:
            raise ValueError("egg needs a ZIP size or offset above 0xFFFFFFFE; ZIP64 is not a §9.1 container")
        crc = zlib.crc32(data) & 0xFFFFFFFF
        fields = (_EGG_ZIP_FLAGS, 0, _EGG_ZIP_DOS_TIME, _EGG_ZIP_DOS_DATE, crc, len(data), len(data), len(encoded), 0)
        header = _EGG_ZIP_LOCAL.pack(0x04034B50, _EGG_ZIP_VERSION, *fields) + encoded
        central.append(_EGG_ZIP_CENTRAL.pack(0x02014B50, _EGG_ZIP_VERSION, _EGG_ZIP_VERSION, *fields, 0, 0, 0, 0, offset) + encoded)
        local += [header, data]
        offset += len(header) + len(data)
    directory = b"".join(central)
    if offset > _EGG_ZIP_MAX_FIELD or len(directory) > _EGG_ZIP_MAX_FIELD:
        raise ValueError("egg needs a ZIP size or offset above 0xFFFFFFFE; ZIP64 is not a §9.1 container")
    end = _EGG_ZIP_END.pack(0x06054B50, 0, 0, len(entries), len(entries), len(directory), offset, 0)
    return b"".join(local) + directory + end


_EGG_HB_SPACES = frozenset({"rapp/1:egg", "rapp/1:rappid", "rapp/1:grail", "rapp/1:seal"})


def _egg_hb(space, b):
    """§5 (rev-17 E-7): Hb, the octet hash, takes only its own tags; any other tag is refused."""
    if not (isinstance(space, str) and space in _EGG_HB_SPACES):
        raise ValueError(f"§5: Hb is used only with the tags {sorted(_EGG_HB_SPACES)}; refused {space!r}")
    return hashlib.sha256(space.encode() + b"\x0a" + b).hexdigest()


def _egg_strict_json(octets):
    """One §4 JSON text (octets) -> value, refusing what rapp.py::_strict_json refuses."""
    if octets.startswith(b"\xef\xbb\xbf"):
        raise ValueError("JSON text starts with a byte-order mark (§4)")
    text = octets.decode("utf-8")

    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError(f"duplicate JSON member: {k}")
            out[k] = v
        return out

    def constant(token):
        raise ValueError(f"{token} is not a JSON number (§4 (c))")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_float=_egg_json_number,
                           parse_int=_egg_json_int, parse_constant=constant)
    except RecursionError:
        raise ValueError("JSON nesting depth exceeds 64 (§4 (d))") from None
    stack = [(value, 1)]
    while stack:
        cur, depth = stack.pop()
        for item in cur.values() if isinstance(cur, dict) else cur if isinstance(cur, list) else ():
            if isinstance(item, (dict, list)):
                if depth + 1 > 64:
                    raise ValueError("JSON nesting depth exceeds 64 (§4 (d))")
                stack.append((item, depth + 1))
    if len(_egg_canonical(value).encode("utf-8")) > _EGG_MAX_CANONICAL:
        raise ValueError("canonical JSON exceeds the 1 MiB ceiling (§4 (d))")
    return value


def _now_iso_ms():
    from datetime import datetime,timezone
    n=datetime.now(timezone.utc); return n.strftime("%Y-%m-%dT%H:%M:%S.")+f"{n.microsecond//1000:03d}Z"
class _EggCollector:
    def __init__(self): self.files={}; self.meta={}
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def writestr(self,name,data):
        import json as _j
        o=data.encode("utf-8") if isinstance(data,str) else data
        if name=="manifest.json": self.meta=_j.loads(o); return
        self.files[name]=o
    def write(self,fn,arc):
        with open(fn,"rb") as _f: self.files[arc]=_f.read()
def _pack_v9(variant,rappid,created,files,payload):
    # §9.3 Producer (rapp.py::pack_egg): refuse every egg a consumer refuses; unsigned organism/rapplication only.
    if variant not in ("organism","rapplication"): raise ValueError(f"unknown variant: {variant!r}")
    if not _canon_match(rappid): raise ValueError(f"egg rappid is not a §6.1 rappid: {rappid!r}")
    if not isinstance(payload,dict): raise ValueError("egg payload MUST be an object")
    _egg_names_ok(payload)
    for p,o in files.items():
        if not _egg_path_valid(p): raise ValueError(f"egg path violates the §9.1 path grammar: {p!r}")
        if not isinstance(o,bytes): raise ValueError(f"egg file octets MUST be bytes: {p!r}")
    if "manifest.json" in files: raise ValueError("egg contents MUST NOT hold the root path manifest.json")
    need={"rappid.json","soul.md"} if variant=="organism" else {"rappid.json"}
    if not need<=set(files): raise ValueError(f"§9.2: a {variant} egg MUST hold {sorted(need)}")
    ident=_egg_strict_json(files["rappid.json"])
    if not isinstance(ident,dict) or ident.get("schema","rapp/1")!="rapp/1" or ident.get("rappid")!=rappid:
        raise ValueError("§9.2: rappid.json MUST be an object (schema rapp/1 when present) naming the egg's rappid")
    if variant=="rapplication" and [n for n in files if "/" not in n and n.endswith(".py")]!=["agent.py"]:
        raise ValueError("§9.2: a rapplication MUST have exactly one root agent.py")
    contents=sorted(({"path":p,"hash":_egg_hb("rapp/1:egg",o)} for p,o in files.items()),
                    key=lambda c:c["path"].encode("utf-8"))
    manifest={"schema":EGG_SCHEMA,"variant":variant,"rappid":rappid,"created_utc":created,
              "contents":contents,"payload":payload,"sig":None}
    man=_egg_canonical(manifest).encode("utf-8")
    if len(man)>_EGG_MAX_CANONICAL: raise ValueError("canonical manifest exceeds the 1 MiB ceiling (§4 (d))")
    return _egg_zip_pack([("manifest.json",man)]+[(c["path"],files[c["path"]]) for c in contents])
def _finalize_egg(z,variant):
    import json as _j
    meta=z.meta; rid=meta.get("rappid"); files=dict(z.files)
    if variant=="rapplication":
        cand=next((n for n in sorted(files) if n.endswith("_agent.py") or n.split("/")[-1]=="agent.py"),None)
        if cand: files["agent.py"]=files.pop(cand)
        for n in [n for n in list(files) if "/" not in n and n.endswith(".py") and n!="agent.py"]:
            files["src/"+n]=files.pop(n)
        if "agent.py" not in files: variant="organism"
    if variant=="organism": files.setdefault("soul.md",b"# soul\n")
    if not _canon_match(rid):
        # A legacy or missing identity cannot travel in a rapp/1-egg (§6.1): derive the stand-in BEFORE
        # rappid.json names the egg's identity, so the two agree (§9.2).
        import hashlib as _h
        content=b"".join(files[k] for k in sorted(files))
        slug=_canon_label(str(meta.get("name") or meta.get("slug") or "thing"),"thing")
        rid=f"rappid:@kody-w/{slug}:"+_egg_hb("rapp/1:rappid",_h.sha256(content).digest())
    if "rappid.json" not in files:
        files["rappid.json"]=(_j.dumps({"schema":"rapp/1","rappid":rid,"parent_rappid":meta.get("parent_rappid"),"kind":variant},indent=2)+"\n").encode()
    payload={k:v for k,v in meta.items() if k not in ("schema","type","rappid","exported_at","created_at","created_utc")}
    return _pack_v9(variant,rid,_now_iso_ms(),files,payload)



# ── RAPPID — perpetual, globally-unique digital identity ────────────────
#
# Every twin, rapp, and swarm has a RAPPID generated ONCE at first hatch.
# The same identity travels inside every egg the entity ever produces, so
# regardless of which brainstem hosts the organism — the original, a
# backup, a clone on a friend's laptop, a re-hatch ten years from now —
# anyone can verify "this is that twin." The host is mortal. The RAPPID
# is not.
#
# Format (canonical RAPP, spec §6.1):  rappid:@<owner>/<slug>:<64-hex>
#   owner   GitHub-style handle without the @, e.g. kody-w
#   slug    lowercase [a-z0-9] with internal hyphens (no underscores)
#   tail    64 hex — the keyless mint Hb("rapp/1:rappid", uuid4_bytes),
#           domain separated; NEVER a hash of the name (spec §6.2)
# An organism's kind (twin/rapp/swarm) lives in its manifest, not the string.
#
# Storage: .brainstem_data/identity.json
#   { "twin": "rappid:@kody-w/personal:<64-hex>",
#     "rapps": {"kanban": "rappid:@kody-w/kanban:<64-hex>"} }
#
# A snapshot egg packs identity.json so the destination brainstem inherits
# the source's RAPPIDs. Re-hatching ≠ new identity.

_IDENTITY_FILE = os.path.join(_DATA_DIR, "identity.json")
# Canonical RAPP grammar (spec §6.1): rappid:@<owner>/<slug>:<64-hex>.
_CANON_RAPPID_RE = re.compile(
    r"^rappid:@([a-z0-9]+(?:-[a-z0-9]+)*)/([a-z0-9]+(?:-[a-z0-9]+)*):([0-9a-f]{64})$")
# Legacy pre-RAPP form (rappid:<type>:@pub/slug:<16-hex>). Retained ONLY so a
# brainstem that already stored a legacy identity is still recognized and NOT
# re-minted (which would lose its identity). New mints are always canonical.
_LABEL_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_LEGACY_RAPPID_RE = re.compile(r"^rappid:(twin|rapp|swarm):(@[\w-]+)/([\w-]+):([0-9a-f]{16})$")


def _is_known_rappid(s: str) -> bool:
    """True if `s` is a rappid we recognize — canonical (preferred) or a
    legacy stored form we must not clobber."""
    return bool(isinstance(s, str) and (_CANON_RAPPID_RE.match(s) or _LEGACY_RAPPID_RE.match(s)))


def _canon_match(s):
    """The §6.1 match of the WHOLE string `s` (owner 1-39, slug 1-100
    characters), or None. `re.match` with `$` also accepts a trailing newline."""
    m = _CANON_RAPPID_RE.fullmatch(s) if isinstance(s, str) else None
    return m if m and len(m.group(1)) <= 39 and len(m.group(2)) <= 100 else None


def _is_label(s, longest: int) -> bool:
    return isinstance(s, str) and bool(_LABEL_RE.fullmatch(s)) and len(s) <= longest


def _read_identity() -> dict:
    if not os.path.exists(_IDENTITY_FILE):
        return {"twin": None, "rapps": {}}
    try:
        with open(_IDENTITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"twin": None, "rapps": {}}
        data.setdefault("twin", None)
        data.setdefault("rapps", {})
        return data
    except Exception:
        return {"twin": None, "rapps": {}}


def _write_identity(data: dict) -> None:
    os.makedirs(os.path.dirname(_IDENTITY_FILE), exist_ok=True)
    with open(_IDENTITY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _canon_label(s: str, fallback: str, longest: int = 100) -> str:
    """Coerce to a canonical §6.1 label: lowercase [a-z0-9] with single
    internal hyphens, no leading/trailing/double hyphens, no underscores,
    at most `longest` characters (owner 39, slug 100)."""
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return re.sub(r"-+", "-", s)[:longest].strip("-") or fallback


def _make_rappid(type_: str, publisher: str, slug: str) -> str:
    """Mint a fresh canonical rappid (spec §6.2, keyless). Called ONCE per
    organism, ever.

    The tail is ``Hb("rapp/1:rappid", uuid4_bytes)`` — 64 hex, domain
    separated — never a hash of the name. ``type_`` (twin/rapp/swarm) is the
    caller's bookkeeping only; an organism's kind lives in its manifest, not
    inside the rappid string, so it is not encoded here.

    ``publisher`` ("@owner" or "owner") and ``slug`` must already be §6.1
    labels (owner 1-39, slug 1-100 characters): anything else is refused,
    never renamed (rapp.py::mint_rappid). The get_or_create_* entry points
    derive those labels from free-form names first (``_canon_label``)."""
    pub = publisher[1:] if isinstance(publisher, str) and publisher.startswith("@") else publisher
    if not (_is_label(pub, 39) and _is_label(slug, 100)):
        raise ValueError("owner or slug violates the RAPPID grammar (spec §6.1)")
    tail = hashlib.sha256(b"rapp/1:rappid" + b"\x0a" + uuid.uuid4().bytes).hexdigest()
    return f"rappid:@{pub}/{slug}:{tail}"


def get_or_create_twin_rappid(publisher: str = "@anon",
                              slug: str = "personal") -> str:
    """Return this brainstem's twin RAPPID, minting one on first call."""
    ident = _read_identity()
    if ident.get("twin") and _is_known_rappid(ident["twin"]):
        return ident["twin"]
    new = _make_rappid("twin", _canon_label(publisher.lstrip("@"), "anon", 39),
                       _canon_label(slug, "unnamed"))
    ident["twin"] = new
    _write_identity(ident)
    return new


def get_or_create_rapp_rappid(rapp_id: str, publisher: str = "@anon") -> str:
    """Return a rapp's RAPPID, minting one on first call. Per-rapp scope."""
    ident = _read_identity()
    rapps = ident.setdefault("rapps", {})
    if rapps.get(rapp_id) and _is_known_rappid(rapps[rapp_id]):
        return rapps[rapp_id]
    new = _make_rappid("rapp", _canon_label(publisher.lstrip("@"), "anon", 39),
                       _canon_label(rapp_id, "unnamed"))
    rapps[rapp_id] = new
    _write_identity(ident)
    return new


def parse_rappid(rappid: str) -> Optional[dict]:
    """Decompose a rappid string into its components, or None if invalid.

    Accepts the canonical form (spec §6.1) and, for back-compat, the legacy
    ``rappid:<type>:@pub/slug:<16-hex>`` form. `hash` is the identity tail;
    `type` is None for canonical rappids (kind lives in the manifest)."""
    if not isinstance(rappid, str):
        return None
    m = _canon_match(rappid)
    if m:
        return {
            "type":      None,
            "publisher": "@" + m.group(1),
            "slug":      m.group(2),
            "hash":      m.group(3),
            "entropy":   m.group(3),   # compat alias
            "rappid":    rappid,
        }
    m = _LEGACY_RAPPID_RE.match(rappid)
    if not m:
        return None
    return {
        "type":      m.group(1),
        "publisher": m.group(2),
        "slug":      m.group(3),
        "hash":      m.group(4),
        "entropy":   m.group(4),
        "rappid":    rappid,
    }

# Filenames / paths that NEVER enter an egg, regardless of type
_NEVER_PACK = (
    ".copilot_token",
    ".copilot_session",
    "voice.zip",
    ".DS_Store",
    "Thumbs.db",
    # stream.json is the per-incarnation identifier — when a twin egg is
    # summoned onto a new brainstem, the new brainstem mints its OWN
    # stream_id but inherits the source's RAPPID. That's what makes
    # parallel-omniscience clear: same twin, attributable streams.
    "stream.json",
)
_NEVER_PACK_DIRS = (
    "venv",
    "__pycache__",
    ".pytest_cache",
    "private",  # .brainstem_data/private/
)

# Agent files that ship as part of the brainstem core (not user-installed
# skills) and should not be re-packed in a snapshot — the destination
# brainstem already has them.
_CORE_AGENT_FILES = ("basic_agent.py",)


# ── Path safety ─────────────────────────────────────────────────────────

def _safe_join(base: str, rel: str) -> Optional[str]:
    """Return abs path under `base`, or None on traversal attempt."""
    if not rel or ".." in rel.split("/") or os.path.isabs(rel):
        return None
    target = os.path.abspath(os.path.join(base, rel))
    if not target.startswith(os.path.abspath(base) + os.sep) and target != os.path.abspath(base):
        return None
    return target


def _is_excluded(path_inside_brainstem: str) -> bool:
    """Skip secrets, environment artifacts, OS noise, private namespace."""
    parts = path_inside_brainstem.replace("\\", "/").split("/")
    if any(p in _NEVER_PACK for p in parts):
        return True
    if any(p in _NEVER_PACK_DIRS for p in parts):
        return True
    return False


# ── Pack helpers ────────────────────────────────────────────────────────

def _add_tree(z: zipfile.ZipFile, src_root: str, arcname_prefix: str,
              file_filter=None) -> int:
    """Recursively add src_root → arcname_prefix/<rel>. Returns file count."""
    if not os.path.isdir(src_root):
        return 0
    n = 0
    for root, _dirs, files in os.walk(src_root):
        # prune excluded directories so we don't even enter them
        _dirs[:] = [d for d in _dirs if not _is_excluded(d)]
        for fname in files:
            full = os.path.join(root, fname)
            rel_to_root = os.path.relpath(full, src_root).replace(os.sep, "/")
            if _is_excluded(rel_to_root) or _is_excluded(fname):
                continue
            if file_filter and not file_filter(rel_to_root):
                continue
            arcname = f"{arcname_prefix}/{rel_to_root}" if arcname_prefix else rel_to_root
            z.write(full, arcname)
            n += 1
    return n


def _bytes_size_kb(blob: bytes) -> float:
    return round(len(blob) / 1024, 1)


# ── Pack: rapplication ──────────────────────────────────────────────────

def pack_rapplication(rapp_id: str, agent_filename: str,
                      service_filename: Optional[str] = None,
                      ui_filename: Optional[str] = None,
                      version: str = "?", name: Optional[str] = None,
                      publisher: str = "@anon",
                      parent_rappid: Optional[str] = None) -> bytes:
    """Pack a single installed rapplication into an egg."""
    rappid = get_or_create_rapp_rappid(rapp_id, publisher=publisher)
    buf = io.BytesIO()
    with _EggCollector() as z:
        # agent.py
        if agent_filename:
            agent_path = os.path.join(_AGENTS_DIR, agent_filename)
            if os.path.exists(agent_path):
                z.write(agent_path, f"agents/{agent_filename}")

        # service.py (optional)
        if service_filename:
            svc_path = os.path.join(_SERVICES_DIR, service_filename)
            if os.path.exists(svc_path):
                z.write(svc_path, f"services/{service_filename}")

        # ui bundle (optional)
        ui_dir = os.path.join(_UI_BASE_DIR, rapp_id)
        ui_count = _add_tree(z, ui_dir, f"rapp_ui/{rapp_id}")

        # state cartridge (optional) — .brainstem_data/<rapp_id>/...
        state_dir = os.path.join(_DATA_DIR, rapp_id)
        state_count = _add_tree(z, state_dir, f"data/{rapp_id}")

        manifest = {
            "schema": EGG_SCHEMA_V2,
            "type": "rapplication",
            "rappid": rappid,
            "id": rapp_id,
            "name": name or rapp_id,
            "version": version,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent_filename": agent_filename,
            "service_filename": service_filename,
            "ui_filename": ui_filename,
            "ui_file_count": ui_count,
            "state_file_count": state_count,
            "lineage": {
                "publisher": publisher,
                "parent_rappid": parent_rappid,
                "hatched_on": "rapp-brainstem",
            },
        }
        z.writestr("manifest.json", json.dumps(manifest, indent=2))

    return _finalize_egg(z, "organism" if manifest.get("type") in ("twin","organism",None) else "rapplication")


# ── Pack: twin ──────────────────────────────────────────────────────────
# A twin is the user-as-digital-organism: every installed agent + the
# cross-agent shared state (memory, chat tabs, soul) but NOT per-rapp
# state cartridges or rapp UI bundles. That's the "self" without the
# tooling. For tooling-included, use snapshot.

def pack_twin(twin_id: str, name: Optional[str] = None,
              publisher: str = "@anon",
              parent_rappid: Optional[str] = None) -> bytes:
    """Pack the brainstem's agent set + cross-agent state into a twin egg.

    The twin's RAPPID is read (or minted on first call) from
    .brainstem_data/identity.json and embedded in the manifest. The
    same RAPPID is preserved across every twin egg this brainstem
    ever exports — so any future hatch traces back to this lineage.
    """
    rappid = get_or_create_twin_rappid(publisher=publisher, slug=twin_id)
    # Track incarnation count per RAPPID so the manifest carries lineage depth
    ident = _read_identity()
    incarnations = int(ident.get("twin_incarnations", 0)) + 1
    ident["twin_incarnations"] = incarnations
    _write_identity(ident)

    buf = io.BytesIO()
    agent_count = 0
    state_count = 0
    with _EggCollector() as z:
        # All user-installed agents (skip core)
        if os.path.isdir(_AGENTS_DIR):
            for fname in sorted(os.listdir(_AGENTS_DIR)):
                if fname in _CORE_AGENT_FILES:
                    continue
                if not fname.endswith(".py"):
                    continue
                full = os.path.join(_AGENTS_DIR, fname)
                if os.path.isfile(full):
                    z.write(full, f"agents/{fname}")
                    agent_count += 1

        # Cross-agent state — top-level files in .brainstem_data/
        # (not subdirectories, which are per-rapp state cartridges)
        if os.path.isdir(_DATA_DIR):
            for fname in sorted(os.listdir(_DATA_DIR)):
                full = os.path.join(_DATA_DIR, fname)
                if not os.path.isfile(full):
                    continue
                if _is_excluded(fname):
                    continue
                z.write(full, f"data/{fname}")
                state_count += 1

        manifest = {
            "schema": EGG_SCHEMA_V2,
            "type": "twin",
            "rappid": rappid,
            "id": twin_id,
            "name": name or twin_id,
            "version": "1.0.0",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent_count": agent_count,
            "state_file_count": state_count,
            "lineage": {
                "publisher": publisher,
                "parent_rappid": parent_rappid,
                "hatched_on": "rapp-brainstem",
                "incarnations": incarnations,
            },
        }
        z.writestr("manifest.json", json.dumps(manifest, indent=2))

    return _finalize_egg(z, "organism" if manifest.get("type") in ("twin","organism",None) else "rapplication")


# ── Pack: snapshot ──────────────────────────────────────────────────────
# Snapshot is a full dump: every agent, every service, every rapp UI,
# every state cartridge. The destination brainstem becomes a clone
# (modulo secrets and env).

def pack_snapshot(snapshot_id: str, name: Optional[str] = None,
                  publisher: str = "@anon",
                  parent_rappid: Optional[str] = None) -> bytes:
    """Pack the entire brainstem (sans secrets/env) into a snapshot egg.

    A snapshot carries the source brainstem's identity.json — so when the
    destination unpacks it, the destination INHERITS the source's twin
    RAPPID and rapp RAPPIDs. Re-hatching does not mint a new identity.
    """
    twin_rappid = get_or_create_twin_rappid(publisher=publisher, slug=snapshot_id)
    buf = io.BytesIO()
    counts = {"agents": 0, "services": 0, "ui": 0, "data": 0}
    with _EggCollector() as z:
        # All agents (incl. core — destination might not have them)
        if os.path.isdir(_AGENTS_DIR):
            for fname in sorted(os.listdir(_AGENTS_DIR)):
                if not fname.endswith(".py"):
                    continue
                full = os.path.join(_AGENTS_DIR, fname)
                if os.path.isfile(full):
                    z.write(full, f"agents/{fname}")
                    counts["agents"] += 1

        # All services
        if os.path.isdir(_SERVICES_DIR):
            for fname in sorted(os.listdir(_SERVICES_DIR)):
                if not fname.endswith(".py"):
                    continue
                full = os.path.join(_SERVICES_DIR, fname)
                if os.path.isfile(full):
                    z.write(full, f"services/{fname}")
                    counts["services"] += 1

        # All rapp UI bundles
        counts["ui"] = _add_tree(z, _UI_BASE_DIR, "rapp_ui")

        # All .brainstem_data — recursively, with exclusions
        counts["data"] = _add_tree(z, _DATA_DIR, "data",
                                   file_filter=lambda rel: not _is_excluded(rel))

        manifest = {
            "schema": EGG_SCHEMA_V2,
            "type": "snapshot",
            "rappid": twin_rappid,
            "id": snapshot_id,
            "name": name or snapshot_id,
            "version": "1.0.0",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent_count": counts["agents"],
            "service_count": counts["services"],
            "ui_file_count": counts["ui"],
            "state_file_count": counts["data"],
            "lineage": {
                "publisher": publisher,
                "parent_rappid": parent_rappid,
                "hatched_on": "rapp-brainstem",
            },
        }
        z.writestr("manifest.json", json.dumps(manifest, indent=2))

    return _finalize_egg(z, "organism" if manifest.get("type") in ("twin","organism",None) else "rapplication")


# ── Unpack ──────────────────────────────────────────────────────────────

def is_egg_blob(blob: bytes) -> bool:
    """Cheap check — does this look like an egg (zip with manifest.json)?"""
    if len(blob) < 4 or blob[:4] != b"PK\x03\x04":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return "manifest.json" in z.namelist()
    except Exception:
        return False


def unpack(blob: bytes, mode: str = "merge") -> dict:
    """Extract an egg's contents to the brainstem.

    mode:
      - "merge"   : add files; existing files are overwritten (default)
      - "replace" : (snapshot/twin only) caller is responsible for
                    pre-emptively clearing destination dirs

    Returns a result dict: {ok, type, id, files_restored, ...}.
    """
    if not is_egg_blob(blob):
        return {"ok": False, "error": "not a valid egg (no manifest.json)"}

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except Exception as e:
            return {"ok": False, "error": f"invalid manifest.json: {e}"}

        schema = manifest.get("schema", "")
        egg_type = manifest.get("type", "")
        rapp_id = manifest.get("id", "")

        if schema == EGG_SCHEMA_V1:
            # Legacy single-rapp eggs from the old binder format.
            return _unpack_v1_legacy(z, manifest)

        if schema != EGG_SCHEMA_V2:
            return {"ok": False, "error": f"unsupported schema: {schema!r}"}

        if egg_type not in ("rapplication", "twin", "snapshot", "swarm"):
            return {"ok": False, "error": f"unknown type: {egg_type!r}"}

        return _unpack_v2(z, manifest, mode)


def _unpack_v2(z: zipfile.ZipFile, manifest: dict, mode: str) -> dict:
    """v2.0 unpacker — generic file-tree extraction with destination map."""
    # Map src-tree-prefix → destination root on the local brainstem
    DEST_MAP = {
        "agents/":   _AGENTS_DIR,
        "services/": _SERVICES_DIR,
        "rapp_ui/":  _UI_BASE_DIR,
        "data/":     _DATA_DIR,
    }
    counts = {"agents": 0, "services": 0, "ui": 0, "data": 0, "skipped": 0}
    errors = []

    for name in z.namelist():
        if name == "manifest.json" or name.endswith("/"):
            continue

        # Find which dest tree this file belongs to
        matched = None
        for prefix, dest_root in DEST_MAP.items():
            if name.startswith(prefix):
                matched = (prefix, dest_root, name[len(prefix):])
                break
        if not matched:
            counts["skipped"] += 1
            continue
        prefix, dest_root, rel = matched

        # Path-traversal guard
        if _is_excluded(rel):
            counts["skipped"] += 1
            continue
        target = _safe_join(dest_root, rel)
        if not target:
            errors.append(f"path-traversal blocked: {name}")
            continue

        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            with open(target, "wb") as f:
                f.write(z.read(name))
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue

        if   prefix == "agents/":   counts["agents"] += 1
        elif prefix == "services/": counts["services"] += 1
        elif prefix == "rapp_ui/":  counts["ui"] += 1
        elif prefix == "data/":     counts["data"] += 1

    return {
        "ok": True,
        "schema": manifest.get("schema"),
        "type": manifest.get("type"),
        "id": manifest.get("id"),
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "agent_filename": manifest.get("agent_filename"),
        "service_filename": manifest.get("service_filename"),
        "ui_filename": manifest.get("ui_filename"),
        "files_restored": counts,
        "errors": errors,
        "manifest": manifest,
    }


def _unpack_v1_legacy(z: zipfile.ZipFile, manifest: dict) -> dict:
    """Legacy `rapp-egg/1.0` unpacker — the original binder format.

    v1 eggs stored a single rapp at fixed paths: agent.py, service.py,
    ui/*, state/*. The manifest carries the destination filenames.
    Preserved verbatim so old eggs round-trip without conversion.
    """
    if manifest.get("type") != "rapplication":
        return {"ok": False, "error": f"v1 egg type must be rapplication, got {manifest.get('type')!r}"}
    rapp_id = manifest.get("id")
    if not rapp_id:
        return {"ok": False, "error": "v1 manifest missing id"}

    agent_fn = manifest.get("agent_filename")
    svc_fn = manifest.get("service_filename")
    ui_fn = manifest.get("ui_filename")
    counts = {"agents": 0, "services": 0, "ui": 0, "data": 0, "skipped": 0}

    names = z.namelist()
    if "agent.py" in names and agent_fn:
        os.makedirs(_AGENTS_DIR, exist_ok=True)
        with open(os.path.join(_AGENTS_DIR, agent_fn), "wb") as f:
            f.write(z.read("agent.py"))
        counts["agents"] += 1

    if "service.py" in names and svc_fn:
        os.makedirs(_SERVICES_DIR, exist_ok=True)
        with open(os.path.join(_SERVICES_DIR, svc_fn), "wb") as f:
            f.write(z.read("service.py"))
        counts["services"] += 1

    rapp_ui_dir = os.path.join(_UI_BASE_DIR, rapp_id)
    for n in names:
        if not n.startswith("ui/") or n.endswith("/"):
            continue
        rel = n[len("ui/"):]
        target = _safe_join(rapp_ui_dir, rel)
        if not target:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(z.read(n))
        counts["ui"] += 1

    rapp_state_dir = os.path.join(_DATA_DIR, rapp_id)
    for n in names:
        if not n.startswith("state/") or n.endswith("/"):
            continue
        rel = n[len("state/"):]
        target = _safe_join(rapp_state_dir, rel)
        if not target:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(z.read(n))
        counts["data"] += 1

    return {
        "ok": True,
        "schema": EGG_SCHEMA_V1,
        "type": "rapplication",
        "id": rapp_id,
        "agent_filename": agent_fn,
        "service_filename": svc_fn,
        "ui_filename": ui_fn,
        "files_restored": counts,
        "errors": [],
        "manifest": manifest,
    }


# ── Convenience: introspect without unpacking ───────────────────────────

def inspect(blob: bytes) -> dict:
    """Read just the manifest from an egg blob — no extraction."""
    if not is_egg_blob(blob):
        return {"ok": False, "error": "not a valid egg"}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        try:
            return {"ok": True, "manifest": json.loads(z.read("manifest.json"))}
        except Exception as e:
            return {"ok": False, "error": f"invalid manifest: {e}"}


# ── Schema 2.1: variant-repo eggs (universal twin cartridge) ────────────
#
# A variant-repo egg captures the entire local-first twin layout: the
# kernel snapshot at root, the agents dir, utils, installer, content
# files (soul.md, MANIFEST.md, README.md, LICENSE, vbrainstem.html), and
# .brainstem_data state. The egg is self-sufficient — it can materialize
# the twin onto any host with just a kernel runtime, no upstream fetch
# required (though the manifest carries source pointers for verification
# and optional re-sync).
#
# This is the cartridge the user names "rappid.egg" — pack on device A,
# transport, summon on device B with a vanilla brainstem, twin appears.

# Top-level files at the variant-repo root that are part of the organism
# and must travel in the egg. Anything else at root is excluded unless
# explicitly listed.
_REPO_ROOT_FILES = {
    "brainstem.py",       # kernel snapshot
    "rappid.json",        # lineage anchor + brainstem pin
    "soul.md",            # voice
    "MANIFEST.md",        # vision doc
    "README.md",          # public-facing intro
    "LICENSE",            # license posture
    "SUMMON.md",          # summon URL convention
    "TEMPLATE.md",        # template usage doc
    "index.html",         # GitHub Pages landing
    "vbrainstem.html",    # browser simulator
    "summon.svg",         # QR code
    ".gitignore",
}

# Subdirectories at the variant-repo root that travel as full trees.
_REPO_ROOT_DIRS = ("agents", "utils", "installer", "app")

# Path pieces that are NEVER packed (mirror _NEVER_PACK_DIRS but applied
# to the variant-repo tree, not the brainstem-instance tree).
_REPO_NEVER_DIRS = ("__pycache__", ".pytest_cache", "venv", ".git", "node_modules")
_REPO_NEVER_FILES = (".DS_Store", "Thumbs.db", ".env", ".env.local")


def _is_repo_excluded(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in _REPO_NEVER_DIRS for p in parts):
        return True
    if any(p in _REPO_NEVER_FILES for p in parts):
        return True
    if "private" in parts:
        # .brainstem_data/private/ — explicit no-share
        return True
    return False


def _walk_repo_tree(src: str, arc_prefix: str, z: zipfile.ZipFile) -> int:
    """Add every non-excluded file under src to the zip at arc_prefix/. Returns count."""
    if not os.path.isdir(src):
        return 0
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in _REPO_NEVER_DIRS]
        for fn in files:
            if fn in _REPO_NEVER_FILES:
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            if _is_repo_excluded(rel):
                continue
            z.write(full, f"{arc_prefix}/{rel}" if arc_prefix else rel)
            n += 1
    return n


def pack_twin_from_repo(repo_path: str,
                        bundled_repo: bool = True,
                        bundled_state: bool = True,
                        attestation: Optional[dict] = None) -> bytes:
    """Pack a hatched variant repo into a brainstem-egg/2.1 blob.

    Layout produced inside the zip:
        manifest.json                  — schema 2.1, source + brainstem pin
        repo/<rel>                     — the variant-repo tree (if bundled_repo)
        data/<rel>                     — .brainstem_data tree (if bundled_state)

    The repo MUST have rappid.json at its root and SHOULD have brainstem.py
    + an agents/ dir + a utils/ dir. Unbundled fields are recorded in the
    manifest but their tree is omitted (smaller egg, requires online fetch
    on summon — not implemented yet, reserved).
    """
    repo = os.path.abspath(repo_path)
    rappid_json_path = os.path.join(repo, "rappid.json")
    if not os.path.exists(rappid_json_path):
        raise ValueError(f"no rappid.json at {repo} — not a variant repo")

    with open(rappid_json_path, "r", encoding="utf-8") as f:
        rj = json.load(f)

    rappid_uuid = rj.get("rappid")
    if not rappid_uuid:
        raise ValueError("rappid.json has no 'rappid' field")

    bs_block = rj.get("brainstem") or {}

    buf = io.BytesIO()
    with _EggCollector() as z:
        repo_files = 0
        data_files = 0

        if bundled_repo:
            # Top-level files at root
            for fname in _REPO_ROOT_FILES:
                full = os.path.join(repo, fname)
                if os.path.exists(full) and os.path.isfile(full):
                    z.write(full, f"repo/{fname}")
                    repo_files += 1
            # Subdirs as full trees
            for d in _REPO_ROOT_DIRS:
                src = os.path.join(repo, d)
                repo_files += _walk_repo_tree(src, f"repo/{d}", z)

        if bundled_state:
            data_src = os.path.join(repo, ".brainstem_data")
            data_files = _walk_repo_tree(data_src, "data", z)

        manifest = {
            "schema": EGG_SCHEMA_V2_1,
            "type": "twin",
            "rappid": rappid_uuid,  # the source twin's own rappid (§9.2: rappid.json names it), never a fresh sibling
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": {
                "rappid_uuid": rappid_uuid,
                "parent_rappid_uuid": rj.get("parent_rappid"),
                "repo": rj.get("parent_repo"),
                "commit": rj.get("parent_commit"),
                "name": rj.get("name"),
            },
            "brainstem": {
                "version": bs_block.get("version"),
                "source_repo": bs_block.get("source_repo"),
                "source_commit": bs_block.get("source_commit"),
            },
            "bundled_repo": bool(bundled_repo),
            "bundled_state": bool(bundled_state),
            "repo_file_count": repo_files,
            "data_file_count": data_files,
            "attestation": attestation or rj.get("attestation"),
            "size_kb_approx": None,  # filled below
        }

        z.writestr("manifest.json", json.dumps(manifest, indent=2))

    return _finalize_egg(z, "organism")


def summon_twin_egg(blob: bytes, host_root: str,
                    keep_existing_kernel: bool = False) -> str:
    """Materialize a brainstem-egg/2.1 blob into a workspace under host_root.

    Workspace path: <host_root>/<rappid_uuid>/

    The summon flow:
      1. Read manifest, extract rappid_uuid.
      2. Create or reuse <host_root>/<rappid_uuid>/.
      3. Extract repo/ → workspace/.
      4. Extract data/ → workspace/.brainstem_data/.
      5. (if keep_existing_kernel) restore the workspace's previous brainstem.py
         after extraction — used for the egg-based hatching cycle where the
         host already swapped to a newer kernel before summon.

    Returns the workspace absolute path.
    """
    if not is_egg_blob(blob):
        raise ValueError("not a valid egg blob")

    with zipfile.ZipFile(io.BytesIO(blob), "r") as z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except Exception as e:
            raise ValueError(f"invalid egg manifest: {e}")

        schema = manifest.get("schema")
        if schema not in (EGG_SCHEMA_V2_1, EGG_SCHEMA_V2):
            raise ValueError(f"unsupported egg schema for variant summon: {schema}")

        source = manifest.get("source") or {}
        rappid_uuid = source.get("rappid_uuid")
        if not rappid_uuid:
            raise ValueError("egg manifest has no source.rappid_uuid")

        host = os.path.abspath(host_root)
        workspace = os.path.join(host, rappid_uuid)
        os.makedirs(workspace, exist_ok=True)

        # If the caller wants to preserve the workspace's existing kernel
        # (the hatching-cycle usecase), stash it before extraction.
        preserved_kernel: Optional[bytes] = None
        if keep_existing_kernel:
            kpath = os.path.join(workspace, "brainstem.py")
            if os.path.exists(kpath):
                with open(kpath, "rb") as f:
                    preserved_kernel = f.read()

        # Extract repo/ → workspace root
        for name in z.namelist():
            if name.startswith("repo/") and not name.endswith("/"):
                rel = name[len("repo/"):]
                target = _safe_join(workspace, rel)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(name) as src, open(target, "wb") as dst:
                    dst.write(src.read())
            elif name.startswith("data/") and not name.endswith("/"):
                rel = name[len("data/"):]
                target = _safe_join(os.path.join(workspace, ".brainstem_data"), rel)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(name) as src, open(target, "wb") as dst:
                    dst.write(src.read())

        # Restore preserved kernel if requested
        if keep_existing_kernel and preserved_kernel is not None:
            with open(os.path.join(workspace, "brainstem.py"), "wb") as f:
                f.write(preserved_kernel)

        return workspace
