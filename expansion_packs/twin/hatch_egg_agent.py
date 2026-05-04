"""
hatch_egg_agent.py — drop-in cartridge for hatching .egg cartridges into
fully-viable local twins.

Drop into ~/.brainstem/agents/. Restart brainstem. The LLM gets a tool
called `HatchEgg`. Now in chat:

    User: "I have my dad's twin egg on a USB stick at /Volumes/usb/dad.egg.
           Hatch it on this machine."
    Model: <calls HatchEgg(egg_path="/Volumes/usb/dad.egg")>
    Tool result: "Hatched dad-twin (rappid 7bd3...). Twin lives at
                  ~/.rapp/twins/7bd3.../, registered at port 7081, with
                  state and lineage intact. Identity preserved across
                  the substrate hop. Boot with SOUL_PATH=...
                  ~/.brainstem/start.sh --port 7081."

Per the static-ancestor / game-console model: this cartridge relies ONLY
on the ancestor brainstem.py and its already-loaded utility modules
(specifically the brainstem's vendored utils/egg.py and
utils/peer_registry.py). No network, no clone, no kernel bundle —
the brainstem already IS the kernel; the egg just adds the organism.

A successfully-hatched twin is **fully viable**: its rappid.json is
valid, its soul.md is present, its .brainstem_data state is restored,
and any custom agents from the egg's repo/ payload are placed in the
twin's workspace. Booting the brainstem with SOUL_PATH/AGENTS_PATH
pointed at the workspace runs the twin completely.
"""

from __future__ import annotations

import json
import os
import pathlib
import time

# The brainstem's _register_shims sets this up for us.
from agents.basic_agent import BasicAgent


def _rapp_home() -> str:
    return os.environ.get("RAPP_HOME") or os.path.join(os.path.expanduser("~"), ".rapp")


def _twins_dir() -> str:
    return os.path.join(_rapp_home(), "twins")


def _try_import_egg():
    """The brainstem's vendored egg module — pack/unpack of brainstem-egg/2.x."""
    try:
        from utils import egg  # type: ignore
        return egg
    except ImportError:
        try:
            import egg  # type: ignore
            return egg
        except ImportError:
            return None


def _try_import_peer_registry():
    try:
        from utils import peer_registry  # type: ignore
        return peer_registry
    except ImportError:
        try:
            import peer_registry  # type: ignore
            return peer_registry
        except ImportError:
            return None


TWIN_PORT_LOW, TWIN_PORT_HIGH = 7081, 7200


def _allocate_port(peer_registry) -> int:
    if peer_registry is None:
        return TWIN_PORT_LOW
    try:
        claimed = peer_registry.claimed_ports()
    except Exception:
        claimed = set()
    for port in range(TWIN_PORT_LOW, TWIN_PORT_HIGH):
        if port not in claimed:
            return port
    return 0


