"""
peer_registry.py — shared local-machine registry of installed brainstems.

A "good neighbor" pattern for multi-brainstem hosts: each install records
its claimed port + brainstem dir at a shared XDG path so subsequent
installs can pick non-conflicting ports, and a running brainstem can
discover its peers without a tree search.

Registry schema (forever-additive, like the /chat envelope):
    {
      "schema": "rapp-peers/1.0",
      "peers": [
        {
          "id":             "<sha256(brainstem_dir)[:12]>",
          "brainstem_dir":  "/abs/path/to/.brainstem/src/rapp_brainstem",
          "port":           7072,
          "is_global":      false,
          "project_name":   "my-project",
          "installed_at":   "2026-04-26T20:30:00Z",
          "version":        "0.12.2"
        }, ...
      ]
    }

The registry is intentionally a passive ledger — entries are appended on
install and read on lookup. Liveness is determined by probing /health, not
by entries getting "registered" and "unregistered" at runtime. This keeps
the data model simple and survives ungraceful brainstem shutdowns.
"""

import hashlib
import json
import os
import time
from typing import Optional


SCHEMA = "rapp-peers/1.1"

# Schema 1.1 (additive over 1.0):
#   New optional fields per peer entry:
#     - rappid_uuid    str | None  — the twin's repo rappid (from rappid.json)
#     - twin_name      str | None  — human label
#     - parent_repo    str | None  — for lineage display
#     - summoned_from  str | None  — egg URL/path the twin came from (None = native install)
#     - summoned_at    str | None  — ISO timestamp of summon (defaults to installed_at)
#     - is_twin_only   bool        — true when the install lives at ~/.rapp/twins/<rappid>/
#
# Old 1.0 ledgers load cleanly via load() with new fields defaulting to None/False.


