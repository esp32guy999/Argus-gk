// ── Argus shell — canvas, roster, views. Chat lives in chat.js. ──

const BRAIN = '/argus';

// ── State ────────────────────────────────────────────────────────────
const HISTORY_PAGE = 100;

const state = {
  models:         [],
  modelCfg:       {},
  currentModel:   null,
  conversationId: null,
  conversations: [],
  sending:        false,
  participants:   [],
  addressed:      [],
  newestMsgId:    null,
  gkActive:       false,
  persona:        'argus',
  personas:       [{id: 'argus', display: 'Argus'}],
  rtTo:           'both',
  currentView:    'chat',
  pendingBubbleId:null,     // cancel id only — never a render source
  statusPill:     null,
  thinkingCanvas: null,
  oldestMsgId:    null,
  historyExhausted:false,
  historyLoading: false,
};

// ── DOM refs ─────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const messagesEl   = $('chat-messages');
const inputEl      = $('chat-input');
const sendBtn      = $('chat-send');
const modelSelect  = $('model-select');
const personaSelect = $('persona-select');
const sideToggle   = $('side-toggle');
const sideNav      = $('side-nav');
const mainEl       = $('main');
const canvasBody   = $('canvas-body');
const canvasTabbar = $('canvas-tabbar');
const addWidgetBtn = $('add-widget-btn');
const brainDot     = $('brain-status');

