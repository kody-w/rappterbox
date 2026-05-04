/* rapp-mobile.js — local-first multi-twin runtime for mobile browsers.
 *
 * Mirrors swarm/server.py + swarm/t2t.py + swarm/workspace.py + swarm/llm.py
 * + swarm/chat.py — but in vanilla JS, with IndexedDB as the filesystem
 * and WebCrypto as the crypto provider.
 *
 * Multi-twin: one device can hold many twins.
 *   • SELF twins    — you own the cloud_id + secret. Can sign T2T as that twin.
 *   • IMPORTED twins — public bundles pulled from the registry (or any URL).
 *     Have soul + agents, NO secret. Can chat with them offline; can't sign
 *     messages as them.
 *
 * Local-first principles applied:
 *   1. UI never spins on local actions — IDB reads return synchronously fast.
 *   2. Network is OPTIONAL — only the LLM call requires it. Everything else
 *      (chat history, documents, swarm state, T2T peer registry) is local.
 *   3. Identity is generated on first run and never sent to a third party.
 *   4. Imported twins work fully offline once pulled — soul + agents are in IDB.
 *   5. Data lives in IndexedDB, exportable as JSON, owned by the user.
 */

const RAPP = (function () {
'use strict';

// ─── IndexedDB ─────────────────────────────────────────────────────────

const DB_NAME = 'rapp-twin';
const DB_VERSION = 2;
// All stores are keyed by `<twin_id>:<inner_key>` so one DB holds many twins.
const STORES = ['twins', 'peers', 'documents', 'inbox', 'outbox',
                'swarms', 'memory', 'settings', 'conversations'];

let _db = null;
function db() {
  if (_db) return Promise.resolve(_db);
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = (e) => {
      const d = e.target.result;
      for (const s of STORES) {
        if (!d.objectStoreNames.contains(s)) d.createObjectStore(s);
      }
    };
    req.onsuccess = () => { _db = req.result; resolve(_db); };
    req.onerror = () => reject(req.error);
  });
}
async function idbGet(s, k)   { const d = await db(); return new Promise((r,j)=>{const x=d.transaction(s).objectStore(s).get(k);x.onsuccess=()=>r(x.result);x.onerror=()=>j(x.error);}); }
async function idbPut(s, k, v){ const d = await db(); return new Promise((r,j)=>{const x=d.transaction(s,'readwrite').objectStore(s).put(v,k);x.onsuccess=()=>r(true);x.onerror=()=>j(x.error);}); }
async function idbDel(s, k)   { const d = await db(); return new Promise((r,j)=>{const x=d.transaction(s,'readwrite').objectStore(s).delete(k);x.onsuccess=()=>r(true);x.onerror=()=>j(x.error);}); }
async function idbList(s) {
  const d = await db();
  return new Promise((res, rej) => {
    const out = [];
    const c = d.transaction(s).objectStore(s).openCursor();
    c.onsuccess = (e) => { const cur = e.target.result; if (cur) { out.push({ key: cur.key, value: cur.value }); cur.continue(); } else res(out); };
    c.onerror = () => rej(c.error);
  });
}

// ─── Crypto helpers ────────────────────────────────────────────────────

function bytesToHex(b) { return Array.from(b).map(x => x.toString(16).padStart(2, '0')).join(''); }
function randomHex(n)  { const a = new Uint8Array(n); crypto.getRandomValues(a); return bytesToHex(a); }
function uuidv4() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const b = new Uint8Array(16); crypto.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const h = bytesToHex(b);
  return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
}
async function _hmacKey(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i*2, 2), 16);
  return crypto.subtle.importKey('raw', bytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
}
async function sign(payload, secretHex) {
  const sig = await crypto.subtle.sign('HMAC', await _hmacKey(secretHex), new TextEncoder().encode(payload));
  return bytesToHex(new Uint8Array(sig));
}
async function verify(payload, sigHex, secretHex) {
  try {
    const exp = await sign(payload, secretHex);
    if (exp.length !== sigHex.length) return false;
    let ok = 0;
    for (let i = 0; i < exp.length; i++) ok |= exp.charCodeAt(i) ^ sigHex.charCodeAt(i);
    return ok === 0;
  } catch { return false; }
}
function canonicalJson(obj) {
  if (obj === null || typeof obj !== 'object') return JSON.stringify(obj);
  if (Array.isArray(obj)) return '[' + obj.map(canonicalJson).join(',') + ']';
  const keys = Object.keys(obj).sort();
  return '{' + keys.map(k => JSON.stringify(k) + ':' + canonicalJson(obj[k])).join(',') + '}';
}
function envelopePayload(conv_id, seq, body) { return canonicalJson({ conv_id, seq, body }); }

// ─── Twin registry (multi-twin) ────────────────────────────────────────
// Keys in `twins` store are twin_id (UUID). Values:
//   { twin_id, kind: 'self'|'imported', handle, cloud_id, secret?,
//     origin, pulled_from?, pulled_at?, created_at, soul_summary }

