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
import hashlib
import io
import json
import os
import re
import secrets
import time
import zipfile
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

# ── RAPPID — perpetual, globally-unique digital identity ────────────────
#
# Every twin, rapp, and swarm has a RAPPID generated ONCE at first hatch.
# The same identity travels inside every egg the entity ever produces, so
# regardless of which brainstem hosts the organism — the original, a
# backup, a clone on a friend's laptop, a re-hatch ten years from now —
# anyone can verify "this is that twin." The host is mortal. The RAPPID
# is not.
#
# Format:  rappid:<type>:<publisher>/<slug>:<entropy>
#   type      twin | rapp | swarm
#   publisher GitHub-style handle, e.g. @kody-w or @rapp
#   slug      human-readable name within the publisher namespace
#   entropy   16 hex chars from secrets.token_hex(8) — irreproducible
#
# Storage: .brainstem_data/identity.json
#   { "twin": "rappid:twin:@kody-w/personal:f7a3b2c1d4e5a8b9",
#     "rapps": {"kanban": "rappid:rapp:@kody-w/kanban:9d8e7f6a5b4c3d2e"} }
#
# A snapshot egg packs identity.json so the destination brainstem inherits
# the source's RAPPIDs. Re-hatching ≠ new identity.

_IDENTITY_FILE = os.path.join(_DATA_DIR, "identity.json")
_RAPPID_RE = re.compile(r"^rappid:(twin|rapp|swarm):(@[\w-]+)/([\w-]+):([0-9a-f]{16})$")


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


def _make_rappid(type_: str, publisher: str, slug: str) -> str:
    """Generate a fresh RAPPID. Called ONCE per organism, ever."""
    if not publisher.startswith("@"):
        publisher = "@" + publisher
    publisher = re.sub(r"[^@\w-]", "", publisher) or "@anon"
    slug = re.sub(r"[^\w-]", "_", slug or "unnamed").strip("_") or "unnamed"
    entropy = secrets.token_hex(8)
    return f"rappid:{type_}:{publisher}/{slug}:{entropy}"


def get_or_create_twin_rappid(publisher: str = "@anon",
                              slug: str = "personal") -> str:
    """Return this brainstem's twin RAPPID, minting one on first call."""
    ident = _read_identity()
    if ident.get("twin") and _RAPPID_RE.match(ident["twin"]):
        return ident["twin"]
    new = _make_rappid("twin", publisher, slug)
    ident["twin"] = new
    _write_identity(ident)
    return new


def get_or_create_rapp_rappid(rapp_id: str, publisher: str = "@anon") -> str:
    """Return a rapp's RAPPID, minting one on first call. Per-rapp scope."""
    ident = _read_identity()
    rapps = ident.setdefault("rapps", {})
    if rapps.get(rapp_id) and _RAPPID_RE.match(rapps[rapp_id]):
        return rapps[rapp_id]
    new = _make_rappid("rapp", publisher, rapp_id)
    rapps[rapp_id] = new
    _write_identity(ident)
    return new


def parse_rappid(rappid: str) -> Optional[dict]:
    """Decompose a RAPPID string into its components, or None if invalid."""
    if not isinstance(rappid, str):
        return None
    m = _RAPPID_RE.match(rappid)
    if not m:
        return None
    return {
        "type":      m.group(1),
        "publisher": m.group(2),
        "slug":      m.group(3),
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
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
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

    return buf.getvalue()


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
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
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

    return buf.getvalue()


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
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
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

    return buf.getvalue()


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
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
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
            "rappid": rj.get("name") and f"rappid:twin:@source/{rj['name']}:{secrets.token_hex(8)}" or None,
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

    blob = buf.getvalue()
    return blob


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
