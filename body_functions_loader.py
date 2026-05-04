"""
body_functions_loader.py — additive body_function dispatcher (kernel sibling).

Discovers `*_body_function.py` files under `utils/body_functions/` and
attaches `/api/<name>/<path>` routes to a Flask app. The canonical
kernel does not dispatch body_functions itself — this sibling does it
without modifying the kernel.

Body_function contract (Constitution Article XXXII / XXXIII):
    name: str
    handle(method: str, path: str, body: dict) -> (dict, int)

The contract was previously called "service" with `*_service.py` and
`utils/services/`; both terms refer to the same single-file unit.
This loader supports both suffixes during the rename window for
backward compatibility with older organisms.

Usage (from boot.py or any wrapper):
    import body_functions_loader
    body_functions_loader.install(app)
"""

from __future__ import annotations

import glob
import importlib.util
import os
import sys
import traceback
from typing import Any


_BRAINSTEM_DIR = os.path.dirname(os.path.abspath(__file__))


def _candidate_dirs() -> list[str]:
    """Both new and legacy locations, in priority order."""
    return [
        os.path.join(_BRAINSTEM_DIR, "utils", "body_functions"),
        os.path.join(_BRAINSTEM_DIR, "utils", "services"),
    ]


def _candidate_files(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    files: list[str] = []
    files.extend(glob.glob(os.path.join(directory, "*_body_function.py")))
    files.extend(glob.glob(os.path.join(directory, "*_service.py")))
    return sorted(set(files))


def _import_body_function(filepath: str, idx: int) -> Any | None:
    """Load a body_function module from disk."""
    # Make sure utils/ is importable so body_functions can do `from utils import ...`
    if _BRAINSTEM_DIR not in sys.path:
        sys.path.insert(0, _BRAINSTEM_DIR)

    base = os.path.basename(filepath).replace(".", "_")
    module_name = f"_bf_{idx}_{base}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        print(f"[boot] body_function failed to import {filepath}: {e}")
        traceback.print_exc()
        return None


def _register_routes(app, bf_name: str, bf_handle) -> None:
    """Add /api/<name>, /api/<name>/, and /api/<name>/<path:rest> rules."""
    from flask import request, jsonify

    def make_view(rest_default: str = ""):
        def view(rest: str = rest_default):
            body = request.get_json(silent=True) or {}
            try:
                result, status = bf_handle(request.method, rest, body)
            except Exception as e:
                traceback.print_exc()
                return jsonify({"error": f"body_function {bf_name} crashed: {e}"}), 500
            if isinstance(result, dict) or isinstance(result, list):
                return jsonify(result), status
            return result, status

        return view

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]

    # /api/<name>  (no trailing slash, no path)
    app.add_url_rule(
        f"/api/{bf_name}",
        endpoint=f"_bf_{bf_name}_root",
        view_func=make_view(""),
        methods=methods,
    )
    # /api/<name>/  (trailing slash)
    app.add_url_rule(
        f"/api/{bf_name}/",
        endpoint=f"_bf_{bf_name}_root_slash",
        view_func=make_view(""),
        methods=methods,
    )
    # /api/<name>/<rest>
    app.add_url_rule(
        f"/api/{bf_name}/<path:rest>",
        endpoint=f"_bf_{bf_name}_path",
        view_func=make_view(),
        methods=methods,
    )


def install(app) -> int:
    """Discover body_functions and register their routes on `app`.

    Returns the number of body_functions successfully registered.
    Idempotent — calling install() twice is harmless on a fresh app
    but Flask will refuse duplicate endpoints if the same loader runs
    against the same app twice.
    """
    seen_names: set[str] = set()
    count = 0
    for directory in _candidate_dirs():
        files = _candidate_files(directory)
        if not files:
            continue
        for idx, filepath in enumerate(files):
            module = _import_body_function(filepath, idx)
            if module is None:
                continue
            bf_name = getattr(module, "name", None)
            bf_handle = getattr(module, "handle", None)
            if not bf_name or not callable(bf_handle):
                continue
            if bf_name in seen_names:
                # Newer location wins; skip the legacy duplicate
                continue
            try:
                _register_routes(app, bf_name, bf_handle)
            except Exception as e:
                print(f"[boot] could not register routes for body_function {bf_name}: {e}")
                continue
            seen_names.add(bf_name)
            count += 1
            print(f"[boot] body_function ready: {bf_name} ({os.path.basename(filepath)})")
    print(f"[boot] {count} body_function(s) wired into /api/<name>/...")
    return count
