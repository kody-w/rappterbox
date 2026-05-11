# `.cart.json` — rappterbox cartridge format v0.1

Schema id: `rappterbox-cart/0.1`

A cartridge is a single JSON file that fully describes one workflow session: the participants, the runtime that renders the session, and the transcript of everything that happened inside it. Drop one onto the rappterbox console and the session reanimates.

This is the format that backs the §7.21 patent claim cluster (kept private with counsel — see `kody-w/wildhaven-ceo/legal/patent/`). It is intentionally small and self-describing.

## The contract in one sentence

A cart is `manifest + runtime + transcript`. The runtime is sha256-pinned. Replaying the transcript against the pinned runtime reproduces the session bit-for-bit.

## Top-level fields

| field | type | required | meaning |
|---|---|---|---|
| `schema` | string | yes | Always `"rappterbox-cart/0.1"`. |
| `rappid` | string | yes | The cart's identity — `rappid:v2:cart:@<owner>/<repo>#<name>:<32-hex>@<substrate>/<owner>/<repo>`. |
| `name` | string | yes | Short slug. Match `<name>` in the rappid. |
| `title` | string | yes | Human-readable display name. |
| `description` | string | yes | One-line pitch shown in the console card. |
| `parent_rappid` | string \| null | yes | The cart this branched from (another cart, an agent, or a rapplication). `null` for genesis carts. |
| `branch_at_event` | int \| null | yes | The transcript event index in the parent at which this cart diverged. `null` if `parent_rappid` is null. |
| `participants` | array | yes | Operator + AI participants. See below. |
| `runtime` | object | yes | Embedded interactive runtime. See below. |
| `transcript` | array | yes | Append-only event log. See below. |
| `minted_at` | string (ISO 8601) | yes | When this cart file was packed. |
| `minted_by` | string | yes | Rappid of the operator or agent that packed it. |

## `participants[]`

Each participant:

```json
{ "rappid": "operator", "role": "operator", "name": "you" }
{ "rappid": "rappid:v2:agent:@kody-w/rappterbox#hello:abc...@github.com/kody-w/rappterbox", "role": "twin", "name": "Echo" }
```

Roles: `operator` | `twin` | `observer`. Exactly one operator per cart. Multiple twins allowed (multi-participant per §7.23).

## `runtime`

```json
{
  "type": "html",
  "sha256": "<hex of payload>",
  "payload": "<inline HTML source>"
}
```

- `type`: `html` for v0.1 (HTML+inline JS). Reserved: `wasm`, `pyodide`.
- `sha256`: hex digest of the payload string. The console verifies before mounting.
- `payload`: full HTML document string. Mounted into an iframe via `srcdoc`. No external fetches required to render — everything inline.

Two cartridges sharing the same `runtime.sha256` share execution semantics — the console MAY cache by hash.

## `transcript[]`

Append-only ordered event log. Each event:

```json
{ "event": <int>, "kind": "<string>", "ts": "<ISO 8601>", "by": "<rappid|operator>", "data": { ... } }
```

Reserved kinds:

- `session_start` — first event of every cart. `data: { runtime_sha256, participants_at_start }`.
- `operator_input` — operator typed/spoke. `data: { text }`.
- `twin_response` — a twin emitted output. `data: { text, twin_rappid }`.
- `cart_state` — runtime emitted a state mutation worth replaying. `data: { ... }` (runtime-defined).
- `branch` — operator forked the cart. `data: { new_cart_rappid }`.

Custom kinds allowed; runtime owns interpretation. Console displays them in the operator-mic panel without needing to understand them.

## Runtime ↔ console postMessage protocol

The runtime iframe communicates with the console parent via `window.parent.postMessage`. All messages are JSON objects with a `kind` field.

**Runtime → console:**

| kind | data | meaning |
|---|---|---|
| `cart_ready` | `{}` | Runtime mounted, ready for input. |
| `cart_event` | `{ event: {...} }` | Append this event to the transcript. |
| `chat_request` | `{ text, twin_rappid? }` | Ask the console to dispatch to the local brainstem. |

**Console → runtime:**

| kind | data | meaning |
|---|---|---|
| `cart_init` | `{ transcript, participants, operator_rappid }` | Replay state on mount. |
| `chat_response` | `{ text, twin_rappid }` | Brainstem's reply to the previous chat_request. |
| `operator_mic` | `{ text }` | Operator interjected at console level (per §7.23.1(e)). |

## Determinism rule

A cart MUST replay deterministically: re-mounting an unchanged cart with the same operator_input sequence MUST produce the same transcript. Runtimes that need entropy MUST seed a PRNG from the cart's rappid (treat the rappid as the seed). Runtimes that need the wall clock for display MAY use it but MUST NOT branch behavior on it.

This is what makes carts ROM-like.

## What changes in v0.2 (planned)

- `runtime.payload` may be base64 (for binary WASM)
- `transcript` events may carry `tool_calls[]` for finer-grained replay
- Cross-cart references via `imports[]` (the recursive sub-tether of §7.22)
- Sealed signature on the manifest using the operator's Binder key (per WH-2026-001 Claim 4)

v0.1 deliberately omits these. The job of v0.1 is to prove `manifest + runtime + transcript` round-trips through a console.
