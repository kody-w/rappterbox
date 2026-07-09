# Rappterbox Console — Specification

> **Schema: `rappterbox-console-spec/1.0`** &nbsp;·&nbsp; First published: 2026-05-04
>
> The contract between the rappterbox console (the **static** game-console runtime) and the cartridges (`*_agent.py` files) that plug into it. This document is the locked promise: cartridges authored against this spec will work on every rappterbox install today and forever.

---

## 1. Mental model

The rappterbox console is a **game console**. The hardware is sealed and never changes. Cartridges are the games — drop one into the slot, the LLM picks it up as a tool, the user gets a new capability inside chat.

| Layer | Mutability | Contract |
|---|---|---|
| **Console** (the rappterbox install) | sealed — never modified by cartridges | this spec, frozen at version 1.0 |
| **Cartridge** (a `*_agent.py` file) | authored, swapped, removed at will | conforms to BasicAgent + manifest |
| **Estate** (twin organisms on the device) | grows over time as the user collects | written to canonical paths only |

A cartridge that needs the console to change does not ship. Capability is added through new cartridges, never console edits.

## 2. Where everything lives

The console writes only to these paths. Cartridges may read/write only these paths. Anything else is out-of-bounds.

| Path | Owner | Purpose |
|---|---|---|
| `~/.brainstem/` | console | the install root — kernel, loaders, agents/, utils/, expansion_packs/ |
| `~/.brainstem/venv/` | console | the Python runtime; cartridges share it, never create their own |
| `~/.brainstem/agents/` | console + cartridges | every `*_agent.py` here is auto-loaded at boot |
| `~/.brainstem/expansion_packs/<pack>/` | console | optional cartridge bundles, installed via `install-expansion-pack.sh` |
| `~/.brainstem/utils/` | console | vendored utilities cartridges may import (`egg`, `peer_registry`, `lineage_check`, ...) |
| `~/.brainstem/data/` | console + cartridges | conversation memory and other kernel state |
| `~/.config/rapp/peers.json` | shared registry | neighborhood — every brainstem on this device, schema `rapp-peers/1.x` |
| `~/.rapp/` | cartridge data root | contents below |
| `~/.rapp/twins/<rappid>/` | cartridges | summoned twin organisms — one workspace per rappid |
| `~/.rapp/eggs/<rappid>/<timestamp>.egg` | cartridges | egg backups, schema `brainstem-egg/2.x` |
| `~/.rapp/pids/<rappid>.pid` | rapp-zoo | tracked PIDs of zoo-started twin processes |

`RAPP_HOME` env var overrides `~/.rapp/`. Tests and CI runs should set it to a temp dir.

## 3. Cartridge contract

Every cartridge is a single Python file under `agents/` ending in `_agent.py`. Required structure:

### 3.1 File naming