def registry_path() -> str:
    """XDG-style registry path: $XDG_CONFIG_HOME/rapp/peers.json or ~/.config/rapp/peers.json."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "rapp", "peers.json")


def rapp_home() -> str:
    """Base for twin-only installs: $RAPP_HOME or ~/.rapp/."""
    return os.environ.get("RAPP_HOME") or os.path.join(os.path.expanduser("~"), ".rapp")


def _peer_id(brainstem_dir: str) -> str:
    return hashlib.sha256(os.path.abspath(brainstem_dir).encode()).hexdigest()[:12]


def _project_name(brainstem_dir: str) -> str:
    """Best-effort project label from the path. /Users/x/proj/.brainstem/src/rapp_brainstem → 'proj'.
    The global install at $HOME/.brainstem/... is labeled 'global' instead of $USER.
    Twin-only installs at ~/.rapp/twins/<rappid>/ are labeled by the rappid prefix."""
    if _is_global(brainstem_dir):
        return "global"
    if _is_twin_only(brainstem_dir):
        # Path looks like ~/.rapp/twins/<rappid>/.brainstem/...
        parts = os.path.abspath(brainstem_dir).split(os.sep)
        try:
            i = parts.index("twins")
            if i + 1 < len(parts):
                return f"twin:{parts[i + 1][:8]}"
        except ValueError:
            pass
        return "twin"
    parts = os.path.abspath(brainstem_dir).split(os.sep)
    try:
        bs_idx = parts.index(".brainstem")
        return parts[bs_idx - 1] if bs_idx > 0 else "global"
    except ValueError:
        return os.path.basename(os.path.dirname(brainstem_dir)) or "unknown"


def _is_global(brainstem_dir: str) -> bool:
    """Global install lives directly under $HOME/.brainstem (not project-local)."""
    home = os.path.expanduser("~")
    return os.path.abspath(brainstem_dir).startswith(os.path.join(home, ".brainstem", ""))


def _is_twin_only(brainstem_dir: str) -> bool:
    """Twin-only install lives under $RAPP_HOME/twins/<rappid>/ — a summoned twin
    not bound to any project directory. The third install scope, sibling to
    `is_global` and the implicit 'is_project' (anything else)."""
    home = os.path.expanduser("~")
    rapp = os.environ.get("RAPP_HOME") or os.path.join(home, ".rapp")
    twins_root = os.path.join(rapp, "twins") + os.sep
    return os.path.abspath(brainstem_dir).startswith(twins_root)


def _migrate_entry(p: dict) -> dict:
    """Add 1.1 fields with safe defaults to a 1.0-era entry. In-place."""
    p.setdefault("rappid_uuid", None)
    p.setdefault("twin_name", None)
    p.setdefault("parent_repo", None)
    p.setdefault("summoned_from", None)
    p.setdefault("summoned_at", None)
    if "is_twin_only" not in p:
        bdir = p.get("brainstem_dir") or ""
        p["is_twin_only"] = _is_twin_only(bdir) if bdir else False
    return p


def load() -> dict:
    """Read the registry. Returns the empty registry if missing or unparseable.
    Migrates 1.0 entries to 1.1 shape (additive, in-memory only)."""
    path = registry_path()
    if not os.path.exists(path):
        return {"schema": SCHEMA, "peers": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "peers" not in data:
            return {"schema": SCHEMA, "peers": []}
        data.setdefault("schema", SCHEMA)
        data["peers"] = [_migrate_entry(p) for p in data["peers"] if isinstance(p, dict)]
        return data
    except (json.JSONDecodeError, OSError):
        return {"schema": SCHEMA, "peers": []}


def group_by_twin() -> dict:
    """Return a dict mapping rappid_uuid → list of peer entries that share that
    rappid. Peers without rappid_uuid are skipped (not part of the estate).

    Multiple peers under the same rappid_uuid represent parallel-omniscience
    incarnations — same twin running in multiple scopes (global + project,
    or global + twin-only) on this device.
    """
    data = load()
    grouped: dict = {}
    for p in data["peers"]:
        rid = p.get("rappid_uuid")
        if not rid:
            continue
        grouped.setdefault(rid, []).append(p)
    return grouped


def _save(data: dict) -> None:
    path = registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def upsert(brainstem_dir: str, port: int, version: Optional[str] = None,
           rappid_uuid: Optional[str] = None,
           twin_name: Optional[str] = None,
           parent_repo: Optional[str] = None,
           summoned_from: Optional[str] = None,
           summoned_at: Optional[str] = None) -> dict:
    """Add or update a peer entry. Idempotent — repeat installs at the same dir overwrite cleanly.

    Twin-aware fields (rappid_uuid, twin_name, parent_repo, summoned_from,
    summoned_at) are optional. Leave them None for legacy/native installs.
    """
    abs_dir = os.path.abspath(brainstem_dir)
    pid = _peer_id(abs_dir)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {
        "id": pid,
        "brainstem_dir": abs_dir,
        "port": int(port),
        "is_global": _is_global(abs_dir),
        "is_twin_only": _is_twin_only(abs_dir),
        "project_name": _project_name(abs_dir),
        "installed_at": now,
        "version": version or "",
        # Twin-aware (1.1) — None means "not a summoned twin / legacy install".
        "rappid_uuid": rappid_uuid,
        "twin_name": twin_name,
        "parent_repo": parent_repo,
        "summoned_from": summoned_from,
        "summoned_at": summoned_at or (now if summoned_from else None),
    }
    data = load()
    data["peers"] = [p for p in data["peers"] if p.get("id") != pid]
    data["peers"].append(entry)
    _save(data)
    return entry


def forget(brainstem_dir: str) -> bool:
    """Remove a peer entry. Returns True if anything was removed."""
    pid = _peer_id(brainstem_dir)
    data = load()
    before = len(data["peers"])
    data["peers"] = [p for p in data["peers"] if p.get("id") != pid]
    removed = len(data["peers"]) != before
    if removed:
        _save(data)
    return removed


def claimed_ports() -> set:
    """Set of ports currently claimed by registered peers — for find_free_port to avoid."""
    data = load()
    return {int(p["port"]) for p in data["peers"] if isinstance(p.get("port"), int)}


if __name__ == "__main__":
    # CLI shim so install.sh can shell out for upsert/claimed-ports without
    # rewriting the logic in bash. Usage:
    #   python3 peer_registry.py claimed-ports
    #   python3 peer_registry.py upsert <brainstem_dir> <port> [version]
    #   python3 peer_registry.py forget <brainstem_dir>
    #   python3 peer_registry.py list
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "claimed-ports":
        print(" ".join(str(p) for p in sorted(claimed_ports())))
    elif cmd == "upsert":
        e = upsert(sys.argv[2], int(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else None)
        print(json.dumps(e))
    elif cmd == "forget":
        print("removed" if forget(sys.argv[2]) else "not-found")
    elif cmd == "list":
        print(json.dumps(load(), indent=2))
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)
