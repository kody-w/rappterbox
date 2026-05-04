/* =====================================================================
 * rapp.js — RAPP v1 core library (browser, vanilla, no deps).
 *
 * Ports the canonical RAR seed/mnemonic/card model from rapp_sdk.py into
 * JavaScript so the virtual brainstem and the binder are wire-compatible
 * with the Python SDK. A card minted here resolves identically there.
 *
 * Modules:
 *   RAPP.Seed        — seed_hash, mulberry32, forge_seed, resolve_card_from_seed
 *   RAPP.Mnemonic    — seed_to_words, words_to_seed (7-word incantation)
 *   RAPP.Agent       — parseAgentSource, agentSourceToCard, cardToAgentSource
 *   RAPP.Card        — mintCard, cardSchemaCheck
 *   RAPP.Hash        — sha256Hex (Web Crypto)
 *   RAPP.Binder      — empty(), addCard, removeCard, exportJSON, importJSON
 * ===================================================================== */

(function (root) {
  'use strict';
  const RAPP = {};

  /* ───── Constants (mirror rapp_sdk.py) ─────────────────────────── */

  const AGENT_TYPES = {
    LOGIC:  { color: '#58a6ff', label: 'Logic'  },
    DATA:   { color: '#3fb950', label: 'Data'   },
    SOCIAL: { color: '#d29922', label: 'Social' },
    SHIELD: { color: '#f0f0f0', label: 'Shield' },
    CRAFT:  { color: '#f85149', label: 'Craft'  },
    HEAL:   { color: '#ff7eb3', label: 'Heal'   },
    WEALTH: { color: '#bc8cff', label: 'Wealth' },
  };
  const TYPE_NAMES = Object.keys(AGENT_TYPES);

  const CATEGORY_TYPE = {
    core: 'LOGIC', devtools: 'LOGIC',
    pipeline: 'DATA', integrations: 'DATA',
    productivity: 'SOCIAL', general: 'SOCIAL',
    federal_government: 'SHIELD', slg_government: 'SHIELD', it_management: 'SHIELD',
    manufacturing: 'CRAFT', energy: 'CRAFT', retail_cpg: 'CRAFT',
    healthcare: 'HEAL', human_resources: 'HEAL',
    financial_services: 'WEALTH', b2b_sales: 'WEALTH', b2c_sales: 'WEALTH',
    professional_services: 'WEALTH', software_digital_products: 'DATA',
  };
  const CATEGORY_LIST = Object.keys(CATEGORY_TYPE);

  const TYPE_WEAKNESS = {
    LOGIC: 'WEALTH', DATA: 'LOGIC', SOCIAL: 'DATA', SHIELD: 'SOCIAL',
    CRAFT: 'SHIELD', HEAL: 'CRAFT', WEALTH: 'HEAL',
  };
  const TYPE_RESISTANCE = {
    LOGIC: 'DATA', DATA: 'SOCIAL', SOCIAL: 'SHIELD', SHIELD: 'CRAFT',
    CRAFT: 'HEAL', HEAL: 'WEALTH', WEALTH: 'LOGIC',
  };
  const EVOLUTION = {
    experimental: { stage: 0, label: 'Seed',      icon: '🌱' },
    community:    { stage: 1, label: 'Base',      icon: '🌿' },
    verified:     { stage: 2, label: 'Evolved',   icon: '🔥' },
    official:     { stage: 3, label: 'Legendary', icon: '👑' },
  };
  const TIER_RARITY = { experimental: 'starter', community: 'core', verified: 'rare', official: 'mythic' };
  const RARITY_LABEL = { starter: 'Starter', core: 'Core', rare: 'Rare', mythic: 'Mythic' };
  const RARITY_FLOOR = { starter: 5, core: 10, rare: 30, mythic: 100 };

  const FLAVOR = [
    'Built for the ecosystem. Ready for the edge.',
    'One file. Infinite possibilities.',
    'Runs anywhere the RAPP runtime breathes.',
    'Forged in the registry. Trusted in production.',
    'A single-file agent. A single promise: perform.',
    'When the network calls, this agent answers.',
    'Data in. Insight out. No drama.',
    'The pipeline starts here.',
    'Born from a manifest. Raised in the registry.',
    'Your code, your agent, your card. Permanent.',
    'Not just code. Identity.',
    'The forge remembers every agent it ever touched.',
  ];

  /* ───── Seed math (mirrors rapp_sdk.py exactly) ────────────────── */

  // FNV-1a 32-bit then mix to make a stable non-negative 32-bit unsigned.
  function seedHash(s) {
    s = String(s);
    let h = 0x811c9dc5 >>> 0;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      // mul by FNV prime 16777619, mask to 32-bit
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    // small avalanche
    h ^= h >>> 13;
    h = Math.imul(h, 0x5bd1e995) >>> 0;
    h ^= h >>> 15;
    return h >>> 0;
  }

  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1) >>> 0;
      t ^= (t + Math.imul(t ^ (t >>> 7), t | 61)) >>> 0;
      return ((t ^ (t >>> 14)) >>> 0) / 0xFFFFFFFF;
    };
  }

  // Use BigInt for seeds to safely handle >32-bit shifts.
  function forgeSeed(name, category, tier, tags, deps) {
    const nameHash = BigInt(seedHash(String(name)) >>> 0);
    let catIdx = CATEGORY_LIST.indexOf(category);
    if (catIdx < 0) catIdx = 0;
    // Derive secondary type
    const types = deriveTypes(category, tags || []);
    let secondaryIdx = 7;
    if (types.length > 1) {
      const idx = TYPE_NAMES.indexOf(types[1]);
      if (idx >= 0) secondaryIdx = idx;
    }
    const tierMap = { experimental: 0, community: 1, verified: 2, official: 3 };
    const tierIdx = tierMap[tier] !== undefined ? tierMap[tier] : 1;
    const tagCount = Math.min(31, (tags || []).length);
    const depCount = Math.min(15, (deps || []).length);
    const tagHash = (tags && tags.length) ? (seedHash((tags || []).join(' ')) & 0x1FFF) : 0;

    const seed =
      (nameHash << 32n) |
      (BigInt(catIdx) << 27n) |
      (BigInt(secondaryIdx) << 24n) |
      (BigInt(tierIdx) << 22n) |
      (BigInt(tagCount) << 17n) |
      (BigInt(depCount) << 13n) |
      BigInt(tagHash);
    return seed;
  }

  function deriveTypes(category, tags) {
    const primary = CATEGORY_TYPE[category] || 'SOCIAL';
    const out = [primary];
    const tagStr = (tags || []).join(' ').toLowerCase();
    const HINTS = {
      LOGIC:  ['ai','ml','algorithm','compute','analysis','ast','parse','model','intelligence'],
      DATA:   ['data','pipeline','etl','sync','migration','import','export','extract','transform'],
      SOCIAL: ['email','chat','meeting','communication','demo','presentation','coach','assistant'],
      SHIELD: ['compliance','security','audit','governance','risk','regulatory','permit','license'],
      CRAFT:  ['inventory','supply','maintenance','production','manufacturing','field','dispatch'],
      HEAL:   ['patient','clinical','care','health','wellness','staff','credentialing','intake'],
      WEALTH: ['sales','revenue','pricing','deal','proposal','billing','financial','portfolio'],
    };
    for (const t of TYPE_NAMES) {
      if (t === primary) continue;
      if (HINTS[t].some(kw => tagStr.includes(kw))) { out.push(t); break; }
    }
    return out;
  }

  function resolveCardFromSeed(seedBig) {
    seedBig = (typeof seedBig === 'bigint') ? seedBig : BigInt(seedBig);
    // 32-bit name seed → no registry available offline; treat as preview
    if (seedBig < (1n << 32n)) seedBig = seedBig << 32n;

    const nameHash = Number((seedBig >> 32n) & 0xFFFFFFFFn);
    const catIdx = Number((seedBig >> 27n) & 0x1Fn);
    const secondaryIdx = Number((seedBig >> 24n) & 0x7n);
    const tierIdx = Number((seedBig >> 22n) & 0x3n);
    const tagCount = Number((seedBig >> 17n) & 0x1Fn);
    const depCount = Number((seedBig >> 13n) & 0xFn);
    const tagHash = Number(seedBig & 0x1FFFn);

    const category = CATEGORY_LIST[catIdx] || 'general';
    const primary = CATEGORY_TYPE[category] || 'SOCIAL';
    const tierList = ['experimental','community','verified','official'];
    const tier = tierList[tierIdx] || 'community';
    const rarity = TIER_RARITY[tier] || 'core';
    const evo = EVOLUTION[tier] || EVOLUTION.community;

    const types = [primary];
    if (secondaryIdx < TYPE_NAMES.length && TYPE_NAMES[secondaryIdx] !== primary) {
      types.push(TYPE_NAMES[secondaryIdx]);
    }

    // Stats — mirror Python override block exactly
    const rng = mulberry32(nameHash ^ 0x57415453);
    const tierBase = { experimental: 15, community: 30, verified: 50, official: 70 };
    const base = tierBase[tier] || 30;
    const tagBonus = Math.min(20, tagCount * 3);
    const depPenalty = Math.min(20, depCount * 5);
    const clamp = v => Math.max(10, Math.min(100, Math.floor(v)));
    const hp  = clamp(base + tagBonus + rng() * 25);
    const atk = clamp(base + tagBonus + rng() * 30);
    const dfs = clamp(base + rng() * 20);
    const spd = clamp(base + 20 - depPenalty + rng() * 25);
    const itl = clamp(base + tagBonus + rng() * 20);
    const stats = { hp, atk, def: dfs, spd, int: itl };

    // Abilities
    const POOL = {
      LOGIC:  [['Analyze',30],['Compute',25],['Parse',20],['Reason',35]],
      DATA:   [['Extract',20],['Transform',30],['Sync',25],['Pipeline',35]],
      SOCIAL: [['Assist',15],['Draft',25],['Coach',30],['Present',20]],
      SHIELD: [['Audit',25],['Enforce',35],['Monitor',20],['Certify',30]],
      CRAFT:  [['Build',30],['Optimize',35],['Schedule',20],['Dispatch',25]],
      HEAL:   [['Triage',25],['Screen',20],['Support',15],['Track',30]],
      WEALTH: [['Prospect',20],['Forecast',30],['Negotiate',35],['Close',40]],
    };
    const pool = POOL[primary] || [['Perform', 25]];
    const tierCount = { experimental: 1, community: 2, verified: 3, official: 3 };
    const count = tierCount[tier] || 2;
    const abRng = mulberry32(tagHash | (nameHash & 0xFF00));
    const abilities = [];
    const used = new Set();
    for (let i = 0; i < Math.min(count, pool.length); i++) {
      let idx = Math.floor(abRng() * pool.length);
      while (used.has(idx) && used.size < pool.length) idx = (idx + 1) % pool.length;
      used.add(idx);
      const [abName, baseDmg] = pool[idx];
      abilities.push({
        name: abName,
        text: '',
        cost: Math.floor(abRng() * 3) + 1,
        damage: baseDmg + Math.floor(abRng() * 15),
      });
    }

    const weakness = TYPE_WEAKNESS[primary] || 'LOGIC';
    const resistance = TYPE_RESISTANCE[primary] || 'DATA';
    const flavorRng = mulberry32(nameHash ^ tagHash);
    const flavor = FLAVOR[Math.floor(flavorRng() * FLAVOR.length)];
    const dual = types.length > 1 ? ` / ${AGENT_TYPES[types[1]].label}` : '';
    const typeLine = `Agent — ${AGENT_TYPES[primary].label}${dual}`;

    return {
      seed: seedBig.toString(),
      display_name: `Agent #${(nameHash & 0xFFFF).toString(16).toUpperCase().padStart(4,'0')}`,
      tier, rarity,
      rarity_label: RARITY_LABEL[rarity] || rarity,
      types,
      type_colors: types.map(t => AGENT_TYPES[t].color),
      hp, stats, abilities,
      weakness, weakness_label: AGENT_TYPES[weakness].label,
      resistance, resistance_label: AGENT_TYPES[resistance].label,
      retreat_cost: Math.min(4, depCount),
      evolution: evo, category, type_line: typeLine, flavor,
      floor_pts: RARITY_FLOOR[rarity] || 10,
      name_seed: nameHash,
      _resolved_from: 'seed',
    };
  }

  /* ───── Mnemonic — 7 words (1024-word list) ────────────────────── */
  // Embedded as a single string so the file stays self-contained.
  // Identical wordlist to rapp_sdk.py MNEMONIC_WORDS.

  const MNEMONIC_WORDS_RAW = "FORGE ANVIL BLADE RUNE SHARD SMELT TEMPER WELD CHISEL BRAND MOLD CAST STAMP ETCH CARVE BIND FUSE ALLOY INGOT RIVET CLASP THORN BARB SPIKE PRONG EDGE HONE GRIND QUENCH SEAR HAMMER STOKE FIRE FROST STORM TIDE QUAKE GUST BOLT SURGE BLAZE EMBER PYRE ASH SMOKE SPARK FLARE FLASH FLOOD GALE DRIFT MIST HAZE VOID FLUX PULSE WAVE SHOCK BURST CRACK ROAR HOWL ECHO BOOM THUNDER VENOM BLIGHT SCORCH SINGE CHAR WITHER ERODE CORRODE DISSOLVE OAK PINE MOSS FERN ROOT VINE BLOOM SEED GROVE GLEN VALE CRAG PEAK RIDGE GORGE CLIFF SHORE REEF DUNE MARSH CAVE LAIR DEN NEST HIVE BURROW STONE IRON STEEL GOLD JADE ONYX RUBY OPAL AMBER PEARL CORAL QUARTZ OBSIDIAN FLINT GRANITE BASALT COBALT CHROME BRONZE COPPER NICKEL RELIC TOTEM SIGIL GLYPH WARD CHARM BANE DOOM FATE OMEN ORACLE SAGE SEER MAGE DRUID SHAMAN WRAITH SHADE PHANTOM SPECTER GOLEM TITAN DRAKE WYRM GRYPHON SPHINX HYDRA CHIMERA SCROLL TOME CODEX LORE MYTH FABLE SAGA EPIC VERSE CHANT HYMN DIRGE OATH CREED VOW AURA MANA ETHER PRISM NEXUS VORTEX RIFT WARP BREACH PORTAL GATE VAULT SHRINE ALTAR CRYPT SPIRE STRIKE SLASH THRUST PARRY GUARD SHIELD HELM LANCE MACE PIKE STAFF WAND DAGGER BOW ARROW QUIVER ARMOR PLATE GAUNTLET VISOR CLOAK CAPE MANTLE AEGIS BULWARK RAMPART BASTION CITADEL KEEP TOWER FORT RALLY CHARGE SIEGE FLANK AMBUSH ROUT VALOR MIGHT FURY WRATH RAGE SPITE MALICE GRUDGE HAVOC CHAOS DASH LEAP SPRINT LUNGE DIVE SOAR GLIDE PROWL STALK CREEP SWIFT FLEET BRISK RAPID SPIN WHIRL TWIST COIL SPIRAL ORBIT ARC CURVE BEND LOOP DAWN DUSK NOON NIGHT SHADOW GLEAM GLOW SHINE BEAM RAY HALO LUSTER MURK GLOOM DREAD FELL GRIM STARK BLEAK PALE ASHEN DIRE GRAVE SOMBER EBON ABYSS SHRIEK WAIL RUMBLE HISS GROWL SNARL BARK BELLOW DRONE CHIME TOLL KNELL CLANG CLASH SNAP THUD RING PEAL GONG HORN DRUM FLUTE LYRE PIPE BOLD KEEN FIERCE STERN VAST DEEP GRAND PRIME NOBLE ROYAL SACRED ANCIENT PRIMAL ELDER PURE VIVID SHARP BRIGHT DARK WILD CALM STILL SILENT STRONG PROUD BRAVE WISE JUST RAW DENSE SOLID WHOLE CORE APEX ZENITH NADIR CUSP BRINK VERGE CREST STAR MOON SUN COMET NOVA SOLAR LUNAR ASTRAL COSMIC NEBULA QUASAR PULSAR ECLIPSE AURORA CORONA PHASE WANE WAX WOLF HAWK EAGLE RAVEN SERPENT VIPER COBRA FALCON STAG BEAR LION TIGER LYNX PANTHER CONDOR OSPREY CRANE HERON OWL CROW BULL BOAR RAM HORSE MARE STALLION AX ORB AWE OAT EEL URN ELK ELM ION IRE ORE AIM ZAP JAB JAW JET JOT JAG HEX HEW HUE HUM DIN DUB DYE FIN FIG FOG FUR GAP GEM GNU SOUL MIND WILL FORCE POWER CRAFT SKILL GRACE POISE NERVE GRIT METTLE VIGOR ZEAL VERVE PLUCK GUILE CUNNING WILE LURE TRAP SNARE BAIT DECOY RUSE FEINT GAMBIT PLOY MESA TARN FORD PASS BLUFF KNOLL MOOR HEATH STEPPE TUNDRA DELTA BASIN PLATEAU FJORD ISLE ATOLL STRAIT CHANNEL HARBOR COVE BAY GULF SOUND CREEK BROOK RAPIDS FALLS CASCADE ARCH DOME PILLAR COLUMN BRIDGE WALL MOAT RAMP LEDGE STEP STAIR HALL NAVE ALCOVE NICHE SILL TRUSS BRACE STRUT JOIST FRAME PLINTH DAIS THRONE QUEST HUNT TRIAL ORDEAL RITE RITUAL PACT BOND PLEDGE DECREE EDICT MANDATE LAW RULE REIGN CROWN SCEPTER BANNER EMBLEM BADGE MARK SEAL TOKEN GUILD CLAN TRIBE ORDER SECT COVEN LEGION HORDE SWARM PACK FLOCK BROOD CLUTCH CRUX JINX GLINT DINT STINT KNIT SLIT SPLIT WHIT CLEFT DEFT HEFT LEFT THEFT WEFT BEREFT SHAFT GRAFT RAFT DRAFT TUFT LOFT CROFT DROIT BRUNT BLUNT STUNT RUNT GRUNT FRONT FONT HAUNT GAUNT FLAUNT JAUNT TAUNT SALT MALT HALT JOLT COLT MOLT SMOLT QUALM BALM PSALM FARM HARM ALARM DISARM FOREARM PLUME FUME LOOM ZOOM GROOM BROOM VROOM SCARCE TERSE MORSE COURSE SOURCE NORSE REMORSE WRENCH CLENCH STENCH TRENCH DRENCH FRENCH BENCH TORCH PORCH MARCH LARCH STARCH SEARCH PERCH BIRCH CHURCH THATCH CATCH MATCH BATCH HATCH LATCH PATCH WATCH SCRATCH SNATCH HEDGE WEDGE DREDGE SLEDGE FRIDGE DANCE TRANCE GLANCE STANCE PRANCE CHANCE ADVANCE ENHANCE BLISS MISS KISS DISMISS AMISS REMISS WHISK DISK RISK FRISK TUSK MUSK RUSK HUSK CROAK SOAK STROKE SPOKE BROKE WOKE INVOKE EVOKE PROVOKE VEIN CHAIN PLAIN STRAIN GRAIN TRAIN BRAIN DOMAIN TERRAIN REMAIN TREAD SPREAD THREAD SHRED BRED SLED STEAD INSTEAD COST LOST HOST GHOST MOST POST ROAST TOAST COAST BOAST STREAM DREAM TEAM CREAM SCHEME THEME SUPREME EXTREME SWAY FRAY PRAY STRAY ARRAY DECAY RELAY CONVEY OBEY SURVEY BETRAY THROW FLOW SNOW GROW KNOW BELOW HOLLOW CLIMB RHYME SLIME THYME SUBLIME PARADIGM PROWESS DURESS FORTRESS MISTRESS COMPASS BYPASS SURPASS AMASS CRIMSON LINDEN MAIDEN WARDEN GOLDEN MOLTEN FROZEN CHOSEN WOVEN SCYTHE CRUCIBLE TORRENT TEMPEST MAELSTROM CINDER INFERNO TYPHOON CYCLONE GLACIER ICECAP PERMAFROST MONSOON SOLSTICE EQUINOX MERIDIAN TWILIGHT MIDNIGHT DAYBREAK SENTINEL WATCHER HUNTER RANGER SCOUT TRACKER SEEKER FINDER KEEPER BINDER ARBITER HERALD ENVOY REGENT PREFECT CONSUL MARSHAL VASSAL SQUIRE KNIGHT PALADIN CHAMPION PARAGON MONARCH SOVEREIGN OVERLORD WARLORD CHIEFTAIN PATRIARCH MATRIARCH PROPHET MYSTIC HERMIT ASCETIC NOMAD PILGRIM WANDERER EXILE OUTCAST ROGUE REBEL VAGRANT DRIFTER MARAUDER BRIGAND CORSAIR BUCCANEER RAIDER REAVER SLAYER BERSERKER GLADIATOR CENTURION LEGIONNAIRE TEMPLAR CRUSADER INQUISITOR ZEALOT HARBINGER AUGUR PORTENT PRESAGE AUGURY PROPHECY DIVINATION REVELATION EPIPHANY REMNANT VESTIGE ARTIFACT FRAGMENT SPLINTER SLIVER MORSEL CRUMB PARTICLE MOTE FILAMENT STRAND FIBER SINEW TENDON LIGAMENT MARROW ICHOR ELIXIR POTION TONIC SALVE REMEDY ANTIDOTE CURE PANACEA CATALYST REAGENT COMPOUND TINCTURE DISTILL INFUSE IMBUE ENCHANT CONJURE SUMMON BANISH DISPEL REVOKE ANNUL NEGATE NULLIFY SUNDER CLEAVE REND SHATTER FRACTURE RUPTURE PIERCE IMPALE SKEWER GORE MAIM RAVAGE DEVASTATE OBLITERATE ANNIHILATE ERADICATE PURGE EXPUNGE EFFACE EXTINGUISH QUELL SUBDUE VANQUISH CONQUER PREVAIL TRIUMPH ASCEND TRANSCEND EVOLVE AWAKEN ARISE EMERGE MANIFEST EMBODY HARNESS WIELD MASTER COMMAND PROCLAIM SANCTIFY CONSECRATE ANOINT BESTOW ENDOW BEQUEST LEGACY HEIRLOOM COVENANT COMPACT TREATY ACCORD ALLIANCE FEDERATION DOMINION REALM KINGDOM EMPIRE DYNASTY EPOCH AEON CYCLE HELIX MATRIX LATTICE TAPESTRY MOSAIC CIPHER ENIGMA RIDDLE PUZZLE LABYRINTH MAZE CAULDRON CHALICE GOBLET GRAIL TRIDENT MAUL FLAIL HALBERD GLAIVE RAPIER SABRE KATANA MACHETE SCIMITAR CUTLASS BROADSWORD GREATSWORD LONGBOW CROSSBOW BALLISTA CATAPULT TREBUCHET MIRAGE ILLUSION REVENANT LICH BANSHEE GHOUL VAMPIRE WEREWOLF GARGOYLE BASILISK KRAKEN LEVIATHAN BEHEMOTH COLOSSUS JUGGERNAUT MONOLITH OBELISK ZIGGURAT MINARET PAGODA DOLMEN BARROW CATACOMB DUNGEON PARAPET TURRET BATTLEMENT DRAWBRIDGE PORTCULLIS BARBICAN PALISADE CONDUIT PYLON TORQUE RATCHET LODESTONE KEYSTONE CAPSTONE BEDROCK SEQUOIA REDWOOD CYPRESS HEMLOCK WILLOW ASPEN HICKORY JUNIPER MAGNOLIA HAWTHORN SAFFRON MYRRH MANGROVE ORCHID THISTLE POPPY HEATHER JASMINE VECTOR SCALAR TENSOR FULCRUM PIVOT CRESCENT PINNACLE VERTEX";
  const MNEMONIC_WORDS = MNEMONIC_WORDS_RAW.split(/\s+/);
  if (MNEMONIC_WORDS.length !== 1024) {
    console.warn('[rapp.js] MNEMONIC_WORDS expected 1024, got', MNEMONIC_WORDS.length);
  }
  const WORD_TO_IDX = {};
  MNEMONIC_WORDS.forEach((w, i) => { WORD_TO_IDX[w] = i; });
  const MNEM_BITS = 10n;
  const MNEM_MASK = (1n << MNEM_BITS) - 1n;

  function seedToWords(seedBig) {
    seedBig = (typeof seedBig === 'bigint') ? seedBig : BigInt(seedBig);
    const out = [];
    let r = seedBig;
    for (let i = 0; i < 7; i++) {
      const idx = Number(r & MNEM_MASK);
      out.push(MNEMONIC_WORDS[idx]);
      r >>= MNEM_BITS;
    }
    return out.join(' ');
  }

  function wordsToSeed(mnemonic) {
    const words = String(mnemonic).replace(/-/g, ' ').trim().split(/\s+/).map(w => w.toUpperCase());
    if (words.length !== 7) throw new Error(`Mnemonic must be 7 words, got ${words.length}`);
    let seed = 0n;
    for (let i = 0; i < 7; i++) {
      const w = words[i];
      if (!(w in WORD_TO_IDX)) throw new Error(`Unknown word: ${w}`);
      seed |= BigInt(WORD_TO_IDX[w]) << (BigInt(i) * MNEM_BITS);
    }
    return seed;
  }

  /* ───── Hash (Web Crypto, async) ───────────────────────────────── */

  async function sha256Hex(text) {
    const enc = new TextEncoder().encode(text);
    const buf = await crypto.subtle.digest('SHA-256', enc);
    return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
  }

  /* ───── Agent source parsing ───────────────────────────────────── */

  // Parse a single-file agent.py and extract the parts we need without
  // executing it. Goal: filename → { className, name, manifest, hasManifest,
  // hasBasicAgent, hasPerform, source }.

  function parseAgentSource(source, hintFilename) {
    const r = {
      source,
      filename: hintFilename || null,
      className: null,
      name: null,
      manifest: null,
      hasManifest: false,
      hasBasicAgent: false,
      hasPerform: false,
      description: null,
      // parsed function-calling parameters schema (best-effort)
      parameters: { type: 'object', properties: {}, required: [] },
    };

    // class FooAgent(BasicAgent):
    const classMatch = source.match(/^\s*class\s+([A-Za-z_]\w*)\s*\(\s*BasicAgent\s*\)\s*:/m);
    if (classMatch) {
      r.className = classMatch[1];
      r.hasBasicAgent = true;
    }
    if (/def\s+perform\s*\(/.test(source)) r.hasPerform = true;

    // self.name = '...'
    const nameMatch = source.match(/self\.name\s*=\s*['"]([^'"]+)['"]/);
    if (nameMatch) r.name = nameMatch[1];

    // Extract __manifest__ dict — match the literal block via brace counting
    const mIdx = source.indexOf('__manifest__');
    if (mIdx >= 0) {
      const eq = source.indexOf('=', mIdx);
      const open = source.indexOf('{', eq);
      if (open >= 0) {
        let depth = 0, end = -1;
        for (let i = open; i < source.length; i++) {
          const c = source[i];
          if (c === '{') depth++;
          else if (c === '}') { depth--; if (depth === 0) { end = i; break; } }
        }
        if (end > open) {
          const dictStr = source.substring(open, end + 1);
          const parsed = pythonDictToJson(dictStr);
          if (parsed && typeof parsed === 'object') {
            r.manifest = parsed;
            r.hasManifest = true;
          }
        }
      }
    }

    // Try to lift description + parameters out of the metadata literal too.
    // Pass the already-extracted self.name in so the parser can resolve the
    // common `"name": self.name` reference inside self.metadata.
    const metaIdx = source.search(/self\.metadata\s*=/);
    if (metaIdx >= 0) {
      const open = source.indexOf('{', metaIdx);
      if (open >= 0) {
        let depth = 0, end = -1;
        for (let i = open; i < source.length; i++) {
          const c = source[i];
          if (c === '{') depth++;
          else if (c === '}') { depth--; if (depth === 0) { end = i; break; } }
        }
        if (end > open) {
          const meta = pythonDictToJson(source.substring(open, end + 1), { selfName: r.name });
          if (meta && typeof meta === 'object') {
            if (typeof meta.description === 'string') r.description = meta.description;
            if (meta.parameters && typeof meta.parameters === 'object') r.parameters = meta.parameters;
          }
        }
      }
    }

    return r;
  }

  // Convert a small Python dict literal to JSON. Best-effort but safe:
  // we reject anything with code-like identifiers (not None/True/False).
  function pythonDictToJson(src, ctx) {
    try {
      let s = src;
      // Resolve `self.name` references inside the dict (used in agent
      // metadata as `"name": self.name`) — the OG ran in Python where this
      // just works; we have to substitute the literal before JSON.parse.
      const selfName = ctx && ctx.selfName;
      if (selfName) {
        s = s.replace(/\bself\.name\b/g, JSON.stringify(selfName));
      }
      // Strip any other unresolved `self.<ident>` so the parser doesn't choke
      // on agent-specific runtime references that aren't part of the schema.
      s = s.replace(/\bself\.[A-Za-z_]\w*/g, 'null');
      // Replace Python literals
      s = s.replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false').replace(/\bNone\b/g, 'null');
      // Convert tuples (...) → arrays. (Rare in manifests but safe.)
      // Replace single-quoted strings with double-quoted, handling escapes.
      // We do it via a tiny tokenizer to avoid breaking strings that contain quotes.
      let out = '';
      let i = 0;
      while (i < s.length) {
        const c = s[i];
        if (c === '#') {
          // skip Python comment to end of line
          while (i < s.length && s[i] !== '\n') i++;
        } else if (c === "'" || c === '"') {
          const quote = c;
          out += '"';
          i++;
          while (i < s.length && s[i] !== quote) {
            if (s[i] === '\\') {
              const next = s[i+1] || '';
              if (next === quote) {
                // \" or \' in source → emit the bare char; we're rewriting to ".."
                if (next === '"') { out += '\\"'; }      // " inside double-quoted: keep escape for JSON
                else { out += next; }                    // ' inside single-quoted: no escape needed in JSON
                i += 2;
              } else if (next === '"' && quote === "'") {
                // raw \" inside a single-quoted source string → literal " → escape for JSON
                out += '\\"'; i += 2;
              } else if (next === '\\') { out += '\\\\'; i += 2; }
              else if (next === 'n')  { out += '\\n';  i += 2; }
              else if (next === 't')  { out += '\\t';  i += 2; }
              else if (next === 'r')  { out += '\\r';  i += 2; }
              else { out += s[i] + next; i += 2; }
            } else if (s[i] === '"' && quote === "'") { out += '\\"'; i++; }
            else if (s[i] === '\n') { out += '\\n'; i++; }
            else { out += s[i]; i++; }
          }
          out += '"';
          i++;
        } else {
          out += c;
          i++;
        }
      }
      // Python implicit string concatenation: `"foo" "bar"` (or split across
      // lines / wrapped in parens) → one combined string. Repeat until stable
      // because three+ adjacent strings need multiple passes.
      let prev;
      do {
        prev = out;
        out = out.replace(/"((?:[^"\\]|\\.)*)"\s*"((?:[^"\\]|\\.)*)"/g, '"$1$2"');
      } while (out !== prev);
      // A leftover `("string")` from a parenthesized concat → just the string.
      out = out.replace(/\(\s*("(?:[^"\\]|\\.)*")\s*\)/g, '$1');
      // Strip trailing commas before } or ]
      out = out.replace(/,(\s*[}\]])/g, '$1');
      return JSON.parse(out);
    } catch (e) {
      return null;
    }
  }

  /* ───── Card mint (from source or manifest) ────────────────────── */

  async function mintCard(source, filenameHint) {
    const parsed = parseAgentSource(source, filenameHint);
    const filename = filenameHint || (parsed.name ? toSnakeCase(parsed.name) + '_agent.py' : 'unknown_agent.py');
    const sha = await sha256Hex(source);

    const manifest = parsed.manifest || synthManifest(parsed, filename);
    const seed = forgeSeed(
      manifest.name,
      manifest.category,
      manifest.quality_tier || 'community',
      manifest.tags || [],
      manifest.dependencies || []
    );
    const card = resolveCardFromSeed(seed);
    // enrich
    card.name = manifest.name;
    card.display_name = manifest.display_name || parsed.name || manifest.name;
    card.version = manifest.version || '1.0.0';
    card.description = manifest.description || parsed.description || '';
    card.author = manifest.author || '';
    card.tags = manifest.tags || [];
    card.power = card.stats.atk;
    card.toughness = card.stats.def;
    card.incantation = seedToWords(BigInt(seed));
    card._resolved_from = parsed.hasManifest ? 'manifest' : 'synthesized';

    return {
      schema: 'rapp-card/1.0',
      name: manifest.name,
      filename,
      source,
      sha256: sha,
      manifest,
      card,
      parsed: {
        className: parsed.className,
        agentName: parsed.name,
        hasManifest: parsed.hasManifest,
        hasBasicAgent: parsed.hasBasicAgent,
        hasPerform: parsed.hasPerform,
      },
    };
  }

  function synthManifest(parsed, filename) {
    const slugBase = (parsed.name || filename.replace(/_agent\.py$/, '') || 'unknown').toLowerCase();
    return {
      schema: 'rapp-agent/1.0',
      name: `@local/${slugBase}`,
      version: '0.0.0',
      display_name: parsed.name || slugBase,
      description: parsed.description || '',
      author: 'unknown',
      tags: [],
      category: 'general',
      quality_tier: 'experimental',
      requires_env: [],
    };
  }

  function toSnakeCase(s) {
    return String(s)
      .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
      .replace(/[^A-Za-z0-9]+/g, '_')
      .toLowerCase()
      .replace(/^_+|_+$/g, '');
  }

  /* ───── Card → agent.py reconstruction ─────────────────────────── */
  // The card.source field IS the agent file. cardToAgentSource simply
  // returns it unchanged, after sha256 verification.

  async function cardToAgentSource(cardObj) {
    if (!cardObj || cardObj.schema !== 'rapp-card/1.0') {
      throw new Error('Not a rapp-card/1.0 object');
    }
    if (typeof cardObj.source !== 'string') {
      throw new Error('Card has no embedded source');
    }
    const sha = await sha256Hex(cardObj.source);
    if (cardObj.sha256 && sha !== cardObj.sha256) {
      throw new Error(`SHA-256 mismatch — card.sha256=${cardObj.sha256}, computed=${sha}`);
    }
    return { filename: cardObj.filename, source: cardObj.source };
  }

  /* ───── Binder ─────────────────────────────────────────────────── */

  function emptyBinder(owner) {
    return {
      schema: 'rapp-binder/1.0',
      owner: owner || '@local/you',
      exported_at: new Date().toISOString(),
      cards: [],
    };
  }

  function binderAddCard(binder, card) {
    if (!binder || binder.schema !== 'rapp-binder/1.0') throw new Error('Not a binder');
    if (!card || card.schema !== 'rapp-card/1.0') throw new Error('Not a card');
    binder.cards = binder.cards || [];
    // Replace if same filename or same name
    const idx = binder.cards.findIndex(c => c.filename === card.filename || c.name === card.name);
    if (idx >= 0) binder.cards[idx] = card; else binder.cards.push(card);
    binder.exported_at = new Date().toISOString();
    return binder;
  }

  function binderRemoveCard(binder, predicate) {
    binder.cards = (binder.cards || []).filter(c => !predicate(c));
    return binder;
  }

  /* ───── Voice playback helpers ─────────────────────────────────
   * Pure, DOM-free helpers for picking what TTS should say given a
   * parsed chat response { text, voice }. Lives here (not in
   * index.html) so the Node test runner can assert the contract.
   *
   * Rule (Path B from the legacy-parity exercise): prefer the
   * `|||VOICE|||` block — it's the assistant's distilled spoken line.
   * If empty, fall back to a stripped, truncated version of the main
   * reply so a user with voice mode toggled on still hears something.
   * The fallback is a frontend-only choice: the agent doesn't know
   * the user enabled local playback. ──────────────────────────── */
  function stripMarkdownForVoice(s) {
    if (!s) return '';
    return s
      .replace(/```[\s\S]*?```/g, ' ')                  // fenced code blocks → drop
      .replace(/`([^`]+)`/g, '$1')                      // inline code → unwrap
      .replace(/!\[[^\]]*\]\([^)]+\)/g, '')             // images → drop
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')          // links → label only
      .replace(/^\s{0,3}#{1,6}\s+/gm, '')               // ATX headings
      .replace(/^\s*>\s?/gm, '')                        // blockquote markers
      .replace(/^\s*[-*+]\s+/gm, '')                    // bullet markers
      .replace(/^\s*\d+\.\s+/gm, '')                    // ordered list markers
      .replace(/\*\*([^*]+)\*\*/g, '$1')                // **bold**
      .replace(/__([^_]+)__/g, '$1')                    // __bold__
      .replace(/\*([^*\n]+)\*/g, '$1')                  // *italic*
      .replace(/(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])/g, '$1') // _italic_
      .replace(/\|/g, ' ')                              // table pipes
      .replace(/([.!?])\s*\n{2,}/g, '$1 ')              // sentence-end then break → collapse
      .replace(/\n{2,}/g, '. ')                         // bare paragraph break → sentence
      .replace(/\n/g, ' ')                              // soft newlines
      .replace(/\s+/g, ' ')
      .trim();
  }

  // Cap fallback length so a 5-page essay doesn't get read aloud.
  // ~280 chars ≈ 2-3 sentences in browser TTS, plenty to give the
  // user something audible when the model forgot the voice block.
  const VOICE_FALLBACK_MAX = 280;

  function pickVoiceText(resp) {
    if (!resp) return '';
    const v = (resp.voice || '').trim();
    if (v) return v;
    const stripped = stripMarkdownForVoice(resp.text || '');
    if (!stripped) return '';
    if (stripped.length <= VOICE_FALLBACK_MAX) return stripped;
    // Cut at a sentence boundary if we can find one before the cap.
    const cut = stripped.slice(0, VOICE_FALLBACK_MAX);
    const lastBreak = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('! '), cut.lastIndexOf('? '));
    return (lastBreak > 80 ? cut.slice(0, lastBreak + 1) : cut).trim();
  }

  /* ───── Export ─────────────────────────────────────────────────── */

  RAPP.AGENT_TYPES = AGENT_TYPES;
  RAPP.TIER_RARITY = TIER_RARITY;
  RAPP.RARITY_LABEL = RARITY_LABEL;
  RAPP.EVOLUTION = EVOLUTION;
  RAPP.Seed = { seedHash, mulberry32, forgeSeed, resolveCardFromSeed, deriveTypes };
  RAPP.Mnemonic = { seedToWords, wordsToSeed, MNEMONIC_WORDS };
  RAPP.Hash = { sha256Hex };
  RAPP.Agent = { parseAgentSource, pythonDictToJson, toSnakeCase };
  RAPP.Card = { mintCard, cardToAgentSource };
  RAPP.Binder = { empty: emptyBinder, addCard: binderAddCard, removeCard: binderRemoveCard };
  RAPP.Voice = { stripMarkdownForVoice, pickVoiceText, VOICE_FALLBACK_MAX };

  if (typeof module !== 'undefined' && module.exports) module.exports = RAPP;
  root.RAPP = RAPP;
})(typeof window !== 'undefined' ? window : globalThis);
