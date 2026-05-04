"""
Index card — one live artifact per chat turn that factory-style agents
update as they work. The client polls GET /card/<turn_id> and re-renders
the same card as state evolves, so "progress view" and "report" are the
same surface — the card simply stops moving when the run finishes.

Usage inside an agent's perform():

    from utils.index_card import current as card

    card().start(
        title="Executive Brief: AI in supply chain",
        stages=[("research","Research"), ("synth","Synthesis"), ("format","Format")],
    )
    card().stage("research", status="running")
    ... do work ...
    card().stage("research", status="done", note="14 sources")
    card().stage("synth", status="running")
    ...
    card().artifact(kind="brief", title="...", body_md="...")
    card().finish()

Agents never pass the turn_id around — brainstem sets it via a
thread-local before calling perform(), so current() just returns the
card bound to the current turn. If no turn is bound (e.g. the agent is
run outside a /chat request) current() returns a no-op so agents can
be card-aware without crashing in unit tests.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Iterable, Optional, Tuple, Union

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".brainstem_data", "cards")
os.makedirs(_DATA_DIR, exist_ok=True)

_local = threading.local()


def _now() -> float:
    return time.time()


def _path(turn_id: str) -> str:
    safe = "".join(c for c in turn_id if c.isalnum() or c in "-_")
    return os.path.join(_DATA_DIR, f"{safe}.json")


class _NoopCard:
    """Returned from current() when no turn is bound. Silently eats calls."""
    def start(self, *a, **kw): return self
    def stage(self, *a, **kw): return self
    def metric(self, *a, **kw): return self
    def artifact(self, *a, **kw): return self
    def note(self, *a, **kw): return self
    def fail(self, *a, **kw): return self
    def finish(self, *a, **kw): return self
    def read(self): return None


class IndexCard:
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            with open(_path(self.turn_id), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _write(self, data: dict) -> None:
        data["updated_at"] = _now()
        with open(_path(self.turn_id), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def read(self) -> Optional[dict]:
        d = self._read()
        return d or None

    def start(
        self,
        title: str,
        stages: Iterable[Union[str, Tuple[str, str]]],
        *,
        subtitle: Optional[str] = None,
    ) -> "IndexCard":
        """Initialize the card. Stages are either ids ("research") or
        (id, label) tuples. Order is preserved — that's the visual order."""
        norm = []
        for s in stages:
            if isinstance(s, tuple):
                sid, slabel = s[0], s[1]
            else:
                sid = slabel = str(s)
            norm.append({"id": sid, "label": slabel, "status": "pending"})
        with self._lock:
            self._write({
                "turn_id": self.turn_id,
                "title": title,
                "subtitle": subtitle,
                "status": "running",
                "started_at": _now(),
                "stages": norm,
                "metrics": {},
                "artifacts": [],
            })
        return self

    def stage(self, stage_id: str, *, status: Optional[str] = None, note: Optional[str] = None) -> "IndexCard":
        """Update one stage. Valid statuses: pending, running, done, failed."""
        with self._lock:
            d = self._read()
            if not d:
                return self
            for s in d.get("stages", []):
                if s["id"] == stage_id:
                    if status is not None:
                        s["status"] = status
                        if status in ("running", "done", "failed"):
                            s.setdefault("times", {})[status + "_at"] = _now()
                    if note is not None:
                        s["note"] = note
                    break
            self._write(d)
        return self

    def metric(self, key: str, value) -> "IndexCard":
        with self._lock:
            d = self._read()
            if not d:
                return self
            d.setdefault("metrics", {})[key] = value
            self._write(d)
        return self

    def artifact(self, *, kind: str, title: str, body_md: Optional[str] = None, url: Optional[str] = None, meta: Optional[dict] = None) -> "IndexCard":
        with self._lock:
            d = self._read()
            if not d:
                return self
            d.setdefault("artifacts", []).append({
                "kind": kind,
                "title": title,
                "body_md": body_md,
                "url": url,
                "meta": meta or {},
            })
            self._write(d)
        return self

    def note(self, text: str) -> "IndexCard":
        """Freeform note attached to the card header. Shown above stages."""
        with self._lock:
            d = self._read()
            if not d:
                return self
            d["note"] = text
            self._write(d)
        return self

    def fail(self, reason: str) -> "IndexCard":
        with self._lock:
            d = self._read()
            if not d:
                return self
            d["status"] = "failed"
            d["error"] = reason
            d["finished_at"] = _now()
            self._write(d)
        return self

    def finish(self) -> "IndexCard":
        with self._lock:
            d = self._read()
            if not d:
                return self
            d["status"] = "done"
            d["finished_at"] = _now()
            # Any still-running stage gets marked done so the final card
            # isn't stuck with a spinner.
            for s in d.get("stages", []):
                if s.get("status") == "running":
                    s["status"] = "done"
            self._write(d)
        return self


def bind(turn_id: str) -> IndexCard:
    """Called by brainstem before agent.perform(). Binds the card to this thread."""
    card = IndexCard(turn_id)
    _local.card = card
    return card


def unbind() -> None:
    _local.card = None


def current() -> Union[IndexCard, _NoopCard]:
    """Agents call this to get the card for the current turn."""
    return getattr(_local, "card", None) or _NoopCard()


def read_by_turn(turn_id: str) -> Optional[dict]:
    """Used by the /card/<turn_id> endpoint — no binding needed."""
    try:
        with open(_path(turn_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
