from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTOPILOT = (
    ROOT
    / "expansion_packs"
    / "objective-autopilot"
    / "objective_autopilot_agent.py"
)
POKEMON = ROOT / "expansion_packs" / "pokemon" / "pokemon_agent.py"


def _load(path: Path, name: str):
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_objective_templates_compile_under_rapp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPP_HOME", str(tmp_path))
    module = _load(AUTOPILOT, "objective_autopilot_agent_test")
    agent = module.ObjectiveAutopilotAgent()

    for run_mode in ("finite", "continuous", "hybrid"):
        result = json.loads(agent.perform(action="template", run_mode=run_mode))
        assert result["template"]["run_mode"] == run_mode
        assert result["constitution"]["loop"][-1] == "report"

    mission = module.TEMPLATES["finite"]
    mission["mission_id"] = "pokemon-red"
    mission["system"]["application"] = "Pokemon Red"
    mission["system"]["environment"] = "authorized local emulator"
    mission["authorization"] = {
        "principal": "operator",
        "scope": ["local emulator"],
        "expires_at": "",
        "revocation": "kill switch",
        "artifact": "operator request",
    }
    mission["objectives"]["true_objective"] = "Enter the Hall of Fame"
    mission["objectives"]["terminal"][0] = {
        "id": "hall-of-fame",
        "description": "Enter the Hall of Fame",
        "pass_condition": "Hall of Fame state is observed",
        "verifier": "deterministic game-memory reader",
        "evidence": "checkpoint plus decoded game state",
    }
    mission["effect_policy"][0]["effect_class"] = "local-emulator-input"
    compiled = json.loads(agent.perform(action="compile", mission=mission))
    path = Path(compiled["path"])
    assert compiled["status"] == "READY"
    assert path == tmp_path / "autopilot/missions/pokemon-red/v1.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert Path(compiled["current_path"]).name == "mission.json"
    again = json.loads(agent.perform(action="compile", mission=mission))
    assert again["created"] is False