const ACTIVE_KEY = '__active_twin__';

async function listTwins() {
  const rows = await idbList('twins');
  return rows
    .filter(r => r.key !== ACTIVE_KEY)
    .map(r => ({ ...r.value, twin_id: r.key }));
}

async function getActiveTwinId() {
  const v = await idbGet('twins', ACTIVE_KEY);
  if (v) return v.twin_id;
  // Auto-create a self-twin on very first launch
  const t = await createSelfTwin('@unhatched');
  await setActiveTwin(t.twin_id);
  return t.twin_id;
}

async function setActiveTwin(twin_id) {
  await idbPut('twins', ACTIVE_KEY, { twin_id });
  return twin_id;
}

async function getTwin(twin_id) {
  return await idbGet('twins', twin_id);
}

async function createSelfTwin(handle) {
  const twin_id = uuidv4();
  const twin = {
    twin_id, kind: 'self', handle: handle || '@unhatched',
    cloud_id: randomHex(16), secret: randomHex(32),
    origin: 'self',
    created_at: new Date().toISOString(),
    device: navigator.userAgent.slice(0, 80),
  };
  await idbPut('twins', twin_id, twin);
  return twin;
}

async function deleteTwin(twin_id) {
  // Wipe all twin-namespaced records across stores
  for (const s of ['peers', 'documents', 'inbox', 'outbox', 'swarms',
                    'memory', 'conversations']) {
    const rows = await idbList(s);
    for (const r of rows) if (r.key.startsWith(twin_id + ':')) await idbDel(s, r.key);
  }
  await idbDel('twins', twin_id);
  // If active, pick another or create a fresh self
  const active = await idbGet('twins', ACTIVE_KEY);
  if (active && active.twin_id === twin_id) {
    const remaining = await listTwins();
    if (remaining.length) await setActiveTwin(remaining[0].twin_id);
    else { const t = await createSelfTwin('@unhatched'); await setActiveTwin(t.twin_id); }
  }
}

// Import a twin bundle (e.g., Molly's cloud from the global registry).
// The imported twin gets its OWN local twin_id; the bundle's cloud_id (if
// present) is recorded as the origin for attribution but never used as
// the LIVE identity (no secret = can't sign T2T as them).
async function importTwinBundle(bundle, opts = {}) {
  if (bundle.schema !== 'rapp-swarm/1.0' && bundle.schema !== 'rapp-twin/1.0') {
    throw new Error(`unsupported bundle schema: ${bundle.schema}`);
  }
  const twin_id = uuidv4();
  const twin = {
    twin_id,
    kind: 'imported',
    handle: opts.handle || bundle.handle || `@imported-${twin_id.slice(0,8)}`,
    cloud_id: bundle.cloud_id || `import-${randomHex(8)}`,
    // NOTE: NO secret. Imported twins can be talked TO (locally), not AS.
    origin: bundle.handle || bundle.name || 'imported',
    pulled_from: opts.pulled_from || '',
    pulled_at: new Date().toISOString(),
    soul_summary: (bundle.soul || '').slice(0, 240),
    created_at: bundle.created_at || new Date().toISOString(),
  };
  await idbPut('twins', twin_id, twin);

  // Hatch the bundle as the imported twin's first swarm
  await deploySwarm(twin_id, bundle);
  return twin;
}

// Export a twin to a portable bundle (with or without conversations).
async function exportTwin(twin_id, opts = {}) {
  const twin = await getTwin(twin_id);
  if (!twin) throw new Error('twin not found');
  const swarms = await listSwarms(twin_id);
  const out = {
    schema: 'rapp-twin/1.0',
    handle: twin.handle,
    cloud_id: twin.cloud_id,
    origin: twin.origin,
    exported_at: new Date().toISOString(),
    swarms: [],
  };
  // Pull each swarm's full manifest (incl. agent source) for portability
  for (const s of swarms) {
    const m = await idbGet('swarms', `${twin_id}:${s.swarm_guid}`);
    if (m) out.swarms.push(m);
  }
  if (opts.includeConversations) {
    const convs = await idbList('conversations');
    out.conversations = convs.filter(r => r.key.startsWith(twin_id + ':'))
                             .map(r => ({ swarm_guid: r.key.split(':')[1], history: r.value.history }));
  }
  if (opts.includeDocuments) {
    out.documents = (await idbList('documents'))
      .filter(r => r.key.startsWith(twin_id + ':'))
      .map(r => ({ name: r.key.split(':').slice(1).join(':'), ...r.value }));
  }
  return out;
}