// ── Utilities ────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function safeUrl(u) {
  return /^https?:\/\//i.test(u || '') ? u : '#';
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function stripCommandTags(text) {
  // Strip media/command tags (e.g. [[IMAGE:]]/[[VIDEO:]], rendered by processCommandTags)
  // but LEAVE the interactive button markers — [[CHOICES:...]] and [[LINKS:...]] are
  // consumed + removed by _renderChoices/_renderLinks. Stripping them here (as this did
  // before) killed the buttons in every render path.
  return String(text || '').replace(/\[\[(?!CHOICES:|LINKS:)[^\]]+\]\]/g, '').trim();
}
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}
function fmtDay(ts) {
  return new Date(ts).toLocaleDateString('en-US',
    { weekday: 'long', month: 'short', day: 'numeric' });
}
// Friendly display name for a model id (falls back to the raw id).
// The model a SEND actually routes to: Grok (SuperGrok) when the GK toggle is on,
// otherwise the dropdown selection. The dropdown keeps showing the local model
// either way — GK is an overlay switch, not a dropdown entry.
const GK_MODEL = 'grok';
function isLocalGpuModel(id) {
  return !!(id && id !== GK_MODEL && id !== 'claude-code' && id !== 'claude'
            && !String(id).startsWith('z-')
            && !(state.modelCfg[id] && state.modelCfg[id].external));
}
function sendTargets() {
  // GK is the only way to pick Grok. The dropdown is the only way to pick a local.
  // Chips / roster must not override that — leftover addressed=[grok] made GK a no-op.
  if (state.gkActive) return [GK_MODEL];
  return state.currentModel ? [state.currentModel] : [];
}
function activeModel() {
  const t = sendTargets();
  return t[0] || state.currentModel;
}
function isStandalonePwa() {
  return !!(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
    || !!window.navigator.standalone;
}
function modelLabel(id) {
  return (id && state.modelCfg && state.modelCfg[id] && state.modelCfg[id].display) || id || '';
}
// The model's accent hex (for the bubble side-bars), grey fallback for unmapped/unknown.
// Accent palette name for a model: prefer the manifest value from /models (so a new
// model is coloured without editing app.js), fall back to the local MODEL_ACCENT map.
function _accentName(id) {
  return (state.modelCfg && state.modelCfg[id] && state.modelCfg[id].accent) || MODEL_ACCENT[id];
}
function modelColor(id) {
  const a = ACCENTS[_accentName(id)];
  return (a && a[0]) || '#888';
}
async function fetchJson(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ── Thinking animation (doodler — blue→purple wandering lines) ──────
// ── Thinking Doodler Themes ──────────────────────────────────────────
const DOODLE_THEMES = {
  aurora:    { name: 'Aurora',    bg: [28, 28, 46], colors: [[80,140,255],[100,120,245],[130,100,240],[155,85,235],[175,75,225],[190,90,240],[140,160,255]] },
  ember:     { name: 'Ember',     bg: [46, 28, 20], colors: [[249,115,22],[239,68,68],[251,146,60],[234,179,8],[220,38,38],[255,160,80],[200,80,40]] },
  matrix:    { name: 'Matrix',    bg: [20, 36, 28], colors: [[34,197,94],[16,185,129],[74,222,128],[6,182,212],[45,212,160],[80,250,123],[20,160,100]] },
  synthwave: { name: 'Synthwave', bg: [40, 20, 46], colors: [[236,72,153],[192,38,211],[244,114,182],[168,85,247],[219,39,119],[255,100,200],[180,60,230]] },
  solar:     { name: 'Solar',     bg: [46, 38, 20], colors: [[234,179,8],[251,191,36],[245,158,11],[253,224,71],[217,119,6],[255,200,60],[200,160,30]] },
};
const DOODLE_THEME_ORDER = ['aurora','ember','matrix','synthwave','solar'];

function getDoodleTheme() {
  const saved = localStorage.getItem('argus-doodle-theme');
  return (saved && DOODLE_THEMES[saved]) ? saved : 'aurora';
}

// Status pill: a single themed chip showing phase + a live elapsed timer, with
// stall/crash escalation. CSS-only animation (opacity breathe) + one 1s interval —
// far cheaper than the old rAF canvas, and it actually reports liveness:
//   ticking timer = alive · climbs past STALL = "still working" (amber) ·
//   past CRASH = "no response" (red, dot stops) · frozen timer = the SSE died.
function createStatusPill() {
  // Wrap = the always-on pill chip + a collapsed activity panel beneath it.
  // The pill stays the default health gauge for every model; for the claude-code
  // path it becomes tappable and reveals the live thinking/tool activity stream.
  const wrap  = document.createElement('div');
  wrap.className = 'status-pill-wrap';
  const pill  = document.createElement('div');
  pill.className = 'status-pill';
  const dot   = document.createElement('span'); dot.className = 'sp-dot';
  const label = document.createElement('span'); label.className = 'sp-label'; label.textContent = 'thinking';
  const time  = document.createElement('span'); time.className = 'sp-time'; time.textContent = '0s';
  const dur   = document.createElement('span'); dur.className = 'sp-dur';   // turn time, filled from the done event
  const caret = document.createElement('span'); caret.className = 'sp-caret'; caret.textContent = '▸';
  pill.append(dot, label, time, dur, caret);
  const panel = document.createElement('div'); panel.className = 'sp-activity';
  wrap.append(pill, panel);

  const start = Date.now();
  let lastActivity = start, gotToken = false, phase = '', hasActivity = false;
  // Longer stall windows: Grok/CC tool turns often go quiet for 30–90s while a
  // shell command runs. "no response" only after a true blackout.
  const STALL = 45, CRASH = 180;  // seconds since the last signal

  function tick() {
    const elapsed = Math.round((Date.now() - start) / 1000);
    const since = (Date.now() - lastActivity) / 1000;
    time.textContent = elapsed + 's';
    pill.classList.remove('warn', 'crit');
    if (since >= CRASH)      { pill.classList.add('crit'); label.textContent = 'no response'; }
    else if (since >= STALL) { pill.classList.add('warn'); label.textContent = 'still working'; }
    else                     { label.textContent = phase || (gotToken ? 'responding' : 'thinking'); }
  }
  const timer = setInterval(tick, 1000);
  tick();

  // Tap to expand — only meaningful once activity has streamed (claude-code / grok).
  pill.addEventListener('click', () => {
    if (!hasActivity) return;
    wrap.classList.toggle('open');
    if (wrap.classList.contains('open')) panel.scrollTop = panel.scrollHeight;
  });

  wrap._activity = () => { lastActivity = Date.now(); gotToken = true; phase = ''; tick(); }; // token: streaming supersedes any tool label
  wrap._status   = (txt) => { phase = txt; lastActivity = Date.now(); tick(); };               // tool/loop labels
  wrap._event    = (ev) => {                                                                   // OAuth-agent activity stream
    if (!ev || ev.kind === 'done') return;
    // Heartbeat: thinking/tool deltas used to update the panel WITHOUT refreshing
    // lastActivity, so the pill flipped to "no response" while Grok was still busy.
    lastActivity = Date.now();
    if (ev.kind === 'thinking' && !phase) phase = 'thinking';
    if (ev.kind === 'tool_use') phase = `using ${ev.name || 'tool'}`;
    if (ev.kind === 'tool_result') phase = phase && phase.startsWith('using') ? phase : 'working';
    hasActivity = true; pill.classList.add('expandable');
    appendActivityEntry(panel, ev);
    if (wrap.classList.contains('open')) panel.scrollTop = panel.scrollHeight;
    tick();
  };
  wrap._dur      = (txt) => { dur.textContent = txt; };                                         // "· 12s"
  wrap._collapse = () => { wrap.classList.remove('open'); };
  wrap._stop     = () => { clearInterval(timer); wrap.remove(); };
  // External heartbeat (turn_status poll) keeps the pill alive when SSE drops.
  wrap._heartbeat = (txt) => {
    lastActivity = Date.now();
    if (txt) phase = txt;
    tick();
  };
  return wrap;
}

// Render one activity entry into the expandable panel. CSS-light, append-only DOM.
// Thinking tokens arrive as many tiny deltas — coalesce into the last think row
// so a long reason stream doesn't create thousands of DOM nodes (UI freeze).
function appendActivityEntry(panel, ev) {
  if (ev.kind === 'thinking') {
    const last = panel.lastElementChild;
    const piece = ev.text || '';
    if (last && last.classList.contains('act-think')) {
      // Keep a rolling tail so the panel stays bounded.
      const next = (last.textContent || '') + piece;
      last.textContent = next.length > 4000 ? next.slice(-4000) : next;
      return;
    }
    const row = document.createElement('div');
    row.className = 'act-think';
    row.textContent = piece.slice(0, 4000);
    panel.appendChild(row);
    return;
  }
  const row = document.createElement('div');
  if (ev.kind === 'tool_use') {
    row.className = 'act-tool';
    row.textContent = `🔧 ${ev.name || 'tool'}(${fmtToolInput(ev.input)})`;
  } else if (ev.kind === 'tool_result') {
    row.className = 'act-result';
    row.textContent = (ev.text || '').slice(0, 2000);
  } else {
    return;
  }
  panel.appendChild(row);
  // Cap activity panel growth (tool spam on long agent jobs).
  while (panel.childElementCount > 80) panel.removeChild(panel.firstChild);
}

// Compact one-line summary of a tool's input args for the activity panel.
function fmtToolInput(input) {
  if (!input || typeof input !== 'object') return '';
  try {
    const s = JSON.stringify(input);
    return s.length > 80 ? s.slice(0, 79) + '…' : s;
  } catch { return ''; }
}

// "· 12s" turn time from the done event's duration_ms (may be missing).
function fmtDuration(durationMs) {
  if (typeof durationMs !== 'number') return '';
  return '· ' + Math.round(durationMs / 1000) + 's';
}

function createThinkingCanvas() {
  let themeName = getDoodleTheme();
  let theme = DOODLE_THEMES[themeName];

  const canvas = document.createElement('canvas');
  canvas.className = 'thinking-canvas';
  canvas.style.cssText = 'display:block;width:100%;height:60px;border-radius:8px;margin:2px 0;cursor:pointer;';
  canvas.title = `Theme: ${theme.name} — click to change`;
  const ctx = canvas.getContext('2d');

  let BG = theme.bg;
  let palette = theme.colors;

  let w = 400, h = 50;
  canvas.width = w * 2; canvas.height = h * 2;
  ctx.scale(2, 2);
  ctx.fillStyle = `rgb(${BG[0]},${BG[1]},${BG[2]})`;
  ctx.fillRect(0, 0, w, h);

  const pens = [];
  let sized = false;

  function spawnPen() {
    const ci = Math.floor(Math.random() * palette.length);
    return {
      x: Math.random() * w,
      y: Math.random() * h,
      angle: Math.random() * Math.PI * 2,
      speed: 0.8 + Math.random() * 1.4,
      wobble: 0.06 + Math.random() * 0.15,
      life: 150 + Math.floor(Math.random() * 250),
      age: 0,
      color: palette[ci],
      lineWidth: 1.5 + Math.random() * 2.5,
    };
  }
  for (let i = 0; i < 5; i++) pens.push(spawnPen());

  // Click to cycle themes
  canvas.addEventListener('click', () => {
    const idx = DOODLE_THEME_ORDER.indexOf(themeName);
    themeName = DOODLE_THEME_ORDER[(idx + 1) % DOODLE_THEME_ORDER.length];
    theme = DOODLE_THEMES[themeName];
    BG = theme.bg;
    palette = theme.colors;
    localStorage.setItem('argus-doodle-theme', themeName);
    canvas.title = `Theme: ${theme.name} — click to change`;
    // Recolor existing pens
    for (const p of pens) { p.color = palette[Math.floor(Math.random() * palette.length)]; }
    // Flash-clear to new BG
    ctx.fillStyle = `rgb(${BG[0]},${BG[1]},${BG[2]})`;
    ctx.fillRect(0, 0, w, h);
  });

  let raf = 0;
  let frame = 0;
  let stopped = false;

  function draw() {
    if (stopped) return;
    const parent = canvas.parentElement;
    if (parent && !sized) {
      const pw = parent.clientWidth - 28;
      if (pw > 50) {
        w = pw; h = 50;
        canvas.width = w * 2; canvas.height = h * 2;
        ctx.setTransform(2, 0, 0, 2, 0, 0);
        ctx.fillStyle = `rgb(${BG[0]},${BG[1]},${BG[2]})`;
        ctx.fillRect(0, 0, w, h);
        for (const p of pens) { p.x = Math.random() * w; p.y = Math.random() * h; }
        sized = true;
      }
    }

    ctx.fillStyle = `rgba(${BG[0]},${BG[1]},${BG[2]},0.03)`;
    ctx.fillRect(0, 0, w, h);

    for (let i = pens.length - 1; i >= 0; i--) {
      const p = pens[i];
      p.angle += (Math.random() - 0.5) * p.wobble;
      if (p.x < 8)     p.angle += 0.15;
      if (p.x > w - 8) p.angle -= 0.15;
      if (p.y < 4)     p.angle += 0.15;
      if (p.y > h - 4) p.angle -= 0.15;

      const nx = p.x + Math.cos(p.angle) * p.speed;
      const ny = p.y + Math.sin(p.angle) * p.speed;
      const cx = Math.max(1, Math.min(w - 1, nx));
      const cy = Math.max(1, Math.min(h - 1, ny));

      const fadeIn = Math.min(p.age / 20, 1);
      const fadeOut = Math.min((p.life - p.age) / 30, 1);
      const alpha = Math.max(0, Math.min(fadeIn, fadeOut)) * 0.7;

      const [r, g, b] = p.color;
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(cx, cy);
      ctx.strokeStyle = `rgba(${r},${g},${b},${alpha})`;
      ctx.lineWidth = p.lineWidth;
      ctx.lineCap = 'round';
      ctx.stroke();

      if (frame % 6 === 0 && alpha > 0.3) {
        ctx.beginPath();
        ctx.arc(cx, cy, p.lineWidth * 1.8, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${r},${g},${b},${alpha * 0.3})`;
        ctx.fill();
      }

      p.x = cx; p.y = cy; p.age++;
      if (p.age >= p.life) { pens[i] = spawnPen(); }
    }

    frame++;
    raf = requestAnimationFrame(draw);
  }

  requestAnimationFrame(() => { raf = requestAnimationFrame(draw); });
  canvas._stopThinking = () => {
    if (stopped) return;
    stopped = true;
    cancelAnimationFrame(raf);
    raf = 0;
    try { canvas.remove(); } catch {}
  };
  return canvas;
}

/** Start both thinking indicators for a turn (status pill + doodler).
 *  Inserted just before `beforeEl` (usually the empty typing bubble) so the
 *  doodle sits where the reply will appear. */
function startThinkingIndicators(beforeEl) {
  // Live-turn bubble in chat.js owns turn chrome.
  return { statusPill: null, canvas: null };
}

function stopThinkingDoodle() {
  if (state.thinkingCanvas) {
    try { state.thinkingCanvas._stopThinking(); } catch {}
    state.thinkingCanvas = null;
  }
}

function stopThinkingIndicators() {
  stopThinkingDoodle();
  if (state.statusPill) {
    try { state.statusPill._stop(); } catch {}
    state.statusPill = null;
  }
}

// ── Boot ─────────────────────────────────────────────────────────────
async function init() {
  await loadModels();
  await loadConversations();
  await loadHistory();
  await loadLayout();
  if (window.ArgusChat) ArgusChat.startPolling();
  wireUI();
  wirePresence();
  // Kick off view-specific polls lazily; they'll hydrate on tab switch too.
  loadBriefing();
}

// ── PWA focus heartbeat (reply toast when minimized) ─────────────────
// Android Chrome freezes the PWA when it's not on screen, so the server
// cannot ask at reply time. We report visibility here; hide uses
// `keepalive` so the POST still leaves as the app backgrounds.
function pwaVisible() {
  return document.visibilityState === 'visible';
}
function reportPresence(visible) {
  const v = (visible === undefined) ? pwaVisible() : !!visible;
  try {
    fetch(`${BRAIN}/presence`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({visible: v}),
      keepalive: true,
    }).catch(() => {});
  } catch (_) { /* ignore */ }
}
function wirePresence() {
  reportPresence();
  document.addEventListener('visibilitychange', () => reportPresence());
  window.addEventListener('pageshow', () => reportPresence(true));
  window.addEventListener('pagehide', () => reportPresence(false));
  document.addEventListener('freeze', () => reportPresence(false));
  document.addEventListener('resume', () => reportPresence());
  setInterval(() => { if (pwaVisible()) reportPresence(true); }, 15000);
}

// ── Per-model accent ──────────────────────────────────────────────────
// Theme B: the active model owns ONLY the accent (nav eye focal + highlights);
// the base theme (data-theme) still owns bg/surfaces and the manual toggle is
// untouched. Inline custom props on <html> override the theme's --accent, so the
// accent reads as "which model is active" regardless of the chosen base theme.
const ACCENTS = {
  orange: ['#f97316', 'rgba(249,115,22,.15)',  'rgba(249,115,22,.25)'],
  blue:   ['#3b82f6', 'rgba(59,130,246,.15)',  'rgba(59,130,246,.25)'],
  red:    ['#ef4444', 'rgba(239,68,68,.15)',   'rgba(239,68,68,.25)'],
  green:  ['#22c55e', 'rgba(34,197,94,.15)',   'rgba(34,197,94,.25)'],
  violet: ['#a855f7', 'rgba(168,85,247,.15)',  'rgba(168,85,247,.25)'],
  yellow: ['#eab308', 'rgba(234,179,8,.15)',   'rgba(234,179,8,.25)'],
  pink:   ['#e8a0bf', 'rgba(232,160,191,.14)', 'rgba(232,160,191,.22)'],
  teal:   ['#14b8a6', 'rgba(20,184,166,.15)',  'rgba(20,184,166,.25)'],
  gold:   ['#f5c542', 'rgba(245,197,66,.18)',  'rgba(245,197,66,.30)'],   // 🥇 bake-off champion
  emerald:['#10b981', 'rgba(16,185,129,.16)',  'rgba(16,185,129,.28)'],   // Loki — the uncensored 35B
};
const MODEL_ACCENT = {
  'claude-code':      'orange',  // Claude — the "home" accent
  'qwen3-next-80b':   'violet',  // Argus local 80B — matches the indigo/violet eye
  'qwen3-coder-30b':  'gold',    // 🥇 bake-off winner — the daily coder
  'qwen3.6-35b-a3b':  'teal',    // Qwen3.6 MoE — fast no-think chat model (replaced GLM)
  'gemma4-26b':     'pink',     // "comfy" accent
  'gemma4-12b':     'green',
  'gpt-oss-20b':    'yellow',
  'bonsai-8b':      'blue',
  'lfm2.5-8b':      'red',
  'ornith-35b-uncensored': 'emerald',  // Loki — uncensored daily driver (docs/model-ornith.md)
  'claude':           'blue',    // Claude at the Roundtable (via the :8100 shim; distinct from claude-code's orange)
  'grok':             'blue',    // Grok (xAI cloud) — accent also set in config/models.yaml
  // z-engineer intentionally unmapped (media model, tracked in docs/ISSUES.md) → falls back
};
// ── Roundtable ─────────────────────────────────────────────────────────────
// A three-way room (Shane · Gemma · Claude) living in a fixed conversation id.
// Its replies stream over the same SSE bubbles as chat, but are routed by id
// through state.rtBubbles instead of the single-pending path.
const RT_CID = 'roundtable';
const RT_SPEAKER = { 'gemma4-26b': 'Gemma', 'claude': 'Claude' };  // model id -> display name
function isRoundtable() { return state.conversationId === RT_CID; }
function applyModelAccent(id) {
  const a = ACCENTS[_accentName(id)] || ACCENTS.orange;
  const s = document.documentElement.style;
  s.setProperty('--accent', a[0]);
  s.setProperty('--accent-glow', a[1]);
  s.setProperty('--accent-shadow', a[2]);
}

// ── Model warm-up (slow-load models: warm on select, buzz + pill when ready) ──
function _setModelWarm(kind, text) {
  const el = $('model-warm');
  if (!el) return;
  if (!kind) { el.hidden = true; clearTimeout(el._t); return; }
  const icon = kind === 'loading' ? '⏳ ' : kind === 'ready' ? '🟢 ' : '⚠️ ';
  el.textContent = icon + text;
  el.className = 'model-warm ' + kind;
  el.hidden = false;
  clearTimeout(el._t);
  if (kind !== 'loading') el._t = setTimeout(() => { el.hidden = true; }, 6000);
}

function maybeWarmModel(id) {
  const cfg = state.modelCfg && state.modelCfg[id];
  if (!cfg || !cfg.warm_on_select) { _setModelWarm(null); return; }
  _setModelWarm('loading', `loading ${cfg.display || id}…`);
  fetch(`${BRAIN}/warm`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: id, display: cfg.display || id }),
  }).then(r => r.json()).then(d => {
    if (d.status === 'ready') _setModelWarm('ready', `${cfg.display || id} ready`);
    // 'warming' → wait for the model_warm SSE event to flip the pill.
  }).catch(() => {});
}

// ── Models ───────────────────────────────────────────────────────────
async function loadModels() {
  try {
    const list = await fetchJson(`${BRAIN}/models`);
    // Brain returns [[id, cfg], ...]
    state.models = list.map(([id, cfg]) => ({ id, cfg, display: cfg.display || id }));
    state.modelCfg = Object.fromEntries(list);

    // Restore preferred model + GK-toggle state from layout.
    let L = {};
    try { L = await fetchJson('/layout') || {}; } catch {}
    let saved = L.lastModel || null;
    // Prefer gkActive; migrate old ccActive layout key → off (different product).
    // Default ON: talking to Grok must not llama-swap. Explicit false in layout stays off.
    state.gkActive = L.gkActive !== false;
    // Migration: if Grok was saved as the dropdown pick, treat as "GK on".
    if (saved === GK_MODEL) { state.gkActive = true; saved = L.lastLocalModel || null; }
    // Also migrate legacy claude-code saved pick → local model (CC button is gone).
    if (saved === 'claude-code') { saved = L.lastLocalModel || null; }
    // OAuth agents (GK toggle + legacy claude-code) are not dropdown locals.
    const localModels = state.models.filter(m => isDropdownModel(m.id));
    state.currentModel = (saved && state.modelCfg[saved] && isDropdownModel(saved))
      ? saved
      : (localModels[0]?.id || null);

    applyModelAccent(activeModel());
    renderModelSelect();
    renderModelList();
    await loadPersonas(L);
    await loadRoster();
  } catch (e) {
    modelSelect.innerHTML = `<option>Brain unavailable</option>`;
    brainDot.classList.add('down');
  }
}

function inviteableModels() {
  return (state.models || []).filter(m =>
    m.id && m.id !== 'claude-code' && !String(m.id).startsWith('z-'));
}

async function loadRoster() {
  try {
    const cid = state.conversationId || 'default';
    const conv = await fetchJson(`${BRAIN}/conversations/${encodeURIComponent(cid)}`);
    state.participants = conv.participants || [];
    state.addressed = conv.addressed || [];
    if (conv.persona) {
      state.persona = conv.persona;
      renderPersonaSelect();
    }
    if (!state.participants.length) {
      // Prefer Grok (or last GK session) — never seed a local GPU model just
      // because it's ARGUS_DEFAULT. That evicts whatever is on the card.
      // Default empty rooms to Grok (cloud) so we never llama-swap-evict on load.
      state.participants = [GK_MODEL];
      state.addressed = [GK_MODEL];
    }
    if (!state.addressed.length && state.participants.length) {
      const last = state.participants.includes(GK_MODEL) ? GK_MODEL : state.participants[0];
      state.addressed = [last];
    }
    // If the thread was auto-addressed to a local model but GK is on, stay on Grok.
    // Old default-thread infer stuffed every historical model into the bar.
    if (state.participants.length > 6) {
      const ok = new Set(inviteableModels().map(m => m.id));
      const keep = [];
      for (const id of [...state.participants].reverse()) {
        if (ok.has(id) && !keep.includes(id)) keep.unshift(id);
        if (keep.length >= 4) break;
      }
      if (!keep.includes(GK_MODEL)) keep.unshift(GK_MODEL);
      state.participants = keep;
    }
    // GK / dropdown own send routing. Roster is history color only — do not
    // rewrite addressed here or turning GK off still sends to Grok.
  } catch {
    if (!state.participants.length) {
      state.participants = [GK_MODEL];
      state.addressed = [GK_MODEL];
    }
  }
  renderParticipants();
  updateGkToggle();
  applyModelAccent(activeModel());
}

async function patchRoster(body) {
  const cid = state.conversationId || 'default';
  try {
    const conv = await fetch(`${BRAIN}/conversations/${encodeURIComponent(cid)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(r => r.json());
    if (conv.participants) state.participants = conv.participants;
    if (conv.addressed) state.addressed = conv.addressed;
  } catch {}
  renderParticipants();
}

function renderParticipants() {
  const host = $('participant-chips');
  if (!host) return;
  host.innerHTML = '';
  for (const id of state.participants) {
    const on = state.addressed.includes(id);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'md-chip' + (on ? ' addressed' : '');
    btn.style.setProperty('--chip-accent', modelColor(id));
    btn.title = on ? `Talking to ${modelLabel(id)} — tap to mute` : `Tap to address ${modelLabel(id)}`;
    btn.innerHTML = `<span class="dot"></span><span class="lbl">${escHtml(modelLabel(id))}</span><span class="x" title="Remove">×</span>`;
    btn.querySelector('.x').addEventListener('click', ev => {
      ev.stopPropagation();
      const parts = state.participants.filter(p => p !== id);
      const addr = state.addressed.filter(p => p !== id);
      patchRoster({ participants: parts, addressed: addr });
    });
    btn.addEventListener('click', () => {
      const next = state.addressed.includes(id)
        ? state.addressed.filter(p => p !== id)
        : [...state.addressed, id];
      patchRoster({ addressed: next });
      applyModelAccent(next[0] || id);
    });
    host.appendChild(btn);
  }
}

function openInviteSheet() {
  let sheet = $('invite-sheet');
  if (sheet) { sheet.remove(); return; }
  sheet = document.createElement('div');
  sheet.id = 'invite-sheet';
  const have = new Set(state.participants);
  for (const m of inviteableModels()) {
    if (have.has(m.id)) continue;
    const b = document.createElement('button');
    b.type = 'button';
    b.innerHTML = `<span class="dot" style="width:8px;height:8px;border-radius:50%;background:${modelColor(m.id)}"></span>${escHtml(m.display)}`;
    b.addEventListener('click', () => {
      patchRoster({ participants: [...state.participants, m.id] });
      sheet.remove();
    });
    sheet.appendChild(b);
  }
  if (!sheet.childElementCount) {
    sheet.innerHTML = '<div style="padding:8px 10px;color:var(--text-dim)">Everyone inviteable is already in the room.</div>';
  }
  document.getElementById('top-bar').appendChild(sheet);
  const closer = ev => {
    if (!sheet.contains(ev.target) && ev.target.id !== 'participant-add') {
      sheet.remove();
      document.removeEventListener('click', closer, true);
    }
  };
  setTimeout(() => document.addEventListener('click', closer, true), 0);
}

// Reflect GK-toggle state on the button (active = routing to Grok) + dim the
// dropdown so it's clear the local model is on standby.
function updateGkToggle() {
  const btn = $('gk-toggle');
  if (btn) {
    btn.classList.toggle('active', state.gkActive);
    btn.setAttribute('aria-pressed', state.gkActive ? 'true' : 'false');
    btn.title = state.gkActive
      ? 'Routing to Grok (SuperGrok). Click to return to your dropdown model.'
      : 'Talk to Grok (SuperGrok) — keeps your dropdown model selected, does not unload the GPU.';
  }
  if (typeof modelSelect !== 'undefined' && modelSelect) {
    modelSelect.style.opacity = state.gkActive ? '0.5' : '';
    modelSelect.title = state.gkActive
      ? 'Standby — tap a model here to leave Grok and use the local GPU'
      : 'Local model (llama-swap)';
  }
}

function isDropdownModel(id) {
  return !!(id && id !== GK_MODEL && id !== 'claude-code' && !String(id).startsWith('z-'));
}

async function loadPersonas(layout) {
  try {
    const list = await fetchJson(`${BRAIN}/personas`);
    if (Array.isArray(list) && list.length) state.personas = list;
  } catch {}
  const ids = new Set((state.personas || []).map(p => p.id));
  let pick = (layout && layout.lastPersona) || state.persona || 'argus';
  if (!ids.has(pick)) pick = 'argus';
  state.persona = pick;
  renderPersonaSelect();
}

async function openPersonaForm() {
  await loadPersonas();
  const sel = $('pf-existing');
  if (sel) {
    sel.innerHTML = '<option value="">— new —</option>' +
      (state.personas || []).filter(p => p.id !== 'argus')
        .map(p => `<option value="${escHtml(p.id)}">${escHtml(p.display || p.id)}</option>`).join('');
  }
  $('pf-preview')?.setAttribute('hidden', '');
  openOverlay('persona-overlay');
}

function personaFormSpec() {
  const knobs = {};
  for (const k of ['brevity', 'blunt', 'dry', 'warmth']) {
    knobs[k] = Number($('pf-' + k)?.value || 50);
  }
  const rules = [...document.querySelectorAll('#persona-form input[name=pf-rule]:checked')]
    .map(x => x.value);
  return {
    id: ($('pf-id')?.value || '').trim(),
    name: ($('pf-name')?.value || '').trim(),
    knobs, rules,
    description: ($('pf-desc')?.value || '').trim(),
  };
}

async function fillPersonaForm(id) {
  if (!id) return;
  const spec = await fetchJson(`${BRAIN}/personas/${encodeURIComponent(id)}`);
  if ($('pf-name')) $('pf-name').value = spec.name || spec.display || '';
  if ($('pf-id')) $('pf-id').value = spec.id || '';
  if ($('pf-desc')) $('pf-desc').value = spec.description || '';
  for (const k of ['brevity', 'blunt', 'dry', 'warmth']) {
    const el = $('pf-' + k);
    if (el && spec.knobs && spec.knobs[k] != null) el.value = spec.knobs[k];
    const n = document.querySelector(`.pf-n[data-for="pf-${k}"]`);
    if (n && el) n.textContent = el.value;
  }
  const chosen = new Set(spec.rules || []);
  document.querySelectorAll('#persona-form input[name=pf-rule]').forEach(cb => {
    cb.checked = chosen.has(cb.value);
  });
}

function wirePersonaForm() {
  const form = $('persona-form');
  if (!form || form._wired) return;
  form._wired = true;
  form.querySelectorAll('input[type=range]').forEach(r => {
    const n = document.querySelector(`.pf-n[data-for="${r.id}"]`);
    r.addEventListener('input', () => { if (n) n.textContent = r.value; });
  });
  $('pf-existing')?.addEventListener('change', async e => {
    const id = e.target.value;
    if (id) { try { await fillPersonaForm(id); } catch (err) { showToast(err.message); } }
  });
  $('pf-name')?.addEventListener('input', () => {
    const id = $('pf-id');
    if (id && !id.dataset.locked) {
      id.value = ($('pf-name').value || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48);
    }
  });
  $('pf-id')?.addEventListener('input', () => { $('pf-id').dataset.locked = '1'; });
  $('pf-preview-btn')?.addEventListener('click', async () => {
    try {
      const r = await fetch(`${BRAIN}/personas?dry=1`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(personaFormSpec()),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.status);
      const pre = $('pf-preview');
      if (pre) { pre.hidden = false; pre.textContent = d.markdown || ''; }
    } catch (e) { showToast(e.message || 'preview failed'); }
  });
  form.addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const r = await fetch(`${BRAIN}/personas`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(personaFormSpec()),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.status);
      await loadPersonas();
      if (d.id) {
        state.persona = d.id;
        renderPersonaSelect();
        saveLayout();
      }
      showToast('Saved ' + (d.display || d.id));
      closeOverlay('persona-overlay');
    } catch (err) { showToast(err.message || 'save failed'); }
  });
}

function renderPersonaSelect() {
  if (!personaSelect) return;
  const list = state.personas || [];
  if (!list.length) {
    personaSelect.innerHTML = '<option value="argus">Argus</option>';
    return;
  }
  personaSelect.innerHTML = list
    .map(p => `<option value="${escHtml(p.id)}" ${p.id === state.persona ? 'selected' : ''}>${escHtml(p.display || p.id)}</option>`)
    .join('');
}

function renderModelSelect() {
  // Locals + CPU externals only. Grok is the GK button. Media z-* stay out.
  const locals = (state.models || []).filter(m => isDropdownModel(m.id));
  if (!locals.length) {
    modelSelect.innerHTML = '<option value="">No local models</option>';
    return;
  }
  if (!state.currentModel || !locals.some(m => m.id === state.currentModel)) {
    state.currentModel = locals[0].id;
  }
  const order = ['Chat', 'Coder', 'Small', 'CPU'];
  const buckets = {};
  for (const m of locals) {
    const g = (m.cfg && m.cfg.group) || 'Other';
    (buckets[g] || (buckets[g] = [])).push(m);
  }
  const labels = [...order.filter(g => buckets[g] && buckets[g].length),
                  ...Object.keys(buckets).filter(g => !order.includes(g) && buckets[g].length)];
  const parts = [];
  for (const g of labels) {
    parts.push(`<optgroup label="${escHtml(g)}">`);
    for (const m of buckets[g]) {
      parts.push(`<option value="${escHtml(m.id)}" ${m.id === state.currentModel ? 'selected' : ''}>${escHtml(m.display)}</option>`);
    }
    parts.push('</optgroup>');
  }
  modelSelect.innerHTML = parts.join('');
}

function renderModelList() {
  const el = $('model-list');
  if (!el) return;
  if (!state.models.length) { el.textContent = 'No models detected.'; return; }
  el.innerHTML = state.models.map(m => {
    const cfg = m.cfg;
    const sub = cfg.backend === 'anthropic' ? 'Claude API'
              : cfg.backend === 'claude-code' || cfg.backend === 'claude' ? 'Claude Code CLI'
              : cfg.backend === 'grok' ? 'Grok Build (SuperGrok OAuth)'
              : cfg.backend === 'ollama' ? `ollama · ${cfg.model || m.id}`
              : cfg.backend === 'vllm' ? `vLLM · ${cfg.model || m.id}`
              : String(cfg.backend || m.id);
    return `<div class="model-row">
      <span>${escHtml(m.display)}</span>
      <span class="model-meta">${escHtml(sub)}</span>
    </div>`;
  }).join('');
}

// Tiny transient toast (bottom-center) for actions with no other visible result.
function showToast(msg) {
  let t = document.getElementById('app-toast');
  if (!t) { t = document.createElement('div'); t.id = 'app-toast'; document.body.appendChild(t); }
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => t.classList.remove('show'), 1400);
}

function copyToClipboard(text) {
  if (!text) return false;
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(() => _legacyCopy(text));
    return true;
  }
  return _legacyCopy(text);
}