- Filename ends in `_agent.py` (mandatory — the loader's glob pattern).
- snake_case (no dashes, no spaces).
- Slug used in the manifest matches the filename minus `.py`.

### 3.2 The `__manifest__` dict

```python
__manifest__ = {
    "schema": "rapp-agent/1.0",
    "name": "@<publisher>/<slug>",      # @kody-w/summon_twin_agent
    "version": "1.0.0",                  # SemVer; immutable per (name, version)
    "display_name": "Human Name",        # what RAR/UIs show
    "description": "One-line.",          # one sentence; informs LLM tool routing
    "author": "Your Name",
    "tags": ["..."],                     # 2–5 keywords for search
    "category": "general",               # see RAR's VALID_CATEGORIES
    "quality_tier": "community",         # experimental | community | verified | official
    "requires_env": [],                  # env vars the cartridge reads
    "dependencies": ["@rapp/basic_agent"],
}
```

### 3.3 The class

```python
from agents.basic_agent import BasicAgent

class MyCartridge(BasicAgent):
    def __init__(self):
        self.name = "MyTool"             # what the LLM calls it
        self.metadata = {
            "name": self.name,
            "description": "...",         # tells the LLM when to call you
            "parameters": {                # JSON Schema for inputs
                "type": "object",
                "properties": { "..." : {...} },
                "required": [...],
            },
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs) -> str:
        # do work, return a string the model relays to the user
        return "..."
```

### 3.4 Hard rules

- `perform()` MUST return a `str`. Not a dict, not bytes, not `None`.
- No network calls in `__init__()`. The kernel constructs every cartridge at boot; slow inits block boot.
- No hardcoded secrets. Read from `os.environ.get(...)` only.
- No modifying `~/.brainstem/` outside of the cartridge's own files.
- No deleting other cartridges' data. Stay in your lane.
- Errors return as a string starting with `"Error: ..."`. The tool call still succeeds (the LLM gets the error in the result); never raise into the kernel unless something is genuinely unrecoverable.

### 3.5 Optional capabilities

- `system_context()` — return a string injected into the system prompt every turn (for cartridges that contribute persistent context, like memory-recall cartridges).
- `to_tool()` — auto-implemented by `BasicAgent`; returns the OpenAI function-calling tool dict from `self.metadata`. Don't override.
- `__manifest__["dependencies"]` — declare other agents you need (e.g., `["@rapp/basic_agent"]`). The console doesn't auto-resolve these yet; declare them for future-proofing.

## 4. The kernel-side guarantees

Cartridges may rely on these forever. The console is the **static ancestor** — none of these change.

### 4.1 Loader

- `<install_root>/brainstem.py` discovers files matching `<install_root>/agents/*_agent.py`.
- For each file, the loader uses `importlib.util.spec_from_file_location()` to import it.
- Any class in the loaded module that:
  - is a `type`,
  - has a `perform` attribute,
  - is not named `BasicAgent` or `object`,
  - does not start with `_`,
  
  is instantiated with no arguments. The instance is registered by `instance.name`.
- The loader runs once per boot. To pick up new cartridges, restart the brainstem.

### 4.2 Shims registered before any cartridge import

The console pre-registers these so cartridge imports resolve cleanly:

| Import statement | Resolves to |
|---|---|
| `from agents.basic_agent import BasicAgent` | the canonical base class |
| `from utils.azure_file_storage import AzureFileStorageManager` | `local_storage.py`'s local-disk implementation |
| `from utils.dynamics_storage import DynamicsStorageManager` | same local-disk implementation |
| `from utils.storage_factory import get_storage_manager` | factory returning local manager |

Cartridges with cloud-flavored imports therefore run locally without modification. Drop a cartridge written for an Azure-backed brainstem and it works on a vanilla rappterbox.

### 4.3 Vendored utilities

`<install_root>/utils/` ships the modules below. Cartridges import them via either `from utils import X` or bare `import X` — the brainstem adds `<install_root>` and `<install_root>/utils` to `sys.path` before any cartridge loads.

| Module | Purpose | Schema |
|---|---|---|
| `utils/egg.py` | pack/unpack `.egg` cartridges (twin transport + backup) | `brainstem-egg/2.0`, `brainstem-egg/2.1` |
| `utils/peer_registry.py` | XDG-stored registry of all brainstems on this machine | `rapp-peers/1.1` |
| `utils/lineage_check.py` | detect uninitialized template clones; refuse to boot | — |
| `utils/local_storage.py` | local disk replacement for Azure/Dynamics storage | — |
| `utils/twin.py` | digital-twin calibration helpers (`|||TWIN|||` block parser) | — |
| `utils/llm.py` | provider-agnostic LLM call wrapper used by the kernel | — |
| `utils/workspace.py` | workspace utilities | — |
| `utils/frames.py`, `utils/index_card.py` | UX framing helpers | — |

Cartridges should treat unavailability gracefully: wrap imports in `try/except ImportError` and degrade. The four shipped Wii Sports demonstrate the pattern.

### 4.4 Body functions and senses

Same single-file drop-in pattern for two other extension surfaces:

- `utils/body_functions/<name>_body_function.py` → `/api/<name>/...` HTTP routes
- `utils/senses/<name>_sense.py` → chat-stream contributors that augment system prompts and split responses

Spec for those is layered on top of this one and inherits the same stability rules.

## 5. Egg cartridge format (`brainstem-egg/2.x`)

Eggs are zip files with a typed manifest and a payload tree. Used for transport, backup, and the egg-based hatching cycle.

### 5.1 Schema versions

| Schema | What it carries | Where it lives |
|---|---|---|
| `brainstem-egg/2.0` | rapplications, twins, snapshots, swarms (RAPP-instance shape) | `utils/egg.py` |
| `brainstem-egg/2.1` | variant repos (rappid.json + brainstem.py at same root) — bundles `repo/` + `data/` | `utils/egg.py` |
| `brainstem-egg/2.2-organism` | brainstem-instance organisms (rappid.json above `src/rapp_brainstem/`) | `utils/bond.py` (rapp-zoo) |
| `brainstem-egg/2.2-rapplication` | rapplications packed with state cartridge | `utils/bond.py` (rapp-zoo) |

The console must accept all of these on summon. Cartridges that produce eggs should pick the most specific schema that fits the source layout.

### 5.2 Egg manifest (2.1)

```json
{
  "schema": "brainstem-egg/2.1",
  "type": "twin",
  "rappid": "rappid:@<publisher>/<slug>:<64hex>",
  "exported_at": "2026-05-04T...",
  "source": {
    "rappid_uuid": "<UUID4>",
    "parent_rappid_uuid": "<parent's UUID4>",
    "repo": "https://github.com/<owner>/<repo>.git",
    "commit": "<git SHA at pack time>",
    "name": "<twin display name>"
  },
  "brainstem": {
    "version": "0.12.2",
    "source_repo": "https://github.com/kody-w/RAPP.git",
    "source_commit": "<rapp brainstem SHA>"
  },
  "bundled_repo": true,
  "bundled_state": true,
  "repo_file_count": <int>,
  "data_file_count": <int>,
  "attestation": null
}
```

> **Identity (Eternity form).** The top-level `rappid` is the consolidated Eternity string `rappid:@<publisher>/<slug>:<64hex>` — the `<64hex>` is the keyless identity hash (per `rapp-eternity/1.0` / CONSTITUTION Art. XXXIV.1/XXXVI.1: a stable UUID/commit-derived or content hash, **independent of the slug**), and `kind` is **not** in the string (it is carried by the sibling `"type"` field / the record). Legacy `rappid:v2:<kind>:@<owner>/<repo>:<32hex>@github.com/...` strings are read-forever and canonicalized on read — **never newly emitted**. (The `source.rappid_uuid` fields below are legacy provenance from pre-Eternity packs, preserved read-only.)

### 5.3 Payload layout

```
<egg>.egg
├── manifest.json              ← required
├── repo/<rel>                 ← variant-repo tree (when bundled_repo)
│   ├── brainstem.py
│   ├── rappid.json
│   ├── soul.md / MANIFEST.md / README.md / LICENSE
│   └── agents/  utils/  installer/
└── data/<rel>                 ← .brainstem_data tree (when bundled_state)
    ├── memory.json
    ├── identity.json
    └── conversations/
```

### 5.4 Excluded from packing (always)

`.copilot_token`, `.copilot_session`, `voice.zip`, `.DS_Store`, `Thumbs.db`, `__pycache__/`, `venv/`, `.pytest_cache/`, `.brainstem_data/private/`, `.env`, `.env.local`. Cartridges that re-implement pack must enforce the same exclusions.

### 5.5 The summon contract

`utils.egg.summon_twin_egg(blob: bytes, host_root: str, keep_existing_kernel: bool = False) -> str`

- Returns the absolute path of the materialized workspace at `<host_root>/<rappid_uuid>/`.
- Idempotent: summoning the same egg twice into the same host returns the same path; existing files are overwritten with the egg's bytes (the egg is the source of truth on summon).
- `keep_existing_kernel=True` preserves any `brainstem.py` already at the workspace path. This is the egg-based hatching cycle: lay-egg → swap kernel files → summon-egg back with `keep_existing_kernel=True`.

## 6. Peer registry schema (`rapp-peers/1.1`)

`~/.config/rapp/peers.json`:

```json
{
  "schema": "rapp-peers/1.1",
  "peers": [
    {
      "id": "<sha256(brainstem_dir)[:12]>",
      "brainstem_dir": "<absolute path>",
      "port": <int>,
      "is_global": <bool>,
      "is_twin_only": <bool>,
      "project_name": "<string>",
      "installed_at": "<ISO timestamp>",
      "version": "<semver>",
      "rappid_uuid": "<UUID4 | null>",
      "twin_name": "<string | null>",
      "parent_repo": "<URL | null>",
      "summoned_from": "<egg path or source identifier | null>",
      "summoned_at": "<ISO timestamp | null>"
    }
  ]
}
```

Three install scopes:

| Scope | Path pattern | When used |
|---|---|---|
| `is_global=true` | `~/.brainstem/...` | the catch-all global brainstem |
| `is_twin_only=true` | `~/.rapp/twins/<rappid>/...` | a summoned twin not bound to a project |
| (neither) | `<project>/.brainstem/...` | project-local install |

Cartridges that produce peers (e.g., SummonTwin, HatchEgg) MUST upsert with `rappid_uuid` and `twin_name` set so the estate UIs can group incarnations by twin (parallel-omniscience pattern).

## 7. Versioning and stability

### 7.1 Spec stability

This document is `rappterbox-console-spec/1.0`. The schema is **frozen forever**. Future spec versions are additive only — `1.1`, `1.2` add fields; never remove or rename. A `2.0` would only ship if the static-ancestor commitment had to be broken (it should not).

### 7.2 Cartridge versioning

- `__manifest__["version"]` is SemVer. The pair `(name, version)` is **immutable** in RAR — once published with a given hash, that version cannot be republished with different content. Bump the patch version (1.0.0 → 1.0.1) for any code change.
- Major version bumps (1.x → 2.0) signal an interface change. Cartridges with the same name but different major versions are different cartridges from the console's perspective.

### 7.3 Schema versioning

- `rappterbox-console-spec/1.x` — this document
- `rapp-agent/1.x` — cartridge manifest schema
- `rapp-peers/1.x` — peer registry schema
- `brainstem-egg/2.x` — egg cartridge schema
- `rapp-rappid/2.x` — twin identity/lineage record (defers to `rapp-eternity/1.0`, the sole identity standard)

All additive. Old readers ignore unknown fields. Old writers produce data that newer readers accept.

## 8. Lineage rules (Constitution Article XXXIV)

When a cartridge generates a new twin, `parent_rappid` MUST point at the actual code-ancestor — the repo whose code shaped this twin. Cartridges have these defaults:

- A cartridge that templates a twin from local templates (e.g., `SummonTwin`) defaults to `parent_rappid = wildhaven_rappid` (the canonical Pre-Founder twin pattern), since the soul-template structure descends from wildhaven.
- A cartridge that imports an egg (e.g., `HatchEgg`) preserves whatever `parent_rappid` was in the egg's manifest. Lineage chains are not rewritten on transport.
- A cartridge MAY NOT set `parent_rappid` to a repo whose code it did not actually inherit. The single-parent rule is enforced by `lineage_check.py` at boot.

## 9. Drop-in kernel replaceability (Constitution Article XXXIII)

The brainstem.py kernel is sacred. The contract between kernel and the rest is:
- The kernel imports a small stable set of names (`local_storage.AzureFileStorageManager`, etc.) which are shimmed into existence by `_register_shims()` before any cartridge loads.
- A user can `cp upstream/brainstem.py ~/.brainstem/brainstem.py` over a locally-mutated install, and the organism keeps living. Cartridges are not invalidated by kernel updates.
- The egg-based hatching cycle exists exactly so that kernel updates never lose state: lay an egg, swap the kernel, summon the egg back with `keep_existing_kernel=True`.

## 10. Security and trust

The console is intentionally low-trust:
- Bound to `127.0.0.1` only. Never listens on a public interface by default.
- No auth — the host OS is the trust boundary. If someone has shell access, they have brainstem access.
- Cartridges run in-process with full Python privileges. Treat them like extensions: only install cartridges from sources you trust.
- The RAR registry is one such trust source — cartridges with `quality_tier: official` have been reviewed; `community` is unreviewed.

Future signing (Article XXXIV.7) will let the console verify cartridge attestation cryptographically. Until then, trust is human-mediated.

## 11. References

- [`README.md`](./README.md) — install and usage
- [`agents/`](./agents/) — the four bundled Wii Sports cartridges
- [`expansion_packs/twin/`](./expansion_packs/twin/) — opt-in SummonTwin + HatchEgg cartridges
- [Constitution Article XXXII–XXXIV](https://github.com/kody-w/RAPP/blob/main/CONSTITUTION.md) — kernel sacredness, lineage rules
- [`utils/egg.py`](./utils/egg.py) — egg pack/unpack reference implementation
- [`utils/peer_registry.py`](./utils/peer_registry.py) — peer registry reference implementation
- [RAR registry](https://github.com/kody-w/RAR) — public catalog of cartridges by namespace

## 12. Compliance checklist

A cartridge ships rappterbox-compliant when:

- [ ] Filename ends in `_agent.py`, snake_case
- [ ] `__manifest__` dict at module level with all required fields
- [ ] One class extending `BasicAgent` with `name`, `metadata`, `perform()`
- [ ] `perform()` returns a `str`
- [ ] No network calls in `__init__`
- [ ] No hardcoded secrets
- [ ] Errors returned as `"Error: ..."` strings
- [ ] Loadable via `_load_agent_from_file()` against the canonical brainstem
- [ ] If it produces twins: peer_registry upsert with `rappid_uuid` + `twin_name`
- [ ] If it produces eggs: schema `brainstem-egg/2.x`; honors exclusion list
- [ ] If it claims an ancestor: `parent_rappid` reflects actual code lineage
