"""
boot.py — additive launcher that wraps the canonical kernel.

The canonical brainstem.py is the digital organism's DNA — it boots a
Flask app that serves /chat, /agents, /health, etc. It does NOT
dispatch body_functions, mount /web/ static assets, or add other
local-repo integrations. Per Constitution Article XXXIII, the kernel
stays small and untouched; everything around it is mutable.

This file IS that "everything around it" — a kernel-sibling launcher
that:

  1. Monkey-patches `Flask.run` BEFORE the kernel runs.
  2. Executes the canonical kernel verbatim via runpy (the kernel's
     `if __name__ == "__main__":` block runs unchanged).
  3. The patched `Flask.run` injects body_function dispatch and
     /web/<path> static handling onto the kernel's app right before
     it starts serving.

The kernel itself never imports this file. It does not know boot.py
exists. start.sh / start.ps1 invoke `python boot.py` instead of
`python brainstem.py` — that's the one piece that has to know.

Running the kernel directly (`python brainstem.py`) still works and
gives you the canonical /chat surface without body_functions or the
web mount — exactly what the canonical kernel promises.

Future integrations (senses, twin, frames, index_card, etc.) can be
added here as additional `app.add_url_rule` calls, additional Flask
blueprints, or additional `before_request` hooks — all without ever
touching brainstem.py.
"""

from __future__ import annotations

import os
import runpy
import sys


_HERE = os.path.dirname(os.path.abspath(__file__))


def _wrap_flask_run() -> None:
    """Hook a one-time pre-serve callback into Flask.run."""
    import flask

    _real_run = flask.Flask.run

    def _wrapped_run(self, *args, **kwargs):
        # Last-mile additions, just before the kernel starts serving.
        try:
            sys.path.insert(0, _HERE)
            import body_functions_loader  # kernel sibling
            body_functions_loader.install(self)
        except Exception as e:
            print(f"[boot] body_functions_loader failed: {e}")

        try:
            sys.path.insert(0, _HERE)
            import senses_loader  # kernel sibling
            senses_loader.install(self)
        except Exception as e:
            print(f"[boot] senses_loader failed: {e}")

        try:
            _mount_web_static(self)
        except Exception as e:
            print(f"[boot] /web mount failed: {e}")

        return _real_run(self, *args, **kwargs)

    flask.Flask.run = _wrapped_run


def _mount_web_static(app) -> None:
    """Serve static files from utils/web/ at /web/<path>.

    The canonical kernel's `/` already serves index.html. /web/ is for
    body_function viewers (neighborhood.html, etc.) and any future
    static UI a body_function wants to ship alongside its handler.
    """
    web_dir = os.path.join(_HERE, "utils", "web")
    if not os.path.isdir(web_dir):
        return

    from flask import send_from_directory, abort

    def web_view(rest: str = ""):
        if not rest:
            # /web/ — serve a directory index if present
            index = os.path.join(web_dir, "index.html")
            if os.path.exists(index):
                return send_from_directory(web_dir, "index.html")
            return abort(404)
        # Refuse any path traversal
        full = os.path.normpath(os.path.join(web_dir, rest))
        if not full.startswith(web_dir + os.sep) and full != web_dir:
            return abort(403)
        if not os.path.exists(full) or os.path.isdir(full):
            # Try directory index inside the requested path
            if os.path.isdir(full):
                idx = os.path.join(full, "index.html")
                if os.path.exists(idx):
                    return send_from_directory(os.path.dirname(idx), "index.html")
            return abort(404)
        return send_from_directory(os.path.dirname(full), os.path.basename(full))

    web_view.__name__ = "_boot_web_view"
    app.add_url_rule("/web", endpoint="_boot_web_root", view_func=web_view, methods=["GET"])
    app.add_url_rule("/web/", endpoint="_boot_web_root_slash", view_func=web_view, methods=["GET"])
    app.add_url_rule("/web/<path:rest>", endpoint="_boot_web_path", view_func=web_view, methods=["GET"])
    print(f"[boot] /web mounted from {web_dir}")


def _lineage_guard() -> None:
    """Refuse to boot if rappid.json identity doesn't match git location.

    A template clone that hasn't run initialize-variant.sh carries the
    parent's rappid in a non-parent location — running the brainstem on
    that workspace would corrupt the lineage chain. The guard runs
    before the kernel and any sibling loaders.

    Bypass: RAPP_SKIP_LINEAGE_CHECK=1 (logged, for emergency repair).
    """
    try:
        utils_dir = os.path.join(_HERE, "utils")
        if utils_dir not in sys.path:
            sys.path.insert(0, utils_dir)
        from lineage_check import assert_initialized  # type: ignore
        assert_initialized()
    except SystemExit:
        raise
    except Exception as e:
        print(f"[boot] lineage check skipped: {e}")


def main() -> None:
    _lineage_guard()
    _wrap_flask_run()
    # Run the canonical kernel as if launched directly.
    kernel_path = os.path.join(_HERE, "brainstem.py")
    runpy.run_path(kernel_path, run_name="__main__")


if __name__ == "__main__":
    main()