// Pull a twin from a public registry URL (the global directory).
async function pullTwinFromRegistry(registryUrl, cloudId) {
  const r = await fetch(registryUrl, { cache: 'no-cache' });
  if (!r.ok) throw new Error(`registry HTTP ${r.status}`);
  const reg = await r.json();
  const all = [...(reg.hero_humans || []), ...(reg.hero_role_twins || [])];
  const cloud = all.find(c => c.id === cloudId);
  if (!cloud) throw new Error(`cloud not found in registry: ${cloudId}`);
  const bundle = bundleFromRegistryEntry(cloud, reg);
  return importTwinBundle(bundle, {
    handle: cloud.owner_handle || `@${cloud.id}`,
    pulled_from: registryUrl,
  });
}

// Build a deployable swarm bundle from a registry cloud entry.
function bundleFromRegistryEntry(cloud, reg) {
  const stub = (cid, s) => {
    const cls = (s.name || '').replace(/[^A-Za-z0-9]/g, '') + 'Agent';
    return `from agents.basic_agent import BasicAgent\n` +
           `__manifest__ = {"schema":"rapp-agent/1.0","name":"@twinstack/${cid}-${s.name.toLowerCase()}",` +
           `"tier":"core","trust":"community","version":"0.1.0","tags":["twin-stack"]}\n` +
           `class ${cls}(BasicAgent):\n` +
           `    def __init__(self):\n` +
           `        self.name = ${JSON.stringify(s.name)}\n` +
           `        self.metadata = {"name":self.name,"description":${JSON.stringify(s.description||'')},` +
           `"parameters":{"type":"object","properties":{},"required":[]}}\n` +
           `        super().__init__(name=self.name, metadata=self.metadata)\n` +
           `    def perform(self, **kwargs):\n` +
           `        return ${JSON.stringify(`Stub for ${s.name}: ${s.role_framing||''}`)}\n`;
  };
  return {
    schema: 'rapp-swarm/1.0',
    name: cloud.title,
    purpose: cloud.tagline || cloud.for || cloud.title,
    soul: cloud.soul_addendum || '',
    cloud_id: cloud.id,
    handle: cloud.owner_handle || `@${cloud.id}`,
    created_at: new Date().toISOString(),
    created_by: '@imported',
    agents: (cloud.swarms || []).map(s => ({
      filename: s.name.toLowerCase().replace(/[^a-z0-9]/g, '_') + '_agent.py',
      name: s.name,
      description: s.description || '',
      role_framing: s.role_framing || '',
      source: stub(cloud.id, s),
    })),
  };
}

// ─── Twin-namespaced storage helpers ───────────────────────────────────

function tk(twin_id, key) { return twin_id + ':' + key; }

// ─── Peers (per active twin) ───────────────────────────────────────────

async function listPeers(twin_id) {
  const rows = await idbList('peers');
  return rows.filter(r => r.key.startsWith(twin_id + ':')).map(r => {
    const { secret, ...rest } = r.value;
    return { cloud_id: r.key.split(':').slice(1).join(':'), ...rest };
  });
}
async function addPeer(twin_id, { cloud_id, secret, handle = '', url = '', allowed_caps = ['*'] }) {
  await idbPut('peers', tk(twin_id, cloud_id),
    { secret, handle, url, allowed_caps, added_at: new Date().toISOString() });
  return { cloud_id, handle, url, allowed_caps };
}
async function getPeer(twin_id, cloud_id) {
  const p = await idbGet('peers', tk(twin_id, cloud_id));
  return p ? { cloud_id, ...p } : null;
}

// ─── Documents (per twin) ──────────────────────────────────────────────

const MAX_DOC_BYTES = 10 * 1024 * 1024;
function safeName(n) { return (n||'').replace(/[\/\\]/g, '').replace(/[\x00-\x1f]/g, '').slice(0, 255); }

async function listDocuments(twin_id) {
  const summarize = async (store) => {
    const rows = await idbList(store);
    return rows.filter(r => r.key.startsWith(twin_id + ':')).map(r => ({
      name: r.key.split(':').slice(1).join(':'),
      bytes: r.value.bytes,
      modified_at: r.value.modified_at,
    }));
  };
  return {
    documents: await summarize('documents'),
    inbox:     await summarize('inbox'),
    outbox:    await summarize('outbox'),
  };
}
async function readDocument(twin_id, name, location = 'documents') {
  const v = await idbGet(_locStore(location), tk(twin_id, safeName(name)));
  return v ? { name, location, ...v } : null;
}
async function writeDocument(twin_id, name, contentBytes, location = 'documents') {
  const n = safeName(name);
  if (!n) throw new Error('invalid name');
  if (contentBytes.length > MAX_DOC_BYTES) throw new Error('document exceeds 10MB cap');
  let b64 = '';
  for (let i = 0; i < contentBytes.length; i += 0x8000)
    b64 += String.fromCharCode.apply(null, contentBytes.subarray(i, i + 0x8000));
  const rec = { bytes: contentBytes.length, content_b64: btoa(b64), modified_at: new Date().toISOString() };
  await idbPut(_locStore(location), tk(twin_id, n), rec);
  return { name: n, location, bytes: rec.bytes, saved_at: rec.modified_at };
}
async function deleteDocument(twin_id, name, location = 'documents') {
  await idbDel(_locStore(location), tk(twin_id, safeName(name)));
  return true;
}
function _locStore(loc) { return loc === 'inbox' ? 'inbox' : loc === 'outbox' ? 'outbox' : 'documents'; }

