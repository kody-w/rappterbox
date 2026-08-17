# `.egg` cartridge — `brainstem-egg/2.3-session` (formerly `rappterbox-cart/0.1`)

A **session cartridge** is one variant of the unified **`.egg` cartridge** family. The egg cartridge is the universal sneakernet primitive across the RAPP ecosystem — anything portable between brainstems is an egg cartridge, identified by its schema kind. Same `.egg` extension, same rappzoo Pokédex shelf, same drag-drop UX. The kernel's `egg_hatcher_agent.py` introspects the cartridge and routes to the right destination based on what's inside.

## The egg-cartridge family

| Schema | Kind | Payload shape | Hatcher route → destination | Status |
|---|---|---|---|---|
| `brainstem-egg/2.2-organism` | `organism` | ZIP: rappid + soul + .env + agents + organs + senses + services + .brainstem_data | hatch into `~/.brainstem/` or `~/.rapp/twins/<rappid>/` (full instance) | shipping |
| `brainstem-egg/2.2-rapplication` | `rapplication` | ZIP: rappid + agent.py + UI + per-rapp state | install as a planted rapp under host brainstem | shipping |
| **`brainstem-egg/2.3-session`** | **`session`** | **JSON: rappid + runtime payload (HTML/JS) + transcript + participants** | **mount in rappterbox console iframe (or vbrainstem.html standalone)** | **this doc** |
| `brainstem-egg/2.3-neighborhood` | `neighborhood` | ZIP: rappid + neighborhood.json + members.json + agents/ + rapplications/ + ses/ + soul.md + CONSTITUTION.md + rar/index.json | mint a new GitHub repo (or local mirror) acting as a neighborhood gate | planned |
| `brainstem-egg/2.3-estate` | `estate` | ZIP: public discovery surface + private "bones" repo pointer + sealed PII pointer | re-anchor the operator's whole multi-tier identity on a new substrate | planned |

The egg-cartridge ecosystem is **substrate-agnostic** — cartridges travel via AirDrop, USB, GitHub, Discord, email, NFC, QR. The format MUST round-trip across any of those without loss.

## How `egg_hatcher_agent.py` routes

The kernel agent reads any `.egg` from a path or URL, parses its `manifest.json` (or top-level JSON for session cartridges), and dispatches by `schema` / `type`:

```
HatchEgg(egg_path="/Volumes/usb/dad.egg")
  → opens the file
  → reads manifest.json (ZIP) OR parses as JSON (session)
  → switch (manifest.type):
      organism      → utils.bond.hatch_organism(...)        → ~/.rapp/twins/<rappid>/
      rapplication  → utils.bond.hatch_rapplication(...)    → planted rapp dir
      session       → returns "session cartridge — mount in rappterbox console:
                              https://kody-w.github.io/rappterbox/console.html
                              or vbrainstem.html — drag the .egg in"
      neighborhood  → (planned) mint new GitHub repo via gh CLI + scaffold
      estate        → (planned) re-anchor estate on target substrate
      unknown       → "unknown egg kind '<kind>' — schema '<schema>'.
                              Hatcher knows: organism, rapplication, session.
                              See kody-w/rappterbox/carts/SCHEMA.md."
```

The hatcher NEVER guesses. If the cartridge has no recognized schema, it tells the operator and stops — no destructive routing.

---

## `brainstem-egg/2.3-session` — the session cartridge spec

A session cartridge is structurally a single workflow session: one runtime that renders the interactive surface, one transcript that captures what happened inside it, and a manifest that names the rappid + lineage + participants. JSON-only (not ZIP) because there's no directory tree to compress — just one runtime + one transcript.

### Top-level fields (all required unless noted)