function _legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.contentEditable = 'true';
  ta.readOnly = false;
  ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;font-size:16px;background:transparent;';
  document.body.appendChild(ta);
  ta.focus();
  const isIOS = /iP(ad|hone|od)/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (isIOS) {
    const range = document.createRange();
    range.selectNodeContents(ta);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    ta.setSelectionRange(0, text.length);
  } else {
    ta.select();
  }
  let ok = false;
  try { ok = document.execCommand('copy'); } catch {}
  try { window.getSelection().removeAllRanges(); } catch {}
  ta.remove();
  return ok;
}

// Copy sheet — present the text pre-selected so iOS's NATIVE copy (the selection
// callout / long-press → Copy) does the work. The programmatic clipboard API is
// unavailable over plain http on iOS, so we lean on the OS instead of fighting it.
function showCopySheet(text) {
  if (!text) return;
  document.getElementById('copy-sheet')?.remove();
  const sheet = document.createElement('div');
  sheet.id = 'copy-sheet';
  sheet.innerHTML = `
    <div class="cs-backdrop"></div>
    <div class="cs-panel">
      <div class="cs-head">Copy <span class="cs-hint">— selected; tap <b>Copy</b> in the iOS menu, or the button</span></div>
      <textarea class="cs-text" readonly autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"></textarea>
      <div class="cs-actions">
        <button class="cs-copy">Copy</button>
        <button class="cs-close">Done</button>
      </div>
    </div>`;
  document.body.appendChild(sheet);
  const ta = sheet.querySelector('.cs-text');
  ta.value = text;
  const close = () => sheet.remove();
  sheet.querySelector('.cs-backdrop').onclick = close;
  sheet.querySelector('.cs-close').onclick = close;
  // A clean button tap is the most reliable user-gesture for the legacy copy path.
  sheet.querySelector('.cs-copy').onclick = () => {
    const ok = copyToClipboard(text);
    if (ok) { showToast('⧉ Copied'); close(); }
    else showToast('Select the text above → Copy');
  };
  // Auto-select so iOS surfaces its native Copy callout immediately.
  setTimeout(() => {
    ta.focus();
    try { ta.setSelectionRange(0, text.length); } catch {}
  }, 60);
}

