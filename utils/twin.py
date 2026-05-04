"""
rapp_brainstem/twin.py — Digital twin calibration helpers.

The v0 twin emits hints inside |||TWIN|||. Calibration (v1) lets the twin
predict in turn N and self-judge in turn N+1 against what the user
actually did — autonomously building fidelity without any explicit teach
step from the user.

Wire shape (everything is inside the existing |||TWIN||| block):

    <probe id="t-4711" kind="priority-claim" subject="PR#123" confidence="0.7"/>
    <calibration id="t-4711" outcome="validated" note="user merged PR#123"/>

Both tags are stripped before the twin block is rendered to the user.
Events are appended to `<root>/.twin_calibration.jsonl` and a rolling
accuracy summary (~200 tokens) is injected into the next turn's system
prompt so the twin's confidence drifts toward reality over time.

Stdlib only. Single-file. Safe to vendor into rapp_swarm/_vendored/.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

LOG_FILENAME = ".twin_calibration.jsonl"

# In-band vocabulary — all authored by the twin, all live INSIDE |||TWIN|||.
# |||TWIN||| is the twin's entire real estate. Nothing twin-related leaks
# outside its block — telemetry, probes, and calibrations are all tags the
# server strips before anyone reads the twin panel.
_PROBE_RE       = re.compile(r"<probe\s+([^/>]*?)\s*/>", re.IGNORECASE)
_CALIBRATION_RE = re.compile(r"<calibration\s+([^/>]*?)\s*/>", re.IGNORECASE)
_TELEMETRY_RE   = re.compile(r"<telemetry\s*>(.*?)</telemetry\s*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE        = re.compile(r"""(\w+)\s*=\s*["']([^"']*)["']""")


def _parse_attrs(s: str) -> dict:
    return {k.lower(): v for k, v in _ATTR_RE.findall(s)}


def parse_twin_tags(twin_text: str):
    """Extract <probe/>, <calibration/>, and <telemetry>…</telemetry> blocks
    from a twin block. Returns (cleaned_text, probes, calibrations, telemetry_text).
    `telemetry_text` is the concatenated contents of every <telemetry> block
    (for the server to print to logs); tags are stripped from `cleaned_text`
    so the twin panel never renders them."""
    if not twin_text:
        return "", [], [], ""
    probes = [_parse_attrs(m.group(1)) for m in _PROBE_RE.finditer(twin_text)]
    calibs = [_parse_attrs(m.group(1)) for m in _CALIBRATION_RE.finditer(twin_text)]
    telemetry_chunks = [m.group(1).strip() for m in _TELEMETRY_RE.finditer(twin_text)]
    telemetry = "\n".join(c for c in telemetry_chunks if c)
    cleaned = _TELEMETRY_RE.sub("", twin_text)
    cleaned = _CALIBRATION_RE.sub("", _PROBE_RE.sub("", cleaned))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, probes, calibs, telemetry


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_events(root: str | Path, probes: list, calibrations: list) -> None:
    """Append probes + calibrations as one JSONL event per item.
    Uses `event` (not `kind`) as the event-type field so a probe's own
    `kind` attribute (the category slug) doesn't collide with it."""
    if not probes and not calibrations:
        return
    path = Path(root) / LOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = _now_iso()
    lines = []
    for p in probes:
        lines.append(json.dumps({"event": "probe", "ts": ts, **p}, ensure_ascii=False))
    for c in calibrations:
        lines.append(json.dumps({"event": "calibration", "ts": ts, **c}, ensure_ascii=False))
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _read_log(root: str | Path, max_lines: int = 1000) -> list:
    path = Path(root) / LOG_FILENAME
    if not path.exists():
        return []
    try:
        # Windowed read — only the tail matters for pending + summary.
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines()[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


def pending_probes(root: str | Path, limit: int = 6) -> list:
    """Return the most recent probes that have no matching calibration.
    These get surfaced to the next turn so the twin can judge them."""
    events = _read_log(root)
    calibrated_ids = {e.get("id") for e in events if e.get("event") == "calibration" and e.get("id")}
    pending = []
    for e in reversed(events):  # newest first
        if e.get("event") != "probe":
            continue
        pid = e.get("id")
        if not pid or pid in calibrated_ids:
            continue
        pending.append(e)
        if len(pending) >= limit:
            break
    return list(reversed(pending))  # chronological


def accuracy_summary(root: str | Path, window: int = 50) -> str:
    """Rolling hit-rate by probe kind, ignoring silent outcomes.
    Returns a one-line string suitable for a system-prompt injection,
    or "" if there's not enough signal yet."""
    events = _read_log(root)
    probes_by_id = {}
    outcomes_by_id = {}
    for e in events:
        et = e.get("event")
        pid = e.get("id")
        if not pid:
            continue
        if et == "probe":
            probes_by_id[pid] = e
        elif et == "calibration":
            outcomes_by_id[pid] = e.get("outcome", "")

    # Count hits/misses by probe kind (category slug), trimmed to the most
    # recent `window` calibrated pairs overall (not per-kind — keeps fresh).
    paired_ids = [pid for pid in probes_by_id if pid in outcomes_by_id][-window:]
    stats: dict = {}
    for pid in paired_ids:
        kind = probes_by_id[pid].get("kind") or probes_by_id[pid].get("probe_kind") or "unknown"
        outcome = outcomes_by_id[pid]
        if outcome not in ("validated", "contradicted"):
            continue  # silent outcomes don't move the ratio
        s = stats.setdefault(kind, {"validated": 0, "contradicted": 0})
        s[outcome] += 1

    parts = []
    for kind, s in sorted(stats.items()):
        total = s["validated"] + s["contradicted"]
        if total == 0:
            continue
        rate = int(round(100 * s["validated"] / total))
        parts.append(f"{kind} {rate}% ({s['validated']}/{total})")
    if not parts:
        return ""
    return "Your historical accuracy (last " + str(window) + " calibrated probes): " + ", ".join(parts) + "."


def build_calibration_system_block(root: str | Path) -> str:
    """Render the <twin_calibration>…</twin_calibration> block injected
    into the next turn's system prompt. Empty string if there's no signal
    to share yet (no pending probes and no accuracy data)."""
    pending = pending_probes(root)
    summary = accuracy_summary(root)
    if not pending and not summary:
        return ""
    lines = ["<twin_calibration>"]
    if summary:
        lines.append(summary)
    if pending:
        lines.append(
            "Pending probes from prior turns — judge each against what the "
            "user actually did in their most recent message. Emit a "
            '<calibration id="..." outcome="validated|contradicted|silent" '
            'note="..."/> tag inside your |||TWIN||| block for each one. '
            'Outcome "silent" means the user neither validated nor '
            "contradicted the claim — use it honestly; a silent probe "
            "does not move your hit rate."
        )
        for p in pending:
            attrs = " ".join(
                f'{k}="{v}"' for k, v in p.items()
                if k not in ("ts", "event")
            )
            lines.append(f"  <probe {attrs}/>")
    lines.append("</twin_calibration>")
    return "\n".join(lines)