def _is_egg(path: pathlib.Path) -> bool:
    """Quick magic-bytes check — eggs are zips."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


class HatchEggAgent(BasicAgent):
    def __init__(self):
        self.name = "HatchEgg"
        self.metadata = {
            "name": self.name,
            "description": (
                "Imports a .egg cartridge file and hatches it into a "
                "fully-viable digital twin on this device. The hatched "
                "twin's identity (rappid), memory (.brainstem_data), and "
                "any local mutations are restored intact. Use when the "
                "user wants to materialize a twin from a backup egg, "
                "from another device's egg, or from a shared egg they "
                "received from someone else. The egg must be a local file "
                "path; download URLs to disk first if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "egg_path": {
                        "type": "string",
                        "description": "Absolute path to the .egg file on "
                                       "this device. Must exist and be a "
                                       "valid zip (egg files are zips with "
                                       "manifest.json + repo/ + data/).",
                    },
                    "keep_existing_kernel": {
                        "type": "boolean",
                        "description": "If true, preserve any brainstem.py "
                                       "already at the workspace path "
                                       "instead of restoring the kernel "
                                       "from the egg. Use for the "
                                       "egg-based hatching cycle (kernel "
                                       "update via egg roundtrip). "
                                       "Default false.",
                    },
                },
                "required": ["egg_path"],
            },
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs) -> str:
        egg_path_str = kwargs.get("egg_path") or ""
        keep_kernel = bool(kwargs.get("keep_existing_kernel"))

        if not egg_path_str:
            return "Error: egg_path is required."

        egg_path = pathlib.Path(egg_path_str).expanduser()
        if not egg_path.exists():
            return f"Error: file not found: {egg_path}"
        if not egg_path.is_file():
            return f"Error: not a file: {egg_path}"
        if not _is_egg(egg_path):
            return f"Error: {egg_path} is not a valid egg cartridge (missing zip magic bytes)."

        egg = _try_import_egg()
        if egg is None:
            return ("Error: the brainstem's egg module is unavailable. "
                    "This usually means the brainstem install is missing "
                    "utils/egg.py — check ~/.brainstem/utils/egg.py and "
                    "reinstall via: curl -fsSL https://kody-w.github.io/"
                    "rapp-installer/install.sh | bash")

        # Read manifest first so we can report back what we're hatching
        try:
            with open(egg_path, "rb") as f:
                blob = f.read()
            inspect = egg.inspect(blob) if hasattr(egg, "inspect") else None
            manifest = (inspect or {}).get("manifest") or {}
        except Exception as e:
            return f"Error: could not read egg: {e}"

        source = manifest.get("source") or {}
        rappid_uuid = source.get("rappid_uuid")
        if not rappid_uuid:
            # Fallback: schema-2.0 eggs have rappid at the top level
            rappid_uuid = manifest.get("rappid") or "<unknown>"

        # Materialize via the brainstem's vendored summon function
        try:
            host_root = _twins_dir()
            os.makedirs(host_root, exist_ok=True)
            workspace_str = egg.summon_twin_egg(
                blob, host_root,
                keep_existing_kernel=keep_kernel,
            )
            workspace = pathlib.Path(workspace_str)
        except Exception as e:
            return f"Error: summon failed: {e}"

        # Read the workspace's rappid.json to confirm identity preserved
        rj_path = workspace / "rappid.json"
        rj_loaded = {}
        if rj_path.exists():
            try:
                rj_loaded = json.loads(rj_path.read_text())
            except json.JSONDecodeError:
                pass
        actual_rappid = rj_loaded.get("rappid") or rappid_uuid
        twin_name = rj_loaded.get("name") or "<unnamed>"

        # Verify viability: required files present
        viability = {
            "rappid.json":  rj_path.exists(),
            "soul.md":      (workspace / "soul.md").exists(),
        }
        missing = [k for k, v in viability.items() if not v]

        # Register in the neighborhood
        peer_registry = _try_import_peer_registry()
        port = _allocate_port(peer_registry)
        registry_status = "skipped (peer_registry unavailable)"
        if peer_registry is not None and not missing:
            try:
                peer_registry.upsert(
                    str(workspace),
                    port,
                    version=(rj_loaded.get("brainstem") or {}).get("version"),
                    rappid_uuid=actual_rappid,
                    twin_name=twin_name,
                    parent_repo=rj_loaded.get("parent_repo"),
                    summoned_from=str(egg_path),
                )
                registry_status = f"registered at port {port}"
            except Exception as e:
                registry_status = f"registry error: {e}"

        if missing:
            return (
                f"Hatched but NOT viable — missing required files: "
                f"{', '.join(missing)}. Workspace at {workspace}. "
                f"Re-pack the egg from a complete twin and try again."
            )

        return (
            f"Hatched twin '{twin_name}' (rappid {actual_rappid}) — fully viable.\n"
            f"  Workspace:    {workspace}\n"
            f"  Source egg:   {egg_path}\n"
            f"  Estate:       {registry_status}\n"
            f"  Boot it:      SOUL_PATH={workspace}/soul.md "
            f"AGENTS_PATH={workspace}/agents ~/.brainstem/start.sh --port {port or '<available>'}\n"
            f"\n"
            f"Identity preserved across the substrate hop. Memory + mutations "
            f"intact. Same rappid as wherever this egg was packed; this is the "
            f"same twin, materialized here."
        )