// ── File staging (paperclip / paste / drop)
// Client keeps base64 dataUrls for the turn; server materialises to disk
// (argus.attachments) and routes: GK/CC = native vision, local = OCR + paths.
const stagedFiles = []; // [{filename, isImage, dataUrl?}]

function _fileIcon(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (['mp4','mov','mkv','webm'].includes(ext)) return '🎬';
  if (['mp3','wav','flac','ogg','m4a'].includes(ext)) return '🎵';
  if (ext === 'pdf') return '📕';
  if (['zip','tar','gz','7z','rar'].includes(ext)) return '📦';
  if (['py','js','ts','rs','go','java','c','cpp','h','sh','rb'].includes(ext)) return '💻';
  if (['md','txt','csv','json','yaml','yml','toml','log'].includes(ext)) return '📝';
  if (['xls','xlsx'].includes(ext)) return '📊';
  if (['doc','docx'].includes(ext)) return '📄';
  return '📎';
}

function _ensureStagingBar() {
  let bar = document.getElementById('input-staging');
  if (bar) return bar;
  const inputArea = document.getElementById('chat-input-area');
  bar = document.createElement('div');
  bar.id = 'input-staging';
  bar.style.cssText = 'display:none;flex-wrap:wrap;gap:6px;padding:6px 8px;border-bottom:1px solid #2a2a2a;';
  inputArea.insertBefore(bar, inputArea.firstChild);
  return bar;
}

function _renderStaging() {
  const bar = _ensureStagingBar();
  bar.innerHTML = '';
  if (!stagedFiles.length) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  stagedFiles.forEach((s, i) => {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'position:relative;display:inline-flex;';
    if (s.isImage && s.dataUrl) {
      const img = document.createElement('img');
      img.src = s.dataUrl;
      img.style.cssText = 'width:48px;height:48px;object-fit:cover;border-radius:6px;border:1px solid #444;';
      img.title = s.filename;
      wrap.appendChild(img);
    } else {
      const card = document.createElement('div');
      card.style.cssText = 'display:flex;align-items:center;gap:6px;height:48px;padding:0 10px;background:#2a2a2a;border:1px solid #444;border-radius:6px;max-width:240px;';
      card.title = s.filename;
      card.innerHTML = `<span style="font-size:1.4em;line-height:1;">${_fileIcon(s.filename)}</span><span style="font-size:0.82em;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${s.filename}</span>`;
      wrap.appendChild(card);
    }
    const x = document.createElement('span');
    x.textContent = '×';
    x.style.cssText = 'position:absolute;top:-6px;right:-6px;background:#333;color:#fff;border-radius:50%;width:18px;height:18px;text-align:center;font-size:13px;line-height:18px;cursor:pointer;border:1px solid #555;';
    x.onclick = () => {
      // Staging is client-side only until send(); nothing to DELETE on the server.
      stagedFiles.splice(i, 1);
      _renderStaging();
    };
    wrap.appendChild(x);
    bar.appendChild(wrap);
  });
  if (window.ArgusChat) ArgusChat.refreshSendBtn();
}