function base64ToBytes(b64) {
  const bin = atob(b64); const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// ─── T2T (only valid from a SELF twin — imported twins can't sign) ─────

async function sendDocumentToPeer(twin_id, { to, document_name, location = 'documents' }) {
  const me = await getTwin(twin_id);
  if (!me || me.kind !== 'self') throw new Error('only self-twins can send T2T');
  const peer = await getPeer(twin_id, to);
  if (!peer) throw new Error('peer not whitelisted');
  if (!peer.url) throw new Error('peer has no URL on file');
  const doc = await readDocument(twin_id, document_name, location);
  if (!doc) throw new Error('document not found');

  const payloadObj = {
    from: me.cloud_id, name: doc.name, bytes: doc.bytes,
    content_b64: doc.content_b64, sent_at: new Date().toISOString(),
  };
  const sig = await sign(canonicalJson(payloadObj), me.secret);
  const r = await fetch(peer.url.replace(/\/+$/, '') + '/api/t2t/receive-document', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...payloadObj, sig }),
  });
  if (!r.ok) throw new Error(`peer HTTP ${r.status}`);
  await writeDocument(twin_id, doc.name, base64ToBytes(doc.content_b64), 'outbox');
  return await r.json();
}

// ─── Settings (single global — same LLM keys across all twins) ─────────

const SETTINGS_KEY = 'llm';
async function getSettings() {
  return (await idbGet('settings', SETTINGS_KEY)) || {
    provider: 'azure-openai',
    azure_endpoint: '', azure_api_key: '', azure_deployment: '',
    openai_api_key: '', openai_model: 'gpt-4o',
    anthropic_api_key: '', anthropic_model: 'claude-sonnet-4-6',
    tether_url: 'http://127.0.0.1:7071',
  };
}
async function setSettings(s) { await idbPut('settings', SETTINGS_KEY, s); return s; }

// ─── Local tether bridge (the twin's "hands" on the OS) ────────────────
// When the user runs the local brainstem alongside the PWA, it speaks the
// rapp-tether/1.0 wire shape on :7071 and exposes local *_agent.py files
// with REAL system access (filesystem, processes, LAN, hardware). The PWA
// discovers it via /tether/tools and registers those tools alongside the
// swarm's in-browser stubs. Tool calls route to whichever side actually
// has the agent.
//
// Trust model: the tether is granting the LLM the SAME power as the user's
// own shell. The user must explicitly run the brainstem to enable this —
// it's not on by default, and the URL is a local-network address that
// doesn't resolve from outside the device.

let _tetherCache = null;       // { url, alive, tools, count, fetched_at }
const TETHER_TTL_MS = 30_000;

async function probeTether(force = false) {
  const s = await getSettings();
  const url = (s.tether_url || 'http://127.0.0.1:7071').replace(/\/+$/, '');
  if (!force && _tetherCache && _tetherCache.url === url
      && (Date.now() - _tetherCache.fetched_at) < TETHER_TTL_MS) {
    return _tetherCache;
  }
  try {
    // Probe healthz first (fast), then enrich with tools.
    const hr = await fetch(url + '/tether/healthz', { signal: AbortSignal.timeout(1500) });
    if (!hr.ok) throw new Error(`HTTP ${hr.status}`);
    const h = await hr.json();
    let tools = [];
    try {
      const tr = await fetch(url + '/tether/tools', { signal: AbortSignal.timeout(1500) });
      if (tr.ok) tools = (await tr.json()).tools || [];
    } catch {}
    _tetherCache = {
      url, alive: true,
      agent_count: h.count || (h.agents || []).length,
      tools, fetched_at: Date.now(),
    };
    return _tetherCache;
  } catch {
    _tetherCache = { url, alive: false, agent_count: 0, tools: [], fetched_at: Date.now() };
    return _tetherCache;
  }
}

async function callTetherAgent(name, args) {
  const s = await getSettings();
  const url = (s.tether_url || 'http://127.0.0.1:7071').replace(/\/+$/, '');
  const r = await fetch(url + '/tether/agent', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, args }),
  });
  const j = await r.json();
  if (j.status === 'ok') return typeof j.output === 'string' ? j.output : JSON.stringify(j.output);
  throw new Error(j.message || 'tether call failed');
}

// ─── LLM dispatch ──────────────────────────────────────────────────────

