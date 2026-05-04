"""
estate_body_function.py — twin-estate operations layer.

The neighborhood (peer_registry) is the data layer: a flat list of
brainstems on this device. The estate is a *view* over that data,
grouped by twin identity (rappid_uuid), with operations to lay/summon/
hatch eggs.

Endpoints (dispatched at /api/estate/*):

    GET  /api/estate/twins   — peers grouped by rappid_uuid
                                (parallel-omniscience incarnations)
    GET  /api/estate/eggs    — local egg backups under $RAPP_HOME/eggs/
    POST /api/estate/lay-egg — pack the current twin to a backup egg
                                body: { repo_path, rappid_uuid? }
    POST /api/estate/summon  — materialize an egg into a twin workspace
                                body: { egg_path | egg_url, host_root?,
                                        keep_existing_kernel? }
    POST /api/estate/hatch   — kernel-update via lay-egg + kernel swap
                                + summon-egg back
                                body: { rappid_uuid, new_kernel_dir }

The HTML viewer lives at utils/web/estate.html.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time

name = "estate"


# Two dirname() walks: file → body_functions/ → utils/
_UTILS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _UTILS_DIR not in sys.path:
    sys.path.insert(0, _UTILS_DIR)


def _import_sibling(modname: str):
    """Lazy-import a sibling module from utils/. Returns None on failure."""
    try:
        return importlib.import_module(modname)
    except Exception:
        return None


def _registry():
    return _import_sibling("peer_registry")


def _egg_module():
    return _import_sibling("egg")


def _rapp_home() -> str:
    return os.environ.get("RAPP_HOME") or os.path.join(os.path.expanduser("~"), ".rapp")


def _eggs_dir() -> str:
    return os.path.join(_rapp_home(), "eggs")


def _twins_dir() -> str:
    return os.path.join(_rapp_home(), "twins")


# ── GET /api/estate/twins ────────────────────────────────────────────────


def _list_twins() -> dict:
    reg = _registry()
    if reg is None:
        return {
            "schema": "rapp-estate-view/1.0",
            "twins": [],
            "error": "peer_registry unavailable",
        }
    grouped = reg.group_by_twin()
    twins = []
    for rappid_uuid, peers in sorted(grouped.items()):
        # Pick a display name: prefer the most-recent twin_name found.
        name_choice = None
        for p in peers:
            if p.get("twin_name"):
                name_choice = p["twin_name"]
                break
        twins.append({
            "rappid_uuid": rappid_uuid,
            "name": name_choice or rappid_uuid[:8],
            "incarnation_count": len(peers),
            "incarnations": [
                {
                    "id": p.get("id"),
                    "brainstem_dir": p.get("brainstem_dir"),
                    "port": p.get("port"),
                    "is_global": p.get("is_global", False),
                    "is_twin_only": p.get("is_twin_only", False),
                    "project_name": p.get("project_name"),
                    "version": p.get("version"),
                    "summoned_from": p.get("summoned_from"),
                    "summoned_at": p.get("summoned_at"),
                }
                for p in peers
            ],
            "parent_repo": next((p.get("parent_repo") for p in peers if p.get("parent_repo")), None),
        })
    return {
        "schema": "rapp-estate-view/1.0",
        "twins": twins,
    }


# ── GET /api/estate/eggs ─────────────────────────────────────────────────


def _list_eggs() -> dict:
    eggs_root = _eggs_dir()
    eggs = []
    if os.path.isdir(eggs_root):
        for rappid_dir in sorted(os.listdir(eggs_root)):
            full = os.path.join(eggs_root, rappid_dir)
            if not os.path.isdir(full):
                continue
            for fn in sorted(os.listdir(full), reverse=True):
                if not fn.endswith(".egg"):
                    continue
                p = os.path.join(full, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                eggs.append({
                    "rappid_uuid": rappid_dir,
                    "filename": fn,
                    "path": p,
                    "size_bytes": st.st_size,
                    "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
                })
    return {
        "schema": "rapp-estate-eggs/1.0",
        "eggs_dir": eggs_root,
        "eggs": eggs,
    }


# ── POST /api/estate/lay-egg ─────────────────────────────────────────────


def _lay_egg(body: dict) -> tuple[dict, int]:
    repo_path = body.get("repo_path")
    if not repo_path:
        return {"error": "missing repo_path"}, 400
    if not os.path.isdir(repo_path):
        return {"error": f"not a directory: {repo_path}"}, 400

    egg = _egg_module()
    if egg is None:
        return {"error": "egg module unavailable"}, 500

    try:
        blob = egg.pack_twin_from_repo(repo_path)
    except Exception as e:
        return {"error": f"pack failed: {e}"}, 500

    try:
        with open(os.path.join(repo_path, "rappid.json")) as f:
            rj = json.load(f)
        rappid_uuid = rj["rappid"]
    except Exception as e:
        return {"error": f"could not read rappid.json: {e}"}, 500

    out_dir = os.path.join(_eggs_dir(), rappid_uuid)
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    out_path = os.path.join(out_dir, f"{ts}.egg")
    with open(out_path, "wb") as f:
        f.write(blob)

    return {
        "ok": True,
        "egg_path": out_path,
        "rappid_uuid": rappid_uuid,
        "size_bytes": len(blob),
    }, 200


# ── POST /api/estate/summon ──────────────────────────────────────────────


def _summon(body: dict) -> tuple[dict, int]:
    egg_path = body.get("egg_path")
    if not egg_path or not os.path.isfile(egg_path):
        return {"error": f"egg_path missing or not a file: {egg_path}"}, 400

    host_root = body.get("host_root") or _twins_dir()
    keep = bool(body.get("keep_existing_kernel"))

    egg = _egg_module()
    if egg is None:
        return {"error": "egg module unavailable"}, 500

    try:
        with open(egg_path, "rb") as f:
            blob = f.read()
        workspace = egg.summon_twin_egg(blob, host_root, keep_existing_kernel=keep)
    except Exception as e:
        return {"error": f"summon failed: {e}"}, 500

    # Optionally register in the neighborhood as a twin-only peer.
    try:
        reg = _registry()
        if reg is not None:
            with open(os.path.join(workspace, "rappid.json")) as f:
                rj = json.load(f)
            # Allocate a port (caller can override later when actually starting).
            claimed = reg.claimed_ports()
            port = next((p for p in range(7081, 7200) if p not in claimed), 0)
            reg.upsert(
                workspace,
                port,
                version=(rj.get("brainstem") or {}).get("version"),
                rappid_uuid=rj["rappid"],
                twin_name=rj.get("name"),
                parent_repo=rj.get("parent_repo"),
                summoned_from=egg_path,
            )
    except Exception:
        pass  # registration is best-effort; summon succeeded

    return {"ok": True, "workspace": workspace}, 200


# ── POST /api/estate/hatch ───────────────────────────────────────────────


def _hatch(body: dict) -> tuple[dict, int]:
    """Egg-based hatching cycle:
       1. Find the twin's workspace by rappid_uuid via the registry
       2. Lay an egg from it (backup)
       3. Copy the new kernel files from new_kernel_dir over the workspace
       4. Summon the egg back with keep_existing_kernel=True
    """
    rappid_uuid = body.get("rappid_uuid")
    new_kernel_dir = body.get("new_kernel_dir")
    if not rappid_uuid or not new_kernel_dir:
        return {"error": "rappid_uuid and new_kernel_dir required"}, 400
    if not os.path.isdir(new_kernel_dir):
        return {"error": f"new_kernel_dir not a directory: {new_kernel_dir}"}, 400

    reg = _registry()
    if reg is None:
        return {"error": "peer_registry unavailable"}, 500
    grouped = reg.group_by_twin()
    peers = grouped.get(rappid_uuid) or []
    if not peers:
        return {"error": f"no peer found for rappid_uuid {rappid_uuid}"}, 404

    # Pick the twin-only incarnation if available, else the first.
    peer = next((p for p in peers if p.get("is_twin_only")), peers[0])
    workspace = peer.get("brainstem_dir")
    if not workspace or not os.path.isdir(workspace):
        return {"error": f"workspace not found: {workspace}"}, 404

    # Step 1: lay an egg
    lay_result, lay_status = _lay_egg({"repo_path": workspace})
    if lay_status != 200:
        return lay_result, lay_status
    egg_path = lay_result["egg_path"]

    # Step 2: copy the new kernel files over (brainstem.py is the kernel surface)
    import shutil
    kernel_files = ("brainstem.py",)
    for kf in kernel_files:
        src = os.path.join(new_kernel_dir, kf)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(workspace, kf))

    # Step 3: summon back with keep_existing_kernel=True
    summon_result, summon_status = _summon({
        "egg_path": egg_path,
        "host_root": os.path.dirname(workspace),
        "keep_existing_kernel": True,
    })
    if summon_status != 200:
        return summon_result, summon_status

    return {
        "ok": True,
        "egg_path": egg_path,
        "workspace": summon_result["workspace"],
        "kernel_swapped_from": new_kernel_dir,
    }, 200


# ── Dispatch ────────────────────────────────────────────────────────────


def handle(method: str, path: str, body: dict):
    """Service entry point — see brainstem.py service_dispatch."""
    p = (path or "").strip("/")
    if method == "GET" and p in ("twins", "twins/", ""):
        return _list_twins(), 200
    if method == "GET" and p in ("eggs", "eggs/"):
        return _list_eggs(), 200
    if method == "POST" and p in ("lay-egg", "lay-egg/"):
        return _lay_egg(body or {})
    if method == "POST" and p in ("summon", "summon/"):
        return _summon(body or {})
    if method == "POST" and p in ("hatch", "hatch/"):
        return _hatch(body or {})
    return {"error": f"unknown route: {method} /api/estate/{p}"}, 404