async function _stageBlob(blob, filename) {
  console.log('[Forge] _stageBlob called:', filename, 'type:', blob.type, 'size:', blob.size);
  const isImage = (blob.type || '').startsWith('image/') ||
    /\.(jpe?g|png|gif|webp|heic|heif|bmp|svg)$/i.test(filename);
  const entry = {filename, isImage};
  // Read as base64 data URL — used for thumbnails AND sent to backend
  // iOS Safari: force-materialize the file data via arrayBuffer() first,
  // then wrap in a fresh Blob. The original File object from the photo picker
  // can have stale/invalid data refs after the picker closes.
  try {
    let readBlob = blob;
    if (typeof blob.arrayBuffer === 'function') {
      const buf = await blob.arrayBuffer();
      console.log('[Forge] arrayBuffer materialized:', buf.byteLength, 'bytes');
      if (buf.byteLength === 0) {
        console.warn('[Forge] empty arrayBuffer for', filename);
        return;
      }
      readBlob = new Blob([buf], {type: blob.type || 'application/octet-stream'});
    }
    entry.dataUrl = await new Promise((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => {
        console.log('[Forge] FileReader success:', filename, 'dataUrl length:', fr.result?.length);
        res(fr.result);
      };
      fr.onerror = () => rej(fr.error || new Error('FileReader failed'));
      fr.readAsDataURL(readBlob);
    });
  } catch (err) {
    console.error('[Forge] _stageBlob failed:', filename, err);
    return;  // don't stage a broken file
  }
  stagedFiles.push(entry);
  _renderStaging();
  console.log('[Forge] file staged:', filename, 'total staged:', stagedFiles.length);
}

// Paperclip → native file picker via <label> wrapping (no programmatic .click() — iOS PWA safe)
const attachBtn   = document.getElementById('chat-attach');
const attachInput = document.getElementById('chat-attach-input');

/** Disable paperclip in Roundtable (no vision peer); keep enabled everywhere else —
 *  local models get OCR + paths; GK/claude-code get native images. */
function updateAttachAffordances() {
  const btn = attachBtn || document.getElementById('chat-attach');
  const input = attachInput || document.getElementById('chat-attach-input');
  if (!btn || !input) return;
  btn.classList.remove('attach-disabled');
  btn.title = 'Attach files or photos (or paste / drag). GK sees images natively; local models get OCR + file paths.';
  input.disabled = false;
}

if (attachBtn && attachInput) {
  console.log('[Forge] attach wired: label-wrapped input');

  // Process files from the input — shared by all event paths
  let _processing = false;
  async function _processPickedFiles() {
    if (_processing) return;
    const files = [...(attachInput.files || [])];
    if (!files.length) return;
    _processing = true;
    console.log('[Forge] processing picked files:', files.length, files.map(f => f.name + ' ' + f.type + ' ' + f.size + 'b'));
    for (const f of files) {
      await _stageBlob(f, f.name);
    }
    attachInput.value = '';  // allow re-selecting the same file later
    _processing = false;
  }

  // Primary: 'input' event (fires more reliably than 'change' on iOS Safari PWA)
  attachInput.addEventListener('input', (e) => {
    console.log('[Forge] input event fired, files:', e.target.files?.length);
    _processPickedFiles();
  });
  // Fallback 1: 'change' event (some browsers only fire change, not input, for file inputs)
  attachInput.addEventListener('change', (e) => {
    console.log('[Forge] change event fired, files:', e.target.files?.length);
    _processPickedFiles();
  });
  // Fallback 2: iOS Safari PWA often fires neither event after the photo picker closes.
  // When the picker closes, the page regains focus/visibility. Poll for files at that point.
  function _onReturnFromPicker() {
    setTimeout(() => {
      console.log('[Forge] focus/visibility returned, checking for files:', attachInput.files?.length);
      _processPickedFiles();
    }, 300);  // small delay for iOS to finalize the file reference
  }
  window.addEventListener('focus', _onReturnFromPicker);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') _onReturnFromPicker();
  });
}

// Returning to the app (e.g. after the phone screen slept): make sure the latest
// message is on screen so a reply isn't missed — scroll it into view + brief flash.
function _resurfaceLastMessage() {
  const msgs = messagesEl.querySelectorAll('.message');
  const last = msgs[msgs.length - 1];
  if (!last) return;
  last.scrollIntoView({ block: 'end' });
  last.classList.remove('resurfaced');
  void last.offsetWidth;            // restart the flash animation
  last.classList.add('resurfaced');
}

// Paste images directly into the input (most common for screenshots)
inputEl.addEventListener('paste', async (e) => {
  const items = e.clipboardData?.items || [];
  for (const item of items) {
    if (item.kind === 'file') {
      e.preventDefault();
      const blob = item.getAsFile();
      const fallbackExt = (item.type.split('/')[1] || 'bin').split(';')[0];
      const name = blob.name && blob.name !== 'image.png' ? blob.name : `paste-${Date.now()}.${fallbackExt}`;
      await _stageBlob(blob, name);
      return;
    }
  }
});

// Drag-drop anywhere on the input area — accepts any file type
['dragover','dragenter'].forEach(ev =>
  document.getElementById('chat-input-area').addEventListener(ev, e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  })
);
document.getElementById('chat-input-area').addEventListener('drop', async (e) => {
  e.preventDefault();
  for (const f of [...(e.dataTransfer?.files || [])]) {
    await _stageBlob(f, f.name);
  }
});

// Roundtable is a normal room now — no special send path. The To: bar stays hidden.
function updateRtBar() {
  const bar = $('rt-bar');
  if (bar) bar.classList.add('hidden');
  updateAttachAffordances();
}
async function openRoundtable() {
  await selectConversation(RT_CID);
}

// ── Command tags (canvas side-effects only; chat.js paints the bubble) ──
function processCommandTags(text, bubble) {
  const presetSave  = text.match(/\[\[PRESET_SAVE:([^\]]+)\]\]/);
  const presetLoad  = text.match(/\[\[PRESET_LOAD:([^\]]+)\]\]/);
  const presetList  = text.match(/\[\[PRESET_LIST\]\]/);
  const widgetHtml  = text.match(/\[\[WIDGET_HTML:([\s\S]+?)\]\]/);
  const widgetPrompt = text.match(/\[\[WIDGET_PROMPT:([^\]]+)\]\]/);

  if (presetSave) savePreset(presetSave[1].trim());
  if (presetLoad) loadPreset(presetLoad[1].trim());
  if (presetList) listPresetsToChat();
  if (widgetHtml) {
    const prompt = widgetPrompt ? widgetPrompt[1] : 'Generated widget';
    switchView('canvas');
    createPanel('generated', { html: widgetHtml[1], prompt });
  }
}

// ── Conversations ────────────────────────────────────────────────────
async function loadConversations() {
  try {
    state.conversations = await fetchJson(`${BRAIN}/conversations`);
  } catch {
    state.conversations = [];
  }
  renderConversations();
}

function renderConversations() {
  const list = $('conv-list');
  list.innerHTML = '';
  if (!state.conversations.length) {
    list.innerHTML = '<div class="briefing-err" style="padding:12px 0">No conversations yet.</div>';
    return;
  }
  state.conversations.forEach(c => {
    const id = c.id;
    const title = c.title || `Conversation ${id}`;
    const date = c.updated_at ? new Date(c.updated_at).toLocaleDateString('en-US',
      { month: 'short', day: 'numeric' }) : '';
    const item = document.createElement('div');
    item.className = `conv-item${id === state.conversationId ? ' active' : ''}`;
    item.innerHTML = `
      <div class="conv-item-info">
        <div class="conv-item-title">${escHtml(title)}</div>
        <div class="conv-item-meta">${escHtml(date)}</div>
      </div>
      <button class="conv-item-delete" title="Delete">×</button>`;
    item.addEventListener('click', e => {
      if (e.target.closest('.conv-item-delete')) return;
      selectConversation(id);
    });
    item.querySelector('.conv-item-delete').addEventListener('click', async e => {
      e.stopPropagation();
      if (!confirm(`Delete "${title}"?`)) return;
      await fetch(`${BRAIN}/conversations/${id}`, { method: 'DELETE' });
      if (state.conversationId === id) state.conversationId = null;
      await loadConversations();
      await loadHistory();
    });
    list.appendChild(item);
  });
}

async function selectConversation(id) {
  if (window.ArgusChat) ArgusChat.leaveTurn();
  state.conversationId = id;
  renderConversations();
  updateRtBar();
  await loadRoster();
  await loadHistory();
  closeOverlay('conv-drawer');
  switchView('chat');
}

async function newConversation() {
  try {
    const seed = state.addressed[0] || state.currentModel;
    const c = await fetchJson(`${BRAIN}/conversations`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        title: 'New conversation',
        participants: seed ? [seed] : [],
        addressed: seed ? [seed] : [],
      }),
    });
    state.conversationId = c.id || c.conversation_id || null;
  } catch {
    state.conversationId = null;  // fall back to default thread
  }
  await loadConversations();
  await loadRoster();
  updateRtBar();
  await loadHistory();
  closeOverlay('conv-drawer');
  switchView('chat');
  inputEl.focus();
}