function detectProvider(s) {
  if (s.azure_endpoint && s.azure_api_key) return 'azure-openai';
  if (s.openai_api_key)    return 'openai';
  if (s.anthropic_api_key) return 'anthropic';
  return 'none';
}
async function llmChat(messages, tools = null) {
  const s = await getSettings();
  const p = detectProvider(s);
  if (p === 'azure-openai') return chatAzureOpenAI(messages, tools, s);
  if (p === 'openai')        return chatOpenAI(messages, tools, s);
  if (p === 'anthropic')     return chatAnthropic(messages, tools, s);
  return { role: 'assistant', content: '⚠️ No LLM key configured. Open Settings (⚙) to add one.' };
}
async function chatAzureOpenAI(messages, tools, s) {
  const isV1 = s.azure_endpoint.includes('/openai/v1/');
  let url = s.azure_endpoint;
  if (!url.includes('/chat/completions')) {
    url = url.replace(/\/+$/, '') + `/openai/deployments/${s.azure_deployment}/chat/completions?api-version=2025-01-01-preview`;
  } else if (!isV1 && !url.includes('?')) {
    url += '?api-version=2025-01-01-preview';
  }
  const body = { messages, model: s.azure_deployment };
  if (tools && tools.length) { body.tools = tools; body.tool_choice = 'auto'; }
  const r = await fetch(url, { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'api-key': s.azure_api_key },
    body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`LLM HTTP ${r.status}: ${(await r.text()).slice(0,300)}`);
  return normalizeOpenAI(await r.json());
}
async function chatOpenAI(messages, tools, s) {
  const body = { model: s.openai_model || 'gpt-4o', messages };
  if (tools && tools.length) { body.tools = tools; body.tool_choice = 'auto'; }
  const r = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + s.openai_api_key },
    body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`LLM HTTP ${r.status}: ${(await r.text()).slice(0,300)}`);
  return normalizeOpenAI(await r.json());
}
async function chatAnthropic(messages, tools, s) {
  let sys = ''; const msgs = [];
  for (const m of messages) {
    if (m.role === 'system') sys = (sys + '\n' + (m.content || '')).trim();
    else msgs.push(m);
  }
  const body = { model: s.anthropic_model, max_tokens: 4096, messages: msgs };
  if (sys) body.system = sys;
  if (tools && tools.length) {
    body.tools = tools.filter(t => t.type === 'function').map(t => ({
      name: t.function.name, description: t.function.description || '',
      input_schema: t.function.parameters || { type: 'object', properties: {} },
    }));
  }
  const r = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json',
                'x-api-key': s.anthropic_api_key,
                'anthropic-version': '2023-06-01' },
    body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`LLM HTTP ${r.status}: ${(await r.text()).slice(0,300)}`);
  const j = await r.json();
  let text = ''; const tool_calls = [];
  for (const blk of (j.content || [])) {
    if (blk.type === 'text') text += blk.text;
    if (blk.type === 'tool_use') tool_calls.push({
      id: blk.id, type: 'function',
      function: { name: blk.name, arguments: JSON.stringify(blk.input || {}) },
    });
  }
  const out = { role: 'assistant', content: text };
  if (tool_calls.length) out.tool_calls = tool_calls;
  return out;
}
function normalizeOpenAI(resp) {
  const choices = resp.choices || [];
  if (!choices.length) return { role: 'assistant', content: (resp.error && resp.error.message) || '' };
  const m = choices[0].message || {};
  const out = { role: 'assistant', content: m.content || '' };
  if (m.tool_calls) out.tool_calls = m.tool_calls;
  return out;
}

// ─── Swarms (per twin) ─────────────────────────────────────────────────

async function listSwarms(twin_id) {
  const rows = await idbList('swarms');
  return rows.filter(r => r.key.startsWith(twin_id + ':')).map(r => ({
    swarm_guid: r.key.split(':').slice(1).join(':'),
    name: r.value.name, purpose: r.value.purpose,
    agent_count: (r.value.agents || []).length,
  }));
}
async function getSwarm(twin_id, swarm_guid) {
  return await idbGet('swarms', tk(twin_id, swarm_guid));
}
async function deploySwarm(twin_id, bundle) {
  if (bundle.schema !== 'rapp-swarm/1.0') throw new Error('unsupported bundle schema');
  let sg = (bundle.swarm_guid || '').trim().toLowerCase();
  if (!sg.match(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)) {
    sg = uuidv4();
  }
  const manifest = {
    schema: 'rapp-swarm/1.0', swarm_guid: sg,
    name: bundle.name || 'untitled', purpose: bundle.purpose || '',
    soul: bundle.soul || '',
    created_at: bundle.created_at || new Date().toISOString(),
    agents: (bundle.agents || []).map(a => ({
      filename: a.filename, name: a.name, description: a.description || '',
      role_framing: a.role_framing || '', source: a.source,
    })),
  };
  await idbPut('swarms', tk(twin_id, sg), manifest);
  return manifest;
}
async function deleteSwarm(twin_id, swarm_guid) {
  await idbDel('swarms', tk(twin_id, swarm_guid));
  await idbDel('conversations', tk(twin_id, swarm_guid));
}

// ─── Chat loop ─────────────────────────────────────────────────────────

const MAX_TOOL_ROUNDS = 4;