| field | type | meaning |
|---|---|---|
| `schema` | string | Always `"brainstem-egg/2.3-session"`. (Backwards-compat: legacy carts may use `"rappterbox-cart/0.1"` — the loader accepts both.) |
| `type` | string | Always `"session"` (the egg kind). |
| `rappid` | string | The cartridge's identity — `rappid:v2:cart:@<owner>/<repo>#<name>:<32-hex>@<substrate>/<owner>/<repo>`. |
| `name` | string | Short slug. Match `<name>` in the rappid. |
| `title` | string | Human-readable display name. |
| `description` | string | One-line pitch shown in the rappterbox console card. |
| `parent_rappid` | string \| null | The cartridge this branched from (another cartridge, an agent, or a rapplication). `null` for genesis. |
| `branch_at_event` | int \| null | The transcript event index in the parent at which this cartridge diverged. |
| `participants` | array | Operator + AI participants. See below. |
| `runtime` | object | Embedded interactive runtime. See below. |
| `transcript` | array | Append-only event log. See below. |
| `exported_at` | string (ISO 8601) | When this cartridge was packed. |
| `minted_at` | string (ISO 8601) | When this cartridge was packed (== exported_at for v0.1). |
| `minted_by` | string | Rappid of the operator or agent that packed it. |
| `implements` | array | Constitutional articles this cartridge honors — for spot-validation by hatchers. |

### `participants[]`

```json
{ "rappid": "operator", "role": "operator", "name": "you", "sprite": "🧍" }
{ "rappid": "rappid:v2:agent:@kody-w/RAPP#reporter-twin:hello@github.com/kody-w/RAPP",
  "role": "twin", "name": "Reporter", "sprite": "📰",
  "persona": "You are Reporter. Use the HackerNews agent to fetch today's top story…" }
```

Roles: `operator` | `coordinator` | `twin` | `observer`. Exactly one `operator` per cartridge. The `coordinator` is a special twin — the operator's autonomous proxy who drives workflows on their behalf.

### `runtime`

```json
{
  "type": "html",
  "sha256": "<hex of payload>",
  "payload": "<inline HTML source>"
}
```

- `type`: `html` for v0.1 (HTML+inline JS). Reserved: `wasm`, `pyodide`, `iframe-url`.
- `sha256`: hex digest of the payload string. The console verifies before mounting.
- `payload`: full HTML document string. Mounted into a sandboxed iframe via `srcdoc`. No external fetches required.

Two cartridges sharing the same `runtime.sha256` share execution semantics — the console MAY cache by hash.

### `transcript[]`

Append-only ordered event log. Each event:

```json
{ "event": <int>, "event_id": "<peer>:<n>", "kind": "<string>", "ts": "<ISO 8601>", "by": "<rappid|operator>", "data": { ... } }
```

Reserved kinds: `session_start`, `operator_input`, `twin_response`, `cart_state`, `demo_step`, `branch`. Custom kinds allowed; runtime owns interpretation.

## Runtime ↔ console postMessage protocol

The runtime iframe communicates with the console parent via `window.parent.postMessage`. JSON objects with a `kind` field.

**Runtime → console:**

| kind | data | meaning |
|---|---|---|
| `cart_ready` | `{}` | Runtime mounted, ready for input. |
| `cart_event` | `{ event: {...} }` | Append this event to the transcript. |
| `chat_request` | `{ corr, text, twin_rappid? }` | Ask the console to dispatch to the local brainstem. `corr` round-trips. |

**Console → runtime:**

| kind | data | meaning |
|---|---|---|
| `cart_init` *(or `egg_init`)* | `{ transcript, participants, operator_rappid }` | Replay state on mount. The runtime accepts both names for forward-compat. |
| `chat_response` | `{ corr, text, twin_rappid }` | Brainstem's reply, correlated by `corr`. |
| `operator_mic` | `{ text }` | Operator interjected at console level. |

## Determinism rule

A session cartridge MUST replay deterministically: re-mounting an unchanged cartridge with the same `operator_input` sequence MUST produce the same transcript. Runtimes that need entropy MUST seed a PRNG from the cartridge's rappid.

## v0.4 plans

- `brainstem-egg/2.3-neighborhood` + `2.3-estate` (see top table) — implemented
- Sealed manifest signature using the operator's Binder ECDSA key
- `imports[]` — recursive sub-cartridges
- Base64 binary payloads (for WASM runtimes)

## See also

- Master egg packer: [`rapp_brainstem/utils/bond.py`](https://github.com/kody-w/RAPP/blob/main/rapp_brainstem/utils/bond.py) (organism + rapplication)
- Egg hatcher (introspection + routing): [`rapp_brainstem/agents/egg_hatcher_agent.py`](https://github.com/kody-w/RAPP/blob/main/rapp_brainstem/agents/egg_hatcher_agent.py)
- vBrainstem (emits + consumes session cartridges): [`pages/vbrainstem.html`](https://github.com/kody-w/RAPP/blob/main/pages/vbrainstem.html)