// ── Briefing view ────────────────────────────────────────────────────
async function loadBriefing() {
  const body = $('briefing-body');
  body.innerHTML = '<div class="briefing-loading">Loading…</div>';

  const [haRes, sysRes, newsRes, booksRes] = await Promise.allSettled([
    fetchJson('/ha/states'),
    fetchJson(`${BRAIN}/status`),
    fetchJson(`${BRAIN}/briefing/news`),
    fetchJson(`${BRAIN}/services/readarr/upcoming`),
  ]);

  let html = '';

  // Home
  html += '<div class="briefing-section"><div class="briefing-section-title">Home</div>';
  if (haRes.status === 'fulfilled' && Array.isArray(haRes.value)) {
    const WATCH = [
      'switch.workshop_light', 'switch.porch_lights',
      'light.p1s_01p00a3c2300626_chamber_light',
      'sensor.p1s_01p00a3c2300626_print_status',
      'sensor.p1s_01p00a3c2300626_current_stage',
    ];
    const found = haRes.value.filter(s => WATCH.includes(s.entity_id));
    if (found.length) {
      html += '<div class="briefing-card">';
      for (const s of found) {
        const label = s.attributes?.friendly_name || s.entity_id.split('.')[1].replace(/_/g, ' ');
        const on = s.state === 'on' || (s.state && !['off', 'unavailable', 'unknown'].includes(s.state));
        const cls = on ? 'briefing-on' : 'briefing-off';
        html += `<div class="briefing-row"><span class="briefing-row-label">${escHtml(label)}</span><span class="briefing-row-val ${cls}">${escHtml(s.state)}</span></div>`;
      }
      html += '</div>';
    } else {
      html += '<div class="briefing-err">No watched entities found.</div>';
    }
  } else {
    html += '<div class="briefing-err">HA unavailable.</div>';
  }
  html += '</div>';

  // System
  html += '<div class="briefing-section"><div class="briefing-section-title">System</div>';
  if (sysRes.status === 'fulfilled') {
    const s = sysRes.value;
    const gpu = s.gpu || {};
    html += '<div class="briefing-card">';
    if (gpu.name) {
      html += `<div class="briefing-row"><span class="briefing-row-label">GPU</span><span class="briefing-row-val">${escHtml(`${gpu.vram_used_mib}/${gpu.vram_total_mib} MiB · ${gpu.temp_c}°C · ${gpu.util_pct}%`)}</span></div>`;
    }
    if (Array.isArray(s.models) && s.models.length) {
      html += `<div class="briefing-row"><span class="briefing-row-label">Models</span><span class="briefing-row-val">${escHtml(s.models.join(', '))}</span></div>`;
    }
    html += `<div class="briefing-row"><span class="briefing-row-label">Messages</span><span class="briefing-row-val">${escHtml(String(s.message_count ?? '—'))}</span></div>`;
    html += '</div>';
  } else {
    html += '<div class="briefing-err">System status unavailable.</div>';
  }
  html += '</div>';

  // News
  html += '<div class="briefing-section"><div class="briefing-section-title">News</div>';
  if (newsRes.status === 'fulfilled' && newsRes.value?.results?.length) {
    const { topics, results } = newsRes.value;
    if (topics?.length) {
      html += `<div class="briefing-card-sub" style="margin-bottom:8px">Topics: ${escHtml(topics.slice(0, 5).join(', '))}</div>`;
    }
    for (const item of results) {
      html += `<div class="briefing-card">
        <div class="briefing-card-title"><a href="${escHtml(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer">${escHtml(item.title || '')}</a></div>
        <div class="briefing-card-sub">${escHtml(item.description || '')}${item.age ? ' · ' + escHtml(item.age) : ''}</div>
      </div>`;
    }
  } else {
    html += `<div class="briefing-err">${escHtml(newsRes.value?.error || 'No news available.')}</div>`;
  }
  html += '</div>';

  // Books
  html += '<div class="briefing-section"><div class="briefing-section-title">Upcoming Books</div>';
  if (booksRes.status === 'fulfilled' && Array.isArray(booksRes.value) && booksRes.value.length) {
    const books = booksRes.value.filter(b => b.daysUntil >= -7);
    if (books.length) {
      for (const book of books) {
        const status = book.hasFile ? '✓' : book.grabbed ? '↓' : book.daysUntil <= 0 ? '●' : '…';
        const label = book.series ? `${book.title} (${book.series})` : book.title;
        const days = book.daysUntil > 0 ? `${book.daysUntil}d away`
                   : book.daysUntil === 0 ? 'Today'
                   : `${Math.abs(book.daysUntil)}d ago`;
        html += `<div class="briefing-card">
          <div class="briefing-card-title">${escHtml(status)} ${escHtml(label || '')}</div>
          <div class="briefing-card-sub">${escHtml(book.author || '')}${book.author ? ' · ' : ''}${escHtml(book.releaseDate || '')} · ${escHtml(days)}</div>
        </div>`;
      }
    } else {
      html += '<div class="briefing-err">No upcoming books.</div>';
    }
  } else {
    html += '<div class="briefing-err">Bookshelf unavailable.</div>';
  }
  html += '</div>';

  body.innerHTML = html;
}

// ── GPU view ─────────────────────────────────────────────────────────
let gpuTimer = null;

async function loadGpu() {
  try {
    const s = await fetchJson(`${BRAIN}/status`);
    const g = s.gpu || {};
    $('gpu-temp').textContent = g.temp_c != null ? `${g.temp_c}°C` : '—';
    $('gpu-util').textContent = g.util_pct != null ? `${g.util_pct}%` : '—';
    const pct = g.vram_total_mib ? Math.round(g.vram_used_mib / g.vram_total_mib * 100) : 0;
    $('gpu-vram-bar').style.width = pct + '%';
    $('gpu-vram-text').textContent = `${g.vram_used_mib ?? '—'} / ${g.vram_total_mib ?? '—'} MiB (${pct}%)`;
    const pwr = g.power_w, pwrMax = g.power_limit_w;
    const pwrPct = pwr && pwrMax ? Math.round(pwr / pwrMax * 100) : 0;
    $('gpu-power-bar').style.width = pwrPct + '%';
    $('gpu-power-text').textContent = pwr != null
      ? `${pwr.toFixed(0)} / ${pwrMax?.toFixed(0) ?? '—'} W`
      : '—';
    const vramBar = $('gpu-vram-bar');
    vramBar.classList.toggle('hot',  pct >= 90);
    vramBar.classList.toggle('warm', pct >= 75 && pct < 90);
  } catch {
    $('gpu-temp').textContent = 'err';
  }
}

function startGpuPolling() {
  if (gpuTimer) return;
  loadGpu();
  gpuTimer = setInterval(loadGpu, 5000);
}
function stopGpuPolling() {
  if (gpuTimer) { clearInterval(gpuTimer); gpuTimer = null; }
}

// ── Network view ─────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════
// CANVAS ENGINE — ported from claude-desktop app.js
// ═══════════════════════════════════════════════════════════════════════

const panels  = new Map();  // panelId → PanelState
const widgets = new Map();  // panelId → WidgetBase instance
let   prevCanvasW = 0;
let   prevCanvasH = 0;

function calcAnchorEdge(panel) {
  const cw = canvasBody.offsetWidth;
  const ch = canvasBody.offsetHeight;
  const cx = panel.x + panel.w / 2;
  const cy = panel.y + panel.h / 2;
  const h = cx <= cw / 2 ? 'l' : 'r';
  const v = cy <= ch / 2 ? 't' : 'b';
  return v + h;
}

function onCanvasResize() {
  const cw = canvasBody.offsetWidth;
  const ch = canvasBody.offsetHeight;
  if (prevCanvasW === 0) { prevCanvasW = cw; prevCanvasH = ch; return; }
  panels.forEach(p => {
    if (p.minimized) return;
    if (p.anchorEdge[1] === 'r') p.x = cw - (prevCanvasW - p.x);
    if (p.anchorEdge[0] === 'b') p.y = ch - (prevCanvasH - p.y);
    p.x = clamp(p.x, 0, Math.max(0, cw - p.w));
    p.y = clamp(p.y, 0, Math.max(0, ch - p.h));
    applyPanelPos(p);
  });
  prevCanvasW = cw;
  prevCanvasH = ch;
}

function applyPanelPos(p) {
  const el = document.getElementById(p.id);
  if (!el) return;
  el.style.left   = p.x + 'px';
  el.style.top    = p.y + 'px';
  el.style.width  = p.w + 'px';
  el.style.height = (p.collapsed ? 32 : p.h) + 'px';
}