async function chatWithSwarm(twin_id, swarm_guid, user_input, history = []) {
  const manifest = await getSwarm(twin_id, swarm_guid);
  if (!manifest) return { error: 'swarm not found', swarm_guid };

  const agents = manifest.agents || [];

  // Build tool list. Each swarm agent contributes ONE tool. We also include
  // tether tools IF and ONLY IF the swarm declares any tether_required
  // agents — that's the trigger for involving the local tether at all.
  const swarmDeclaresTether = agents.some(a => _needsTether(a));
  const tools = agents.map(a => ({
    type: 'function',
    function: {
      name: a.name,
      description: a.description || '',
      parameters: _agentParameters(a),
    },
  }));

  // Probe tether ONLY when the swarm has tether-required agents. This keeps
  // the tether out of the loop entirely for browser-only swarms.
  let tether = null;
  if (swarmDeclaresTether) {
    tether = await probeTether();
    if (!tether.alive) {
      // The swarm needs hardware but tether isn't running — surface this
      // to the LLM as an extra system note so it can apologize gracefully.
      tools.push({
        type: 'function',
        function: {
          name: '__tether_unavailable',
          description: 'A required tether tool is unavailable. Inform the user that the local tether process is not running and they need to start it.',
          parameters: { type: 'object', properties: {}, required: [] },
        },
      });
    }
  }

  const messages = [];
  if (manifest.soul) messages.push({ role: 'system', content: manifest.soul });
  if (swarmDeclaresTether && tether && !tether.alive) {
    messages.push({
      role: 'system',
      content: 'NOTE: this swarm has agents that need OS access via the local '
        + 'tether, but the tether is not running. If the user asks for an '
        + 'action that needs hardware/filesystem/process access, tell them: '
        + '"That needs the local brainstem — start it with `brainstem` (or '
        + '`./start.sh` from rapp_brainstem/) to enable OS access."',
    });
  }
  for (const m of history) {
    if (['user','assistant','tool','system'].includes(m.role)) messages.push(m);
  }
  messages.push({ role: 'user', content: user_input });

  const agent_logs = []; let rounds = 0;
  while (true) {
    rounds++;
    let assistant;
    try { assistant = await llmChat(messages, tools.length ? tools : null); }
    catch (e) { return { response: `LLM error: ${e.message}`, agent_logs, rounds, swarm_guid, error: e.message }; }
    messages.push(assistant);

    const tcs = assistant.tool_calls || [];
    if (!tcs.length || rounds >= MAX_TOOL_ROUNDS) {
      return { response: assistant.content || '', agent_logs, rounds, swarm_guid,
                provider: detectProvider(await getSettings()),
                tether_used: agent_logs.some(l => l.via === 'tether') };
    }
    for (const c of tcs) {
      const name = (c.function && c.function.name) || '';
      let args = {};
      try { args = JSON.parse((c.function && c.function.arguments) || '{}'); } catch {}
      const t0 = performance.now();
      const exec = await _executeToolCall(agents, name, args, tether);
      agent_logs.push({
        name, args,
        ms: Math.round(performance.now() - t0),
        output: (exec.output || '').slice(0, 2000),
        via: exec.via,                  // 'browser' | 'tether' | 'unknown'
        error: exec.error || undefined,
      });
      messages.push({ role: 'tool', tool_call_id: c.id || '', name, content: exec.output || '' });
    }
  }
}

// ─── Tool-call dispatch (the agent decides; the tether is opt-in) ──────

function _needsTether(agent) {
  // The agent declares its own requirement. Either via an explicit flag in
  // the manifest, OR via the tag "tether" (convention).
  if (!agent) return false;
  if (agent.tether_required === true) return true;
  if (agent.manifest && agent.manifest.tether_required === true) return true;
  const tags = (agent.tags || (agent.manifest && agent.manifest.tags) || []);
  return Array.isArray(tags) && tags.includes('tether');
}

function _agentParameters(agent) {
  // Prefer explicit metadata.parameters from the manifest if present.
  if (agent.parameters) return agent.parameters;
  if (agent.metadata && agent.metadata.parameters) return agent.metadata.parameters;
  return { type: 'object', properties: {}, required: [] };
}

