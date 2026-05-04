# rappterbox console

> **A static, local-first runtime for digital organisms — with a cartridge slot.**

The rappterbox console is a small Python/Flask brainstem that boots on your machine, exposes a chat surface at `http://127.0.0.1:7071`, and loads `*_agent.py` cartridges from `agents/`. Think Wii: the hardware is sealed and never changes; the games are cartridges you swap in.

The kernel is **sacred and drop-in replaceable** (Constitution Article XXXIII). Cartridges conform to the kernel's `BasicAgent` contract; they never modify the kernel.

## Install

```bash
curl -fsSL https://kody-w.github.io/rappterbox/installer/install.sh | bash
bash ~/.brainstem/start.sh
```

Open <http://127.0.0.1:7071> for the chat surface.

To install with the **twin expansion pack** (`SummonTwin` + `HatchEgg`):

```bash
curl -fsSL https://kody-w.github.io/rappterbox/installer/install.sh | bash -s -- --with twin
```

## What's bundled (Wii Sports)

Four cartridges ship pre-loaded with the console — the showcase set:

| Tool name | What it does | File |
|---|---|---|
| **ManageMemory**   | Save typed memories (fact / preference / insight / task) to local persistent storage. They survive across conversations forever. | [`agents/manage_memory_agent.py`](./agents/manage_memory_agent.py) |
| **ContextMemory**  | Recall the user's saved memories at conversation start, so every chat starts informed. | [`agents/context_memory_agent.py`](./agents/context_memory_agent.py) |
| **HackerNews**     | Fetch top stories from Hacker News's public Firebase API. No key, no auth. | [`agents/hacker_news_agent.py`](./agents/hacker_news_agent.py) |
| **LearnNewAgent**  | The meta-cartridge: describe what you want a new agent to do, and it generates one in real time. Agents creating agents. | [`agents/learn_new_agent.py`](./agents/learn_new_agent.py) |

Plus the [`BasicAgent`](./agents/basic_agent.py) base class — the contract every cartridge inherits.

## Expansion packs

Optional cartridges, installable on demand. Each pack is a directory under `expansion_packs/<name>/` containing one or more `*_agent.py` files. Install:

```bash
bash ~/.brainstem/installer/install-expansion-pack.sh <pack-name>
# Restart the brainstem to pick up the new cartridges:
bash ~/.brainstem/start.sh
```

| Pack | Cartridges | Purpose |
|---|---|---|
| **twin** ([`expansion_packs/twin/`](./expansion_packs/twin/)) | `SummonTwin`, `HatchEgg` | Generate new twin organisms in chat; import `.egg` cartridges from other devices and hatch them locally with identity, memory, and mutations preserved. |

Future packs will land in `expansion_packs/`. Anyone can author their own — see [the cartridge contract](#the-cartridge-contract) below.

## The console + cartridge contract

The console exposes a **stable interface** that cartridges conform to. The contract is locked: the console will never demand changes from cartridges.

**For cartridge authors:**
- Your file must be named `*_agent.py` and live in `agents/` (auto-loaded at boot) or `expansion_packs/<pack>/` (loaded after `install-expansion-pack.sh`).
- Define a class extending `BasicAgent`:
  ```python
  from agents.basic_agent import BasicAgent

  class MyAgent(BasicAgent):
      def __init__(self):
          self.name = "MyTool"
          self.metadata = {
              "name": self.name,
              "description": "...",  # tells the LLM when to call you
              "parameters": { ... },  # JSON Schema for inputs
          }
          super().__init__(name=self.name, metadata=self.metadata)

      def perform(self, **kwargs) -> str:
          # do work, return a string the model relays to the user
          return "..."
  ```
- The brainstem's loader (`_load_agent_from_file()`) will auto-discover and instantiate your class.
- The LLM will see `self.name` as a callable tool. The user just talks to chat; the model invokes you when relevant.

**The console provides** (and these never change):
- `~/.brainstem/` — install root
- `~/.brainstem/venv/` — the Python runtime cartridges share
- `~/.config/rapp/peers.json` — neighborhood registry (schema `rapp-peers/1.x`)
- `~/.rapp/` — cartridge data root (twins/, eggs/, pids/)
- `from agents.basic_agent import BasicAgent` — the base class
- `from utils import egg, peer_registry, lineage_check, ...` — vendored utilities

## Local-first by design

Nothing in this console phones home. No telemetry. No auth (it binds to localhost). All state lives on your device:
- Conversations + memories at `~/.brainstem/data/`
- Twins (organisms) at `~/.rapp/twins/<rappid>/`
- Egg backups at `~/.rapp/eggs/<rappid>/`

Your data is yours. Eggs are how it transports between devices — see [rapp-zoo](https://github.com/kody-w/rapp-zoo) for the keeper UI that manages your estate of organisms.

## Constitution

This console implements the rules from the RAPP species root:
- **Drop-in kernel replaceability** (Article XXXIII §3) — drop a fresh `brainstem.py` from upstream over any locally-mutated install, the organism keeps living.
- **Cartridges never modify the kernel** (Article XXXIII §1). Add capability via `*_agent.py` files; never edit `brainstem.py`.
- **Single-parent rule for variants** (Article XXXIV) — when cartridges spawn new twins (e.g., `SummonTwin`), `parent_rappid` always points to the actual code-ancestor.
- **Never overwrite local data** — installer/expansion-pack scripts refuse to clobber existing files; cartridges write to canonical paths only.

## Related repos

- [`kody-w/RAPP`](https://github.com/kody-w/RAPP) — the species root. The constitutional document. Where the kernel ultimately comes from.
- [`kody-w/rapp-zoo`](https://github.com/kody-w/rapp-zoo) — local-first Pokédex / keeper for the organisms summoned by the twin expansion pack.
- [`kody-w/wildhaven-ai-homes-twin`](https://github.com/kody-w/wildhaven-ai-homes-twin) — the canonical Pre-Founder twin variant; soul-template descendants of `SummonTwin` inherit from here.

## License

All Rights Reserved. Source-available under the same terms as RAPP.