function createPanel(type, config = {}, saved = null) {
  const Cls = WIDGET_REGISTRY[type];
  if (!Cls) return null;

  const id = saved?.id || `panel-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
  const cw = canvasBody.offsetWidth  || 480;
  const ch = canvasBody.offsetHeight || 600;

  // Cascade new panels so consecutive ones don't stack exactly on top of each other.
  let defaultX = 40, defaultY = 40;
  if (!saved && panels.size > 0) {
    const existing = [...panels.values()].filter(p => !p.minimized);
    if (existing.length > 0) {
      const last = existing[existing.length - 1];
      defaultX = clamp(last.x + 30, 0, Math.max(0, cw - Cls.defaultW));
      defaultY = clamp(last.y + 30, 0, Math.max(0, ch - Cls.defaultH));
    }
  }

  const st = {
    id,
    type,
    title:      Cls.label,
    x:          saved?.x          ?? defaultX,
    y:          saved?.y          ?? defaultY,
    w:          saved?.w          ?? Cls.defaultW,
    h:          saved?.h          ?? Cls.defaultH,
    minimized:  saved?.minimized  ?? false,
    collapsed:  saved?.collapsed  ?? false,
    anchorEdge: saved?.anchorEdge ?? 'tl',
    config:     saved?.config     ?? config,
  };
  panels.set(id, st);

  if (st.minimized) { addTab(st); return st; }

  const el = buildPanelDOM(st);
  canvasBody.appendChild(el);
  applyPanelPos(st);

  const inst = new Cls(st, el.querySelector('.panel-body'));
  inst.init();
  widgets.set(id, inst);

  saveLayout();
  return st;
}

function buildPanelDOM(state) {
  const el = document.createElement('div');
  el.id = state.id;
  el.className = `panel${state.collapsed ? ' collapsed' : ''}`;
  el.innerHTML = `
    <div class="panel-header">
      <span class="panel-icon">${escHtml(WIDGET_REGISTRY[state.type]?.icon || '▪')}</span>
      <span class="panel-title">${escHtml(state.title)}</span>
      <button class="panel-btn" title="Minimize" data-act="min">—</button>
      <button class="panel-btn" title="Close"    data-act="close">×</button>
    </div>
    <div class="panel-body"></div>
    <div class="resize-handle"></div>`;

  const header = el.querySelector('.panel-header');
  header.addEventListener('mousedown', e => {
    if (e.target.closest('.panel-btn')) return;
    startDrag(state, e);
  });
  header.addEventListener('dblclick', e => {
    if (e.target.closest('.panel-btn')) return;
    state.collapsed = !state.collapsed;
    el.classList.toggle('collapsed', state.collapsed);
    applyPanelPos(state);
    saveLayout();
  });
  header.querySelector('[data-act="min"]').addEventListener('click', () => minimizePanel(state.id));
  header.querySelector('[data-act="close"]').addEventListener('click', () => closePanel(state.id));

  el.querySelector('.resize-handle').addEventListener('mousedown', e => {
    e.stopPropagation();
    startResize(state, e);
  });
  return el;
}

// ── Drag ────────────────────────────────────────────────────────────
let dragState = null;

function startDrag(panel, e) {
  e.preventDefault();
  dragState = {
    panelId: panel.id,
    startX:  e.clientX,
    startY:  e.clientY,
    origX:   panel.x,
    origY:   panel.y,
  };
  const el = document.getElementById(panel.id);
  if (el) el.classList.add('dragging');
  // Disable iframe mouse capture during drag
  canvasBody.querySelectorAll('iframe').forEach(f => f.style.pointerEvents = 'none');
  document.addEventListener('mousemove', onDragMove);
  document.addEventListener('mouseup',   onDragEnd);
}

function onDragMove(e) {
  if (!dragState) return;
  const p = panels.get(dragState.panelId);
  if (!p) return;
  const cw = canvasBody.offsetWidth;
  const ch = canvasBody.offsetHeight;
  p.x = clamp(dragState.origX + e.clientX - dragState.startX, 0, Math.max(0, cw - p.w));
  p.y = clamp(dragState.origY + e.clientY - dragState.startY, 0, Math.max(0, ch - p.h));
  applyPanelPos(p);

  const tb = canvasTabbar.getBoundingClientRect();
  canvasTabbar.classList.toggle('drop-target', e.clientY >= tb.top);
}

function onDragEnd(e) {
  if (!dragState) return;
  const p = panels.get(dragState.panelId);
  document.removeEventListener('mousemove', onDragMove);
  document.removeEventListener('mouseup',   onDragEnd);
  canvasBody.querySelectorAll('iframe').forEach(f => f.style.pointerEvents = '');
  const el = document.getElementById(dragState.panelId);
  if (el) el.classList.remove('dragging');
  canvasTabbar.classList.remove('drop-target');

  if (p) {
    const tb = canvasTabbar.getBoundingClientRect();
    if (e.clientY >= tb.top) {
      minimizePanel(p.id);
    } else {
      p.anchorEdge = calcAnchorEdge(p);
      saveLayout();
    }
  }
  dragState = null;
}

// ── Resize ──────────────────────────────────────────────────────────
let resizeState = null;

function startResize(panel, e) {
  e.preventDefault();
  resizeState = {
    panelId: panel.id,
    startX:  e.clientX,
    startY:  e.clientY,
    origW:   panel.w,
    origH:   panel.h,
  };
  canvasBody.querySelectorAll('iframe').forEach(f => f.style.pointerEvents = 'none');
  document.addEventListener('mousemove', onResizeMove);
  document.addEventListener('mouseup',   onResizeEnd);
}

function onResizeMove(e) {
  if (!resizeState) return;
  const p = panels.get(resizeState.panelId);
  if (!p) return;
  const cw = canvasBody.offsetWidth;
  const ch = canvasBody.offsetHeight;
  p.w = clamp(resizeState.origW + e.clientX - resizeState.startX, 160, cw - p.x);
  p.h = clamp(resizeState.origH + e.clientY - resizeState.startY, 80,  ch - p.y);
  applyPanelPos(p);
}

function onResizeEnd() {
  if (!resizeState) return;
  document.removeEventListener('mousemove', onResizeMove);
  document.removeEventListener('mouseup',   onResizeEnd);
  canvasBody.querySelectorAll('iframe').forEach(f => f.style.pointerEvents = '');
  const p = panels.get(resizeState.panelId);
  if (p) { p.anchorEdge = calcAnchorEdge(p); saveLayout(); }
  resizeState = null;
}

// ── Tab bar (minimized panels) ──────────────────────────────────────
function minimizePanel(id) {
  const p = panels.get(id);
  if (!p) return;
  p.minimized = true;
  const el = document.getElementById(id);
  if (el) { widgets.get(id)?.destroy(); widgets.delete(id); el.remove(); }
  addTab(p);
  saveLayout();
}

function addTab(panel) {
  const tab = document.createElement('div');
  tab.className   = 'canvas-tab';
  tab.dataset.pid = panel.id;
  tab.innerHTML   = `
    <span>${escHtml(WIDGET_REGISTRY[panel.type]?.icon || '')} ${escHtml(panel.title)}</span>
    <span class="tab-close">×</span>`;
  tab.addEventListener('click', e => {
    if (e.target.closest('.tab-close')) { closePanel(panel.id); return; }
    restorePanel(panel.id);
  });
  canvasTabbar.insertBefore(tab, addWidgetBtn);
}

function restorePanel(id) {
  const p = panels.get(id);
  if (!p) return;
  p.minimized = false;
  const tab = canvasTabbar.querySelector(`[data-pid="${id}"]`);
  if (tab) tab.remove();
  const el = buildPanelDOM(p);
  canvasBody.appendChild(el);
  applyPanelPos(p);
  const Cls = WIDGET_REGISTRY[p.type];
  if (Cls) {
    const inst = new Cls(p, el.querySelector('.panel-body'));
    inst.init();
    widgets.set(id, inst);
  }
  saveLayout();
}

function closePanel(id) {
  const p = panels.get(id);
  if (!p) return;
  widgets.get(id)?.destroy();
  widgets.delete(id);
  panels.delete(id);
  document.getElementById(id)?.remove();
  canvasTabbar.querySelector(`[data-pid="${id}"]`)?.remove();
  saveLayout();
}

// ── Layout persistence ──────────────────────────────────────────────
let saveTimer = null;
async function saveLayout() {
  // Debounce to avoid a flood of POSTs during drags/resizes.
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const layout = {
      panels:    [...panels.values()],
      lastModel: state.currentModel,   // the dropdown (local) model
      lastLocalModel: state.currentModel,
      lastPersona: state.persona || 'argus',
      gkActive:  state.gkActive,       // GK toggle overlay state (SuperGrok)
      timestamp: new Date().toISOString(),
    };
    try {
      await fetch('/layout', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(layout),
      });
    } catch {}
  }, 150);
}

async function loadLayout() {
  try {
    const layout = await fetchJson('/layout');
    prevCanvasW = canvasBody.offsetWidth;
    prevCanvasH = canvasBody.offsetHeight;
    const saved = layout.panels || [];
    if (saved.length === 0) {
      spawnDefaultDashboard();
    } else {
      for (const st of saved) createPanel(st.type, st.config, st);
    }
  } catch {}
}

function spawnDefaultDashboard() {
  const cw = canvasBody.offsetWidth  || 800;
  const ch = canvasBody.offsetHeight || 600;
  createPanel('system',   {}, { x: 20,      y: 40,  w: 300, h: 220, minimized: false, collapsed: false, anchorEdge: 'tl' });
  createPanel('ha',       {}, { x: cw - 350, y: 40,  w: 320, h: 280, minimized: false, collapsed: false, anchorEdge: 'tr' });
  createPanel('torrents', {}, { x: 20,      y: 290, w: Math.max(300, cw - 80), h: 280, minimized: false, collapsed: false, anchorEdge: 'bl' });
}

// ── Presets (named layouts) ─────────────────────────────────────────
async function savePreset(name) {
  const payload = {
    panels:    [...panels.values()],
    timestamp: new Date().toISOString(),
  };
  await fetch(`/presets/${encodeURIComponent(name)}`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(payload),
  });
  appendMessage('assistant', `[Layout saved as "${name}"]`);
}

async function loadPreset(name) {
  let layout;
  try {
    layout = await fetchJson(`/presets/${encodeURIComponent(name)}`);
  } catch {
    appendMessage('assistant', `[Preset "${name}" not found]`);
    return;
  }
  [...panels.keys()].forEach(closePanel);
  for (const st of (layout.panels || [])) {
    createPanel(st.type, st.config, st);
  }
  appendMessage('assistant', `[Layout "${name}" loaded]`);
  switchView('canvas');
}

async function listPresetsToChat() {
  const presets = await fetchJson('/presets');
  if (!presets.length) { appendMessage('assistant', '[No saved presets]'); return; }
  appendMessage('assistant', `Presets: ${presets.map(p => p.name).join(', ')}`);
}

// ── Overlays ────────────────────────────────────────────────────────
// Sidebar-opened panels: clicking the nav item toggles them (menu-select), and the nav
// button reflects an .active state while open — same mechanism as the view tabs.
const SIDEBAR_PANELS = { 'tools-overlay': 'nav-tools', 'conv-drawer': 'nav-convs' };
function syncSidebarNav() {
  for (const [ov, btn] of Object.entries(SIDEBAR_PANELS)) {
    const open = $(ov) && !$(ov).classList.contains('hidden');
    $(btn)?.classList.toggle('active', !!open);
  }
}
function openOverlay(id)  { $(id)?.classList.remove('hidden'); syncSidebarNav(); }
function closeOverlay(id) { $(id)?.classList.add('hidden');    syncSidebarNav(); }
function toggleOverlay(id) {
  const el = $(id); if (!el) return;
  el.classList.contains('hidden') ? openOverlay(id) : closeOverlay(id);
}

async function openWidgetPicker() {
  const list = $('widget-type-list');
  list.innerHTML = '';
  for (const [type, Cls] of Object.entries(WIDGET_REGISTRY)) {
    if (type === 'generated') continue; // generated is created by the model, not picked
    const item = document.createElement('button');
    item.className = 'tools-card';
    item.innerHTML = `
      <span class="tools-card-icon">${escHtml(Cls.icon || '▪')}</span>
      <span class="tools-card-label">${escHtml(Cls.label)}</span>
      <span class="tools-card-desc">${escHtml(type)}</span>`;
    item.addEventListener('click', () => {
      closeOverlay('widget-picker-overlay');
      switchView('canvas');
      createPanel(type, {});
    });
    list.appendChild(item);
  }
  openOverlay('widget-picker-overlay');
}

async function openPresetsPicker(mode) {
  // mode = 'load' | 'save'
  const title = $('presets-title');
  const list  = $('presets-list');
  title.textContent = mode === 'save' ? 'Save layout' : 'Load layout';
  list.innerHTML = '';

  if (mode === 'save') {
    const card = document.createElement('button');
    card.className = 'tools-card';
    card.innerHTML = `
      <span class="tools-card-icon">+</span>
      <span class="tools-card-label">Save new preset</span>
      <span class="tools-card-desc">Snapshot the current canvas</span>`;
    card.addEventListener('click', async () => {
      const name = prompt('Name this layout:');
      if (!name) return;
      await savePreset(name);
      closeOverlay('presets-overlay');
    });
    list.appendChild(card);
  }

  const presets = await fetchJson('/presets').catch(() => []);
  if (!presets.length && mode === 'load') {
    const empty = document.createElement('div');
    empty.className = 'briefing-err';
    empty.textContent = 'No saved presets yet.';
    list.appendChild(empty);
  }
  for (const p of presets) {
    const item = document.createElement('button');
    item.className = 'tools-card';
    const when = p.timestamp ? new Date(p.timestamp).toLocaleString() : '';
    item.innerHTML = `
      <span class="tools-card-icon">◆</span>
      <span class="tools-card-label">${escHtml(p.name)}</span>
      <span class="tools-card-desc">${escHtml(when)}</span>`;
    item.addEventListener('click', async () => {
      if (mode === 'save') {
        if (!confirm(`Overwrite "${p.name}"?`)) return;
        await savePreset(p.name);
      } else {
        await loadPreset(p.name);
      }
      closeOverlay('presets-overlay');
    });
    list.appendChild(item);
  }
  openOverlay('presets-overlay');
}

async function openMemoryBrowser() {
  const body = $('memory-body');
  body.innerHTML = '<div class="briefing-loading">Loading…</div>';
  openOverlay('memory-overlay');
  try {
    const mems = await fetchJson(`${BRAIN}/memory`);
    if (!mems.length) {
      body.innerHTML = '<div class="briefing-err">No memories stored.</div>';
      return;
    }
    body.innerHTML = mems.map(m => `
      <div class="memory-card">
        <div class="key">${escHtml(m.key || '')}</div>
        <div class="title">${escHtml(m.title || m.key || '')}</div>
        <div class="content">${escHtml(m.content || '')}</div>
        ${(m.tags || []).length ? `<div class="tags">${m.tags.map(t => `<span class="tag">${escHtml(t)}</span>`).join('')}</div>` : ''}
      </div>`).join('');
  } catch (e) {
    body.innerHTML = `<div class="briefing-err">Memory fetch failed: ${escHtml(e.message)}</div>`;
  }
}

// ── Tools overlay actions ───────────────────────────────────────────
function handleToolsAction(action) {
  closeOverlay('tools-overlay');
  switch (action) {
    case 'new-chat':        newConversation(); break;
    case 'open-convs':      openOverlay('conv-drawer'); break;
    case 'clear-history':   clearHistory(); break;
    case 'save-layout':     openPresetsPicker('save'); break;
    case 'load-layout':     openPresetsPicker('load'); break;
    case 'add-widget':      openWidgetPicker(); break;
    case 'open-audiobooks': switchView('canvas'); createPanel('audiobook', {}); break;
    case 'memory-browser':  openMemoryBrowser(); break;
    case 'persona-form':    openPersonaForm(); break;
    case 'nec-prefix':
      switchView('chat');
      inputEl.value = '/nec ';
      autosizeInput();
      inputEl.focus();
      inputEl.setSelectionRange(5, 5);
      break;
  }
}

async function clearHistory() {
  if (!confirm('Clear all conversation history?')) return;
  await fetch(`${BRAIN}/history`, { method: 'DELETE' });
  await loadHistory();
}

// ── View switching ──────────────────────────────────────────────────
function switchView(id) {
  state.currentView = id;
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === `view-${id}`));
  document.querySelectorAll('.nav-tab[data-view]').forEach(t => t.classList.toggle('active', t.dataset.view === id));

  if (id === 'gpu')     startGpuPolling(); else stopGpuPolling();
  if (id === 'canvas')  onCanvasResize();
  if (id === 'briefing' && !$('briefing-body').textContent.trim()) loadBriefing();
}

// ── UI wiring ───────────────────────────────────────────────────────
function wireUI() {
  // === Critical-path chat wiring FIRST so a later throw never disables send ===
  // Robust send-enabling: decoupled from autosize, fired on multiple events, so
  // the button can never get stuck disabled.
  const refreshSend = () => {
    if (window.ArgusChat) ArgusChat.refreshSendBtn();
    else sendBtn.disabled = !inputEl.value.trim();
  };
  window._refreshSend = refreshSend;
  ['input', 'keyup', 'change', 'paste'].forEach(ev => inputEl.addEventListener(ev, refreshSend));
  inputEl.addEventListener('input', autosizeInput);
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!sendBtn.disabled) send();
    }
  });
  sendBtn.addEventListener('click', () => {
    if (sendBtn.classList.contains('is-stop')) stopSend();
    else send();
  });
  refreshSend();
  modelSelect.addEventListener('change', () => {
    if (!modelSelect.value) return;
    state.currentModel = modelSelect.value;
    state.gkActive = false;   // picking a local leaves Grok
    updateGkToggle();
    applyModelAccent(state.currentModel);
    if (isLocalGpuModel(state.currentModel)) maybeWarmModel(state.currentModel);
    saveLayout();
  });
  personaSelect?.addEventListener('change', () => {
    if (!personaSelect.value) return;
    state.persona = personaSelect.value;
    saveLayout();
    const cid = state.conversationId || 'default';
    fetch(`${BRAIN}/conversations/${encodeURIComponent(cid)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ persona: state.persona }),
    }).catch(() => {});
  });
  // GK toggle — SuperGrok overlay. Does not change the dropdown pick or llama-swap.
  $('gk-toggle')?.addEventListener('click', () => {
    state.gkActive = !state.gkActive;
    updateGkToggle();
    applyModelAccent(activeModel());
    saveLayout();
  });
  $('participant-add')?.addEventListener('click', ev => {
    ev.stopPropagation();
    openInviteSheet();
  });
  // Everything below is non-critical; wrap so any single failure can't kill the rest.
  try { _wireUI_rest(); } catch (e) {
    console.error('[wireUI rest failed]', e);
    if (window._dbgLog) window._dbgLog('wireUI rest failed: ' + e.message);
  }
}

