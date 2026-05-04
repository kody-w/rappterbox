"""
neighborhood_body_function.py — peer-discovery body_function.

Reads the shared local-machine peer registry (~/.config/rapp/peers.json,
written by install.sh / install.ps1) and probes each entry's /health
endpoint with a short timeout to mark it live or offline. The current
brainstem flags itself via the `is_self` field so the UI can highlight it.

Endpoints (dispatched at /api/neighborhood/*):
    GET    /api/neighborhood/peers       — list peers with live status

The HTML viewer lives at rapp_brainstem/utils/web/neighborhood.html and
is served by the static handler at /web/neighborhood.html.

Why a body_function and not a kernel route: Constitution Article I and
Article XXXIII — the kernel is DNA, body_functions are the dispatchable
musculature that grows around it. Peer discovery is a feature, not DNA.
"""

import json
import os
import urllib.error
import urllib.request


name = "neighborhood"


# File lives at rapp_brainstem/utils/body_functions/neighborhood_body_function.py.
# Three dirname() walks reach the brainstem root (file → body_functions →
# utils → root).
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _peers_payload() -> dict:
    """Build the /api/neighborhood/peers response. Lazy-imports peer_registry
    so a missing helper degrades to an empty list instead of crashing the service."""
    try:
        # peer_registry.py lives in rapp_brainstem/utils/. The brainstem
        # process always runs with cwd=rapp_brainstem (set by start.sh /
        # start.ps1 / the brainstem CLI), so plain `from utils import ...`
        # resolves correctly.
        from utils import peer_registry
    except Exception as e:
        return {
            "schema": "rapp-peers-view/1.0",
            "self_id": "",
            "self_brainstem_dir": _BASE_DIR,
            "peers": [],
            "error": f"peer_registry unavailable: {e}",
        }

    self_id = peer_registry._peer_id(_BASE_DIR)
    data = peer_registry.load()

    peers_out = []
    for p in data.get("peers", []):
        port = int(p.get("port") or 0)
        live = False
        live_version = None
        if port:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/health",
                    headers={"User-Agent": "rapp-peers"},
                )
                with urllib.request.urlopen(req, timeout=1.0) as r:
                    body = r.read().decode("utf-8", errors="replace")
                    try:
                        h = json.loads(body)
                        live = True
                        live_version = h.get("version")
                    except Exception:
                        live = r.status == 200
            except (urllib.error.URLError, OSError, TimeoutError):
                live = False

        peers_out.append({
            "id":            p.get("id"),
            "brainstem_dir": p.get("brainstem_dir"),
            "port":          port,
            "is_global":     bool(p.get("is_global")),
            "project_name":  p.get("project_name") or "",
            "version":       live_version or p.get("version") or "",
            "installed_at":  p.get("installed_at") or "",
            "live":          live,
            "is_self":       p.get("id") == self_id,
        })
    # Self first, then global, then alphabetical by project_name.
    peers_out.sort(key=lambda x: (not x["is_self"], not x["is_global"], x["project_name"].lower()))

    return {
        "schema": "rapp-peers-view/1.0",
        "self_id": self_id,
        "self_brainstem_dir": _BASE_DIR,
        "peers": peers_out,
    }


def handle(method: str, path: str, body: dict):
    """Service entry point — see brainstem.py service_dispatch."""
    if method == "GET" and path in ("peers", "peers/", ""):
        return _peers_payload(), 200
    return {"error": f"unknown route: {method} /api/neighborhood/{path}"}, 404