def test_mission_versions_are_immutable(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPP_HOME", str(tmp_path))
    module = _load(AUTOPILOT, "objective_autopilot_agent_versions_test")
    agent = module.ObjectiveAutopilotAgent()
    mission = module.TEMPLATES["finite"]
    mission.update({"mission_id": "immutable", "version": 1})
    mission["system"].update({"application": "App", "environment": "test"})
    mission["authorization"].update(
        {
            "principal": "operator",
            "scope": ["test"],
            "revocation": "kill",
            "artifact": "request",
        }
    )
    mission["objectives"]["true_objective"] = "Finish"
    mission["objectives"]["terminal"][0].update(
        {
            "id": "done",
            "description": "Finish",
            "pass_condition": "Verified done",
            "verifier": "independent verifier",
            "evidence": "artifact",
        }
    )
    mission["effect_policy"][0]["effect_class"] = "test-effect"
    assert json.loads(agent.perform(action="compile", mission=mission))["status"] == "READY"
    mission["objectives"]["true_objective"] = "Rewrite history"
    assert agent.perform(action="compile", mission=mission).startswith("Error:")


def test_concurrent_versions_cannot_regress_current(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPP_HOME", str(tmp_path))
    module = _load(AUTOPILOT, "objective_autopilot_agent_concurrent_test")
    agent = module.ObjectiveAutopilotAgent()
    base = module.TEMPLATES["finite"]
    base.update({"mission_id": "concurrent", "version": 1})
    base["system"].update({"application": "App", "environment": "test"})
    base["authorization"].update(
        {
            "principal": "operator",
            "scope": ["test"],
            "revocation": "kill",
            "artifact": "request",
        }
    )
    base["objectives"]["true_objective"] = "Finish"
    base["objectives"]["terminal"][0].update(
        {
            "id": "done",
            "description": "Finish",
            "pass_condition": "Verified done",
            "verifier": "independent verifier",
            "evidence": "artifact",
        }
    )
    base["effect_policy"][0]["effect_class"] = "test-effect"
    newer = json.loads(json.dumps(base))
    newer["version"] = 2

    v1_entered = threading.Event()
    release_v1 = threading.Event()
    original_write_once = module._write_once_json

    def delayed_write(path, value):
        if path.name == "v1.json":
            v1_entered.set()
            assert release_v1.wait(timeout=2)
        return original_write_once(path, value)

    monkeypatch.setattr(module, "_write_once_json", delayed_write)
    results = []
    first = threading.Thread(
        target=lambda: results.append(agent.perform(action="compile", mission=base))
    )
    second = threading.Thread(
        target=lambda: results.append(agent.perform(action="compile", mission=newer))
    )
    first.start()
    assert v1_entered.wait(timeout=2)
    second.start()
    release_v1.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    current = json.loads(
        (
            tmp_path / "autopilot/missions/concurrent/mission.json"
        ).read_text(encoding="utf-8")
    )
    assert current["mission"]["version"] == 2


def test_malformed_contract_fails_closed():
    module = _load(AUTOPILOT, "objective_autopilot_agent_malformed_test")
    agent = module.ObjectiveAutopilotAgent()
    malformed = {
        "schema": module.MISSION_SCHEMA,
        "mission_id": "bad-contract",
        "run_mode": "finite",
        "system": None,
        "authorization": "not-an-object",
        "objectives": "not-an-object",
        "effect_policy": [],
        "evidence_contract": None,
        "capabilities": None,
        "effect_ledger": None,
        "fleet": None,
        "data_rules": None,
        "budgets": None,
        "halt": None,
        "acceptance_tests": None,
    }
    result = json.loads(agent.perform(action="validate", mission=malformed))
    assert result["status"] == "NEEDS_CONFIGURATION"
    assert "version must be a positive integer" in result["errors"]
    assert "effect_policy must contain at least one rule" in result["errors"]


def test_nonfinite_values_fail_closed():
    module = _load(AUTOPILOT, "objective_autopilot_agent_nonfinite_test")
    agent = module.ObjectiveAutopilotAgent()
    for amount in (float("nan"), float("inf"), float("-inf")):
        mission = module.TEMPLATES["finite"]
        mission["budgets"]["currency"] = amount
        result = json.loads(agent.perform(action="validate", mission=mission))
        assert result["status"] == "NEEDS_CONFIGURATION"
        assert "mission must contain only finite standard JSON values" in result["errors"]
        assert "budgets.currency must be finite" in result["errors"]


def test_incomplete_mission_fails_closed():
    module = _load(AUTOPILOT, "objective_autopilot_agent_invalid_test")
    agent = module.ObjectiveAutopilotAgent()
    result = json.loads(agent.perform(action="validate", mission={}))
    assert result["status"] == "NEEDS_CONFIGURATION"
    assert "authorization.principal is required" not in result["errors"]
    assert "missing required section: authorization" in result["errors"]


def test_pokemon_pack_is_rbox_native_and_rom_free():
    source = POKEMON.read_text(encoding="utf-8")
    assert '"schema": "rapp-agent/1.0"' in source
    assert '"@kody-w/pokemon_agent"' in source
    assert 'Path.home() / ".openrappter"' not in source
    assert '"rboxes"' in source
    assert "AGENT_ENTRYPOINT" in source
    assert "MODULE_NAME" not in source
    assert "OPENRAPPTER_POKEMON_ROM" in source
    assert not list((ROOT / "expansion_packs" / "pokemon").glob("*.gb"))
    assert not list((ROOT / "expansion_packs" / "pokemon").glob("*.sav"))


def test_pokemon_pack_direct_entrypoint_starts():
    result = subprocess.run(
        [sys.executable, str(POKEMON), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "{run,supervise}" in result.stdout


def test_expansion_installer_supports_pack_dependencies():
    script = (
        ROOT / "installer" / "install-expansion-pack.sh"
    ).read_text(encoding="utf-8")
    assert "system-requirements.txt" in script
    assert '"$PACK_DIR/requirements.txt"' in script
    assert '"$VENV_PIP" install' in script