function _wireUI_rest() {
  // Lazy-load older history when scrolled near top
  const scrollPill = document.getElementById('scroll-pill');
  messagesEl.addEventListener('scroll', () => {
    if (window.ArgusChat) ArgusChat.onScroll();
    else if (messagesEl.scrollTop < 200) loadMoreHistory();
  }, { passive: true });
  if (scrollPill) {
    scrollPill.addEventListener('click', () => {
      if (window.ArgusChat) ArgusChat.jumpToLatest();
      else {
        scrollPill.classList.remove('visible');
        messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: 'smooth' });
      }
    });
  }

  // Side nav
  document.querySelectorAll('.nav-tab[data-view]').forEach(t => {
    t.addEventListener('click', () => {
      switchView(t.dataset.view);
      // Selecting a view closes any open sidebar panel (menu-select consistency)
      closeOverlay('tools-overlay');
      closeOverlay('conv-drawer');
      // Close mobile drawer
      sideNav.classList.remove('open');
      const bd = document.getElementById('nav-backdrop');
      if (bd) bd.classList.remove('visible');
    });
  });
  sideToggle.addEventListener('click', () => {
    const isMobile = window.innerWidth <= 768;
    if (isMobile) {
      const open = sideNav.classList.toggle('open');
      sideNav.classList.remove('collapsed');
      const backdrop = document.getElementById('nav-backdrop');
      if (backdrop) backdrop.classList.toggle('visible', open);
    } else {
      sideNav.classList.toggle('collapsed');
    }
  });
  // Backdrop tap closes drawer
  const navBackdrop = document.getElementById('nav-backdrop');
  if (navBackdrop) {
    navBackdrop.addEventListener('click', () => {
      sideNav.classList.remove('open');
      navBackdrop.classList.remove('visible');
    });
  }

  // (model-select listener moved to wireUI critical path)

  // Sidebar tools & conversations buttons
  const closeMobileDrawer = () => {
    sideNav.classList.remove('open');
    document.getElementById('nav-backdrop')?.classList.remove('visible');
  };
  $('nav-tools').addEventListener('click', () => {
    closeMobileDrawer();
    closeOverlay('conv-drawer');          // only one sidebar panel open at a time
    toggleOverlay('tools-overlay');       // menu-select: re-tap closes
  });
  $('nav-convs').addEventListener('click', () => {
    closeMobileDrawer();
    closeOverlay('tools-overlay');
    toggleOverlay('conv-drawer');
  });

  // Tools overlay buttons
  document.querySelectorAll('#tools-overlay [data-action]').forEach(b => {
    b.addEventListener('click', () => handleToolsAction(b.dataset.action));
  });
  wirePersonaForm();

  // Overlay closers (X buttons + backdrops)
  document.querySelectorAll('[data-close]').forEach(b => {
    b.addEventListener('click', () => closeOverlay(b.dataset.close));
  });
  $('conv-drawer-backdrop').addEventListener('click', () => closeOverlay('conv-drawer'));
  $('conv-new').addEventListener('click', () => newConversation());
  $('conv-roundtable')?.addEventListener('click', () => openRoundtable());
  // Roundtable "To:" addressee bar — tap to pick who the next message goes to.
  $('rt-bar')?.addEventListener('click', e => {
    const btn = e.target.closest('.rt-to');
    if (!btn) return;
    state.rtTo = btn.dataset.to;
    updateRtBar();
    inputEl.focus();
  });

  // (chat input + send listeners moved to wireUI critical path)

  // Refresh buttons
  $('briefing-refresh').addEventListener('click', loadBriefing);
  $('gpu-refresh').addEventListener('click', loadGpu);
  // SABnzbd nav tab → open the SAB dashboard PWA (same host, port 8215) in a new tab.
  $('nav-sabnzb')?.addEventListener('click', () => {
    window.open(`${location.protocol}//${location.hostname}:8215`, '_blank');
    sideNav.classList.remove('open');
    const bd = document.getElementById('nav-backdrop');
    if (bd) bd.classList.remove('visible');
  });

  // Add widget
  addWidgetBtn.addEventListener('click', openWidgetPicker);

  // Esc closes any open overlay
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      ['tools-overlay', 'widget-picker-overlay', 'presets-overlay', 'memory-overlay', 'persona-overlay', 'conv-drawer']
        .forEach(closeOverlay);
    }
  });

  // Canvas resize → reflow panels
  window.addEventListener('resize', () => {
    if (state.currentView === 'canvas') onCanvasResize();
  });
}

function autosizeInput() {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(200, inputEl.scrollHeight) + 'px';
}

// ── Compat shim for widgets (they expect window.app.prefillChat) ─────
window.app = {
  prefillChat(text) {
    switchView('chat');
    inputEl.value = text;
    autosizeInput();
    sendBtn.disabled = !inputEl.value.trim();
    inputEl.focus();
  },
};

// ── Theme toggle ──────────────────────────────────────────────────────
const THEMES = ['default', 'ocean', 'ember', 'matrix', 'synthwave', 'solar', 'comfy', 'slate'];
const THEME_LABELS = { default: 'Default', ocean: 'Ocean', ember: 'Ember', matrix: 'Matrix', synthwave: 'Synthwave', solar: 'Solar', comfy: 'Comfy', slate: 'Slate' };
(function initTheme() {
  const saved = localStorage.getItem('cdn-theme') || 'default';
  if (saved !== 'default') document.documentElement.setAttribute('data-theme', saved);
  const btn = $('nav-theme');
  const label = $('nav-theme-label');
  if (btn) {
    if (label) label.textContent = THEME_LABELS[saved] || 'Default';
    btn.addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'default';
      const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
      if (next === 'default') document.documentElement.removeAttribute('data-theme');
      else document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('cdn-theme', next);
      if (label) label.textContent = THEME_LABELS[next];
      btn.title = `Theme: ${next}`;
    });
  }
})();

// ── Boot ─────────────────────────────────────────────────────────────
init().catch(err => {
  console.error('init failed', err);
});