async function _executeToolCall(agents, name, args, tether) {
  // Special pseudo-tool injected when the swarm needs tether but tether is down.
  if (name === '__tether_unavailable') {
    return { output: JSON.stringify({
      error: 'tether_unavailable',
      message: 'The local brainstem is not running. To enable hardware/filesystem/process actions, start it with: brainstem (or ./start.sh from rapp_brainstem/)',
    }), via: 'browser' };
  }

  const swarmAgent = agents.find(a => a.name === name);
  const declaresTether = _needsTether(swarmAgent);

  // Case 1: swarm agent that needs tether → route to tether
  if (swarmAgent && declaresTether) {
    if (!tether || !tether.alive) {
      return {
        output: JSON.stringify({
          error: 'tether_unavailable',
          message: `Agent "${name}" requires the local brainstem but it's not running. Ask the user to start it with: brainstem (or ./start.sh from rapp_brainstem/)`,
        }),
        via: 'tether', error: 'tether_unavailable',
      };
    }
    try {
      const out = await callTetherAgent(name, args);
      return { output: out, via: 'tether' };
    } catch (e) {
      return { output: JSON.stringify({ error: 'tether_call_failed', message: e.message }),
                via: 'tether', error: e.message };
    }
  }

  // Case 2: swarm agent that does NOT need tether → in-browser stub (Pyodide v2)
  if (swarmAgent) {
    return { output: _browserStub(swarmAgent, args), via: 'browser' };
  }

  // Case 3: not in the swarm — maybe the LLM tried to call a tether-only
  // tool (because the swarm doesn't expose tether tools by default, this
  // shouldn't normally happen, but handle it gracefully).
  return { output: JSON.stringify({ error: 'unknown_agent', name }), via: 'unknown' };
}

function _browserStub(agent, args) {
  return `[${agent.name}] ${agent.description || agent.role_framing || ''} — args=${JSON.stringify(args)}. ` +
         `(Browser-side Pyodide execution lands in v2; for now this is a stub. Set tether_required=true in the agent manifest to route to the local tether instead.)`;
}

// ─── Conversations ─────────────────────────────────────────────────────

async function saveConversation(twin_id, swarm_guid, history) {
  await idbPut('conversations', tk(twin_id, swarm_guid),
    { history, saved_at: new Date().toISOString() });
}
async function loadConversation(twin_id, swarm_guid) {
  const v = await idbGet('conversations', tk(twin_id, swarm_guid));
  return v ? v.history : [];
}

// ─── Offline-resilient send queue ──────────────────────────────────────
// Local-first invariant: when the network drops mid-operation, NOTHING is
// lost. The user's message is persisted to the conversation immediately.
// If the LLM is unreachable, the message goes into the pending queue and
// auto-retries when the browser fires the `online` event.

const PENDING_KEY = '__pending_send_queue__';

async function _loadPending() {
  const v = await idbGet('conversations', PENDING_KEY);
  return v && Array.isArray(v.queue) ? v.queue : [];
}
async function _savePending(queue) {
  await idbPut('conversations', PENDING_KEY, { queue, updated_at: new Date().toISOString() });
}

async function isOnline() {
  // navigator.onLine is hint-only — it can lie. Confirm with a small fetch
  // when the user actually tries to send. For pure indicator purposes, use
  // the hint.
  return navigator.onLine !== false;
}

// chatWithSwarmResilient — same shape as chatWithSwarm, but:
//   • Always saves the user message + the assistant reply locally first
//   • If the LLM throws (network or 5xx), the assistant message is a
//     graceful offline acknowledgment AND the request is queued for retry
//   • Never throws — always returns a usable result the UI can render
async function chatWithSwarmResilient(twin_id, swarm_guid, user_input, history = []) {
  // Snapshot the user message into history immediately
  const localHistory = [...history, { role: 'user', content: user_input }];
  await saveConversation(twin_id, swarm_guid, localHistory);

  try {
    const result = await chatWithSwarm(twin_id, swarm_guid, user_input, history);
    if (!result.error) {
      const newHistory = [...localHistory,
                          { role: 'assistant', content: result.response || '' }];
      await saveConversation(twin_id, swarm_guid, newHistory);
      return { ...result, offline: false };
    }
    // Soft error from LLM (e.g., 4xx) — treat as a real reply, no queue
    return { ...result, offline: false };
  } catch (e) {
    // Hard network failure — queue + return a graceful offline reply
    const queue = await _loadPending();
    queue.push({
      twin_id, swarm_guid, user_input,
      history,
      queued_at: new Date().toISOString(),
    });
    await _savePending(queue);

    const offlineReply =
      `📵 You're offline — your message is saved and will retry automatically when the connection returns. ` +
      `In the meantime, you can keep browsing your twin's documents, conversations, and swarms — all local.`;
    const newHistory = [...localHistory, { role: 'assistant', content: offlineReply }];
    await saveConversation(twin_id, swarm_guid, newHistory);
    return {
      response: offlineReply, offline: true, queued: true,
      agent_logs: [], rounds: 0, swarm_guid,
    };
  }
}

// Drain the pending queue — call on `online` event.
async function drainPendingQueue(onProgress) {
  const queue = await _loadPending();
  if (!queue.length) return { drained: 0 };
  const remaining = [];
  let drained = 0;
  for (const item of queue) {
    try {
      const result = await chatWithSwarm(item.twin_id, item.swarm_guid,
                                          item.user_input, item.history);
      if (result.error) throw new Error(result.error);
      const history = await loadConversation(item.twin_id, item.swarm_guid);
      // Append the LLM's actual response (replace the offline ack from the bottom)
      const cleaned = history.filter(m =>
        !(m.role === 'assistant' && (m.content || '').startsWith('📵 You\'re offline'))
      );
      const newHistory = [...cleaned, { role: 'assistant', content: result.response || '' }];
      await saveConversation(item.twin_id, item.swarm_guid, newHistory);
      drained++;
      if (onProgress) onProgress({ item, result });
    } catch {
      remaining.push(item);  // still failing — keep in queue
    }
  }
  await _savePending(remaining);
  return { drained, remaining: remaining.length };
}

