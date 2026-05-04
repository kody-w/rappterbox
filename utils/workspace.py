"""
workspace.py — per-rapp file scratchpad helper.

Implements SPEC §11 (kody-w/RAPP_Store). Every installed rapplication gets
a persistent, isolated directory at ${BRAINSTEM_ROOT}/.brainstem_data/workspaces/<id>/
where the user and the rapp can collaborate via files (CSVs, transcripts,
generated outputs, anything that doesn't fit a perform() kwarg).

Singletons access this via:

    from utils.workspace import workspace_dir
    ws = workspace_dir()   # pathlib.Path | None

The helper walks the call stack to find the caller's __manifest__ and
resolves the rapp id from it. Returns None outside a brainstem.

The binder install hooks call ensure_workspace(<id>) directly to mkdir at
install time; the HTTP service uses safe_workspace_path() for guarded
file ops.

Distinct from .brainstem_data/<id>/ (rapp-PRIVATE state, bundled into eggs
as state/...). Workspaces are user-collaborative and intentionally NOT
auto-bundled — large user files don't belong in shippable cartridges.
"""

import inspect
import os
import re
from pathlib import Path
from typing import Optional

# rapp_brainstem/utils/workspace.py → walk three dirnames to brainstem root.
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WORKSPACE_BASE = os.path.join(_BASE_DIR, ".brainstem_data", "workspaces")


def _slug(s: str) -> str:
    """Conservative slug: lowercase, [a-z0-9_] only. Empty if input is unusable."""
    return re.sub(r"[^a-z0-9_]+", "_", str(s).lower()).strip("_")


def _resolve_caller_id() -> Optional[str]:
    """Walk the stack outward looking for a frame whose module defines
    __manifest__ with a usable id/name. Skip our own module frames."""
    for frame_info in inspect.stack()[1:]:
        mod = inspect.getmodule(frame_info.frame)
        if mod is None or mod.__name__ == __name__:
            continue
        manifest = getattr(mod, "__manifest__", None)
        if isinstance(manifest, dict):
            cand = manifest.get("id") or manifest.get("name")
            if cand:
                slug = _slug(cand)
                if slug:
                    return slug
    return None


def workspace_root() -> Path:
    """Base dir that holds all per-rapp workspaces. Used by the HTTP service."""
    return Path(_WORKSPACE_BASE)


def workspace_dir(rapp_id: Optional[str] = None) -> Optional[Path]:
    """Return the calling rapp's workspace dir, creating it if absent.

    If rapp_id is omitted, walks the call stack to find the caller's
    __manifest__ and uses its id (or name). Returns None if no rapp
    identity can be determined — singletons MUST handle that case
    rather than crashing (running outside a brainstem, e.g. tests).
    """
    rid = _slug(rapp_id) if rapp_id else _resolve_caller_id()
    if not rid:
        return None
    p = Path(_WORKSPACE_BASE) / rid
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_workspace(rapp_id: str) -> Optional[Path]:
    """Create-and-return a workspace for the given rapp id. No-op if it
    already exists. Used by the binder's install hook. Returns None for
    invalid ids rather than raising — install paths shouldn't fail just
    because a rapp has a weird name."""
    rid = _slug(rapp_id)
    if not rid:
        return None
    p = Path(_WORKSPACE_BASE) / rid
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_workspace_path(rapp_id: str, name: str) -> Optional[Path]:
    """Resolve `<workspace>/<name>` while rejecting any path traversal.

    `name` is treated as a relative leaf — segments containing '..',
    absolute paths, or anything that would resolve outside the rapp's
    workspace dir return None. The HTTP service relies on this guard.
    """
    rid = _slug(rapp_id)
    if not rid or not name:
        return None
    # Reject obvious traversal up front
    if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
        return None
    base = (Path(_WORKSPACE_BASE) / rid).resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def list_workspace(rapp_id: str) -> list[dict]:
    """List files in the rapp's workspace as [{name, size, mtime}].
    Subdirectories are walked one level so 'subdir/file.txt' surfaces.
    Returns [] if the workspace doesn't exist yet."""
    rid = _slug(rapp_id)
    if not rid:
        return []
    base = Path(_WORKSPACE_BASE) / rid
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(base).as_posix()
            st = p.stat()
            out.append({"name": rel, "size": st.st_size, "mtime": int(st.st_mtime)})
        except OSError:
            continue
    return out