async function pendingCount() { return (await _loadPending()).length; }
async function clearPendingQueue() { await _savePending([]); }

// ─── Egg import/export (universal .egg = ZIP with digitaltwin.json) ───

async function _readZipEntry(blob, targetName) {
  const buf = await blob.arrayBuffer();
  const view = new DataView(buf);
  const len = buf.byteLength;
  let eocd = -1;
  for (let i = len - 22; i >= Math.max(0, len - 65558); i--) {
    if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('Not a valid ZIP');
  const cdOff = view.getUint32(eocd + 16, true);
  const cdCount = view.getUint16(eocd + 10, true);
  let pos = cdOff;
  for (let i = 0; i < cdCount; i++) {
    if (view.getUint32(pos, true) !== 0x02014b50) break;
    const nameLen = view.getUint16(pos + 28, true);
    const extraLen = view.getUint16(pos + 30, true);
    const commentLen = view.getUint16(pos + 32, true);
    const localOff = view.getUint32(pos + 42, true);
    const name = new TextDecoder().decode(new Uint8Array(buf, pos + 46, nameLen));
    if (name === targetName) {
      const lv = new DataView(buf, localOff);
      const lNameLen = lv.getUint16(26, true);
      const lExtraLen = lv.getUint16(28, true);
      const method = lv.getUint16(8, true);
      const cSize = lv.getUint32(18, true);
      const dataStart = localOff + 30 + lNameLen + lExtraLen;
      const raw = new Uint8Array(buf, dataStart, cSize);
      if (method === 0) return new TextDecoder().decode(raw);
      if (method === 8) {
        const ds = new DecompressionStream('deflate-raw');
        const writer = ds.writable.getWriter();
        writer.write(raw); writer.close();
        const chunks = []; const reader = ds.readable.getReader();
        while (true) { const {done, value} = await reader.read(); if (done) break; chunks.push(value); }
        const total = chunks.reduce((s,c) => s + c.length, 0);
        const out = new Uint8Array(total); let off = 0;
        for (const c of chunks) { out.set(c, off); off += c.length; }
        return new TextDecoder().decode(out);
      }
      throw new Error(`Unsupported ZIP method: ${method}`);
    }
    pos += 46 + nameLen + extraLen + commentLen;
  }
  return null;
}

async function importEgg(blob) {
  const raw = await _readZipEntry(blob, 'digitaltwin.json');
  if (!raw) throw new Error('Not a portable .egg — missing digitaltwin.json');
  const bundle = JSON.parse(raw);
  if (bundle.schema !== 'rapp-twin/1.0') throw new Error('Unsupported egg schema: ' + bundle.schema);
  const twin = await importTwinBundle(bundle, { handle: bundle.handle || '@digitaltwin' });
  if (bundle.memory && Array.isArray(bundle.memory)) {
    for (const m of bundle.memory) {
      await idbPut('memory', twin.twin_id + ':' + m.key, m.data);
    }
  }
  return twin;
}

async function exportEgg(twin_id) {
  const bundle = await exportTwin(twin_id, { includeConversations: true, includeDocuments: true });
  bundle.schema = 'rapp-twin/1.0';
  const memRows = await idbList('memory');
  bundle.memory = memRows
    .filter(r => r.key.startsWith(twin_id + ':'))
    .map(r => ({ key: r.key.split(':').slice(1).join(':'), data: r.value }));
  return bundle;
}

// ─── Public API ────────────────────────────────────────────────────────

return {
  // Twin lifecycle
  listTwins, getTwin, createSelfTwin, deleteTwin,
  getActiveTwinId, setActiveTwin,
  importTwinBundle, exportTwin, pullTwinFromRegistry, bundleFromRegistryEntry,
  // Egg portability
  importEgg, exportEgg,
  // Crypto
  sign, verify, canonicalJson, envelopePayload,
  // Peers
  listPeers, addPeer, getPeer,
  // Documents
  listDocuments, readDocument, writeDocument, deleteDocument,
  // T2T
  sendDocumentToPeer,
  // Settings + LLM
  getSettings, setSettings, llmChat,
  // Tether bridge (the twin's "hands" on the OS — opt-in per agent)
  probeTether, callTetherAgent,
  // Swarms + chat
  listSwarms, getSwarm, deploySwarm, deleteSwarm,
  chatWithSwarm, chatWithSwarmResilient,
  saveConversation, loadConversation,
  // Offline queue
  isOnline, drainPendingQueue, pendingCount, clearPendingQueue,
};

})();
