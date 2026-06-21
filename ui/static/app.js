// ── Claude Desktop Next — frontend ───────────────────────────────────
// Chat via SSE through /argus/events, widget canvas ported from the legacy
// desktop app, overlays + conversations drawer borrowed from forge.

const BRAIN = '/argus';

// ── State ────────────────────────────────────────────────────────────
const HISTORY_PAGE = 100;

const state = {
  models:         [],       // [{name, params, size, family, cfg}]
  modelCfg:       {},       // {model_id: cfg_from_brain}
  currentModel:   null,
  conversationId: null,
  conversations: [],
  currentView:    'chat',
  abortCtl:       null,     // AbortController for active chat
  pendingBubbleId:null,     // SSE id we're listening for
  pendingMsgEl:   null,     // current streaming message DOM node
  statusPill:     null,     // live status pill (phase + elapsed timer), killed on done
  pendingDbId:    null,     // DB id of the assistant message after save
  pendingUserId:  null,     // DB id of the user message that prompted it
  pendingCostMeta:null,     // " · $0.04 · 12s" from the CC done event, appended to bubble meta
  oldestMsgId:    null,     // id of earliest-loaded message, for lazy paging
  historyExhausted:false,   // true once we've fetched everything older
  historyLoading: false,    // in-flight older-page fetch
};

// ── DOM refs ─────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const messagesEl   = $('chat-messages');
const inputEl      = $('chat-input');
const sendBtn      = $('chat-send');
const modelSelect  = $('model-select');
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
  return String(text || '').replace(/\[\[[^\]]+\]\]/g, '').trim();
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
  const cost  = document.createElement('span'); cost.className = 'sp-cost';   // filled from the done event
  const caret = document.createElement('span'); caret.className = 'sp-caret'; caret.textContent = '▸';
  pill.append(dot, label, time, cost, caret);
  const panel = document.createElement('div'); panel.className = 'sp-activity';
  wrap.append(pill, panel);

  const start = Date.now();
  let lastActivity = start, gotToken = false, phase = '', hasActivity = false;
  const STALL = 20, CRASH = 90;  // seconds since the last signal

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

  // Tap to expand — only meaningful once activity has streamed (claude-code path).
  pill.addEventListener('click', () => {
    if (!hasActivity) return;
    wrap.classList.toggle('open');
    if (wrap.classList.contains('open')) panel.scrollTop = panel.scrollHeight;
  });

  wrap._activity = () => { lastActivity = Date.now(); gotToken = true; phase = ''; tick(); }; // token: streaming supersedes any tool label
  wrap._status   = (txt) => { phase = txt; lastActivity = Date.now(); tick(); };               // tool/loop labels
  wrap._event    = (ev) => {                                                                   // CC activity stream
    if (!ev || ev.kind === 'done') return;
    hasActivity = true; pill.classList.add('expandable');
    appendActivityEntry(panel, ev);
    if (wrap.classList.contains('open')) panel.scrollTop = panel.scrollHeight;
  };
  wrap._cost     = (txt) => { cost.textContent = txt; };                                        // "· $0.04 · 12s"
  wrap._collapse = () => { wrap.classList.remove('open'); };
  wrap._stop     = () => { clearInterval(timer); wrap.remove(); };
  return wrap;
}

// Render one activity entry into the expandable panel. CSS-light, append-only DOM.
function appendActivityEntry(panel, ev) {
  const row = document.createElement('div');
  if (ev.kind === 'thinking') {
    row.className = 'act-think';
    row.textContent = (ev.text || '').slice(0, 2000);
  } else if (ev.kind === 'tool_use') {
    row.className = 'act-tool';
    row.textContent = `🔧 ${ev.name || 'tool'}(${fmtToolInput(ev.input)})`;
  } else if (ev.kind === 'tool_result') {
    row.className = 'act-result';
    row.textContent = (ev.text || '').slice(0, 2000);
  } else {
    return;
  }
  panel.appendChild(row);
}

// Compact one-line summary of a tool's input args for the activity panel.
function fmtToolInput(input) {
  if (!input || typeof input !== 'object') return '';
  try {
    const s = JSON.stringify(input);
    return s.length > 80 ? s.slice(0, 79) + '…' : s;
  } catch { return ''; }
}

// "· $0.04 · 12s" from the done event's cost + duration_ms (either may be missing).
function fmtCostDuration(cost, durationMs) {
  const parts = [];
  if (typeof cost === 'number') parts.push('$' + (cost < 0.1 ? cost.toFixed(4) : cost.toFixed(2)));
  if (typeof durationMs === 'number') parts.push(Math.round(durationMs / 1000) + 's');
  return parts.length ? '· ' + parts.join(' · ') : '';
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

  let raf;
  let frame = 0;

  function draw() {
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
  canvas._stopThinking = () => { cancelAnimationFrame(raf); };
  return canvas;
}

// ── Boot ─────────────────────────────────────────────────────────────
async function init() {
  await loadModels();
  await loadConversations();
  await loadHistory();
  await loadLayout();
  connectEvents();
  wireUI();
  // Kick off view-specific polls lazily; they'll hydrate on tab switch too.
  loadBriefing();
}

// ── Models ───────────────────────────────────────────────────────────
async function loadModels() {
  try {
    const list = await fetchJson(`${BRAIN}/models`);
    // Brain returns [[id, cfg], ...]
    state.models = list.map(([id, cfg]) => ({ id, cfg, display: cfg.display || id }));
    state.modelCfg = Object.fromEntries(list);

    // Restore preferred model from layout if present
    let saved = null;
    try { saved = (await fetchJson('/layout')).lastModel || null; } catch {}
    state.currentModel = (saved && state.modelCfg[saved])
      ? saved
      : (state.models[0]?.id || null);

    renderModelSelect();
    renderModelList();
  } catch (e) {
    modelSelect.innerHTML = `<option>Brain unavailable</option>`;
    brainDot.classList.add('down');
  }
}

function renderModelSelect() {
  modelSelect.innerHTML = state.models
    .map(m => `<option value="${escHtml(m.id)}" ${m.id === state.currentModel ? 'selected' : ''}>${escHtml(m.display)}</option>`)
    .join('');
}

function renderModelList() {
  const el = $('model-list');
  if (!el) return;
  if (!state.models.length) { el.textContent = 'No models detected.'; return; }
  el.innerHTML = state.models.map(m => {
    const cfg = m.cfg;
    const sub = cfg.backend === 'anthropic' ? 'Claude API'
              : cfg.backend === 'claude-code' ? 'Claude Code CLI'
              : cfg.backend === 'ollama' ? `ollama · ${cfg.model || m.id}`
              : cfg.backend === 'vllm' ? `vLLM · ${cfg.model || m.id}`
              : String(cfg.backend || m.id);
    return `<div class="model-row">
      <span>${escHtml(m.display)}</span>
      <span class="model-meta">${escHtml(sub)}</span>
    </div>`;
  }).join('');
}

// ── History ──────────────────────────────────────────────────────────
function _historyQuery(extra = {}) {
  const p = new URLSearchParams();
  if (state.conversationId != null) p.set('conversation_id', state.conversationId);
  for (const [k, v] of Object.entries(extra)) {
    if (v != null) p.set(k, v);
  }
  const qs = p.toString();
  return qs ? `?${qs}` : '';
}

async function loadHistory() {
  state.oldestMsgId = null;
  state.historyExhausted = false;
  state.historyLoading = false;
  let msgs;
  try {
    msgs = await fetchJson(`${BRAIN}/history${_historyQuery({ limit: HISTORY_PAGE })}`);
  } catch {
    msgs = [];
  }
  renderHistory(msgs);
  if (msgs && msgs.length) state.oldestMsgId = msgs[0].id;
  if (!msgs || msgs.length < HISTORY_PAGE) state.historyExhausted = true;
}

async function loadMoreHistory() {
  if (state.historyLoading || state.historyExhausted || state.oldestMsgId == null) return;
  state.historyLoading = true;
  let msgs = [];
  try {
    msgs = await fetchJson(`${BRAIN}/history${_historyQuery({ limit: HISTORY_PAGE, before_id: state.oldestMsgId })}`);
  } catch {
    msgs = [];
  }
  if (!msgs || msgs.length === 0) {
    state.historyExhausted = true;
    state.historyLoading = false;
    return;
  }
  prependHistory(msgs);
  state.oldestMsgId = msgs[0].id;
  if (msgs.length < HISTORY_PAGE) state.historyExhausted = true;
  state.historyLoading = false;
}

function _buildMessageNodes(msgs) {
  const frag = document.createDocumentFragment();
  let lastDay = null;
  (msgs || []).forEach(msg => {
    const day = fmtDay(msg.timestamp);
    if (day !== lastDay) {
      lastDay = day;
      const lbl = document.createElement('div');
      lbl.className = 'timestamp-label';
      lbl.textContent = day;
      frag.appendChild(lbl);
    }
    const role = msg.role === 'user' ? 'user' : 'assistant';
    const text = role === 'assistant' ? stripCommandTags(msg.content) : (msg.content || '');
    const modelDisplay = state.modelCfg?.[msg.model]?.display || msg.model;
    const meta = role === 'assistant' && msg.model
      ? `${modelDisplay} · ${fmtTime(msg.timestamp)}`
      : fmtTime(msg.timestamp);
    const el = document.createElement('div');
    el.className = `message ${role}`;
    el.dataset.msgId = msg.id;
    el.textContent = text;
    if (meta) {
      const m = document.createElement('span');
      m.className = 'msg-meta';
      m.textContent = meta;
      el.appendChild(m);
    }
    if (role === 'assistant') {
      renderImageTags(el, msg.content);
      renderVideoTags(el, msg.content);
    }
    _attachDeleteButton(el, msg.id);
    // Wrap in swipe-to-delete row, then append the wrapper
    const row = _wrapSwipeDelete(el);
    frag.appendChild(row);
  });
  return frag;
}

// Hover-shown × that deletes the message (and any image files it generated)
// from the DB, then removes the element from the DOM.
function _attachDeleteButton(el, msgId) {
  if (!msgId) return;
  const x = document.createElement('span');
  x.className = 'msg-delete';
  x.textContent = '×';
  x.title = 'Delete message';
  x.addEventListener('click', async (ev) => {
    ev.stopPropagation();
    if (!confirm('Delete this message?')) return;
    let r;
    try {
      r = await fetch(`${BRAIN}/history/${encodeURIComponent(msgId)}`, {method: 'DELETE'});
    } catch (e) {
      alert(`Delete failed: ${e.message}`); return;
    }
    if (!r.ok) { alert(`Delete failed: HTTP ${r.status}`); return; }
    el.remove();
  });
  el.appendChild(x);
}

// Swipe-left-to-delete on touch devices.  Wraps a message element in a
// .swipe-row container with a red "Delete" zone behind it.
// Reads msgEl.dataset.msgId at swipe-time so late-bound IDs work.
function _wrapSwipeDelete(msgEl) {
  const row = document.createElement('div');
  row.className = 'swipe-row';
  const bg = document.createElement('div');
  bg.className = 'swipe-delete-bg';
  bg.textContent = 'Delete';
  row.appendChild(bg);
  // Transfer the message into the wrapper
  msgEl.parentNode?.insertBefore(row, msgEl);
  row.appendChild(msgEl);

  let startX = 0, startY = 0, dx = 0, tracking = false;
  const THRESHOLD = 0.30; // 30% of row width

  row.addEventListener('touchstart', e => {
    const t = e.touches[0];
    startX = t.clientX;
    startY = t.clientY;
    dx = 0;
    tracking = true;
    row.classList.add('swiping');
  }, { passive: true });

  row.addEventListener('touchmove', e => {
    if (!tracking) return;
    const t = e.touches[0];
    const deltaX = t.clientX - startX;
    const deltaY = t.clientY - startY;
    // If vertical scroll dominates, bail out
    if (Math.abs(deltaY) > Math.abs(deltaX) && Math.abs(dx) < 10) {
      tracking = false;
      row.classList.remove('swiping');
      msgEl.style.transform = '';
      return;
    }
    dx = Math.min(0, deltaX); // only allow left swipe
    if (dx < 0) e.preventDefault(); // prevent scroll while swiping
    msgEl.style.transform = `translateX(${dx}px)`;
  }, { passive: false });

  row.addEventListener('touchend', () => {
    if (!tracking) return;
    tracking = false;
    row.classList.remove('swiping');
    const pct = Math.abs(dx) / row.offsetWidth;
    const id = msgEl.dataset.msgId;
    if (pct >= THRESHOLD && id) {
      // Animate out then delete
      msgEl.style.transition = 'transform 0.2s ease';
      msgEl.style.transform = `translateX(-${row.offsetWidth}px)`;
      msgEl.addEventListener('transitionend', async () => {
        try {
          await fetch(`${BRAIN}/history/${encodeURIComponent(id)}`, { method: 'DELETE' });
        } catch {}
        row.remove();
      }, { once: true });
    } else {
      msgEl.style.transform = '';
    }
  }, { passive: true });

  row.addEventListener('touchcancel', () => {
    tracking = false;
    row.classList.remove('swiping');
    msgEl.style.transform = '';
  }, { passive: true });

  return row;
}

function renderHistory(msgs) {
  messagesEl.innerHTML = '';
  messagesEl.appendChild(_buildMessageNodes(msgs));
  scrollBottom();
}

function prependHistory(msgs) {
  const prevHeight = messagesEl.scrollHeight;
  const prevTop    = messagesEl.scrollTop;
  messagesEl.insertBefore(_buildMessageNodes(msgs), messagesEl.firstChild);
  // Preserve viewport position — keep the same message under the user's eye.
  messagesEl.scrollTop = prevTop + (messagesEl.scrollHeight - prevHeight);
}

function appendMessage(role, text, { streaming = false, meta = '' } = {}) {
  const el = document.createElement('div');
  el.className = `message ${role}${streaming ? ' streaming' : ''}`;
  el.textContent = text;
  if (meta) {
    const m = document.createElement('span');
    m.className = 'msg-meta';
    m.textContent = meta;
    el.appendChild(m);
  }
  // Wrap in swipe row (reads dataset.msgId at swipe time)
  const row = _wrapSwipeDelete(el);
  messagesEl.appendChild(row);
  scrollBottom();
  return el;
}

function scrollBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── File staging (paperclip/paste/drop) — images for Z-Edit + vision models;
// any file type for claude-code or models that read [File: /path] markers.
const stagedFiles = []; // [{filename, path, isImage, dataUrl?}]

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
    x.onclick = async () => {
      try { await fetch(`${BRAIN}/uploads/${encodeURIComponent(s.filename)}`, {method: 'DELETE'}); } catch {}
      stagedFiles.splice(i, 1);
      _renderStaging();
    };
    wrap.appendChild(x);
    bar.appendChild(wrap);
  });
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

// ── Send ─────────────────────────────────────────────────────────────
async function send() {
  const text = inputEl.value.trim();
  if ((!text && !stagedFiles.length) || !state.currentModel) return;
  inputEl.value = '';
  autosizeInput();

  // @doc: prefix → RAG query, streams text chunks
  if (text.startsWith('@doc:')) {
    return sendDocQuery(text);
  }

  // Build attachments array with base64 data for staged files
  const attachments = stagedFiles
    .filter(s => s.dataUrl)
    .map(s => ({ filename: s.filename, isImage: s.isImage, dataUrl: s.dataUrl }));
  // Snapshot for the user bubble; then clear staging.
  const sentFiles = stagedFiles.slice();
  stagedFiles.length = 0;
  _renderStaging();

  // Show user bubble: text + thumbnails for images / icon cards for other files.
  const userEl = appendMessage('user', text || (sentFiles.length === 1 ? sentFiles[0].filename : `(${sentFiles.length} files)`));
  sentFiles.forEach(s => {
    if (s.isImage && s.dataUrl) {
      const img = document.createElement('img');
      img.src = s.dataUrl;
      img.style.cssText = 'display:block;margin-top:6px;max-width:240px;max-height:240px;border-radius:6px;border:1px solid #444;';
      userEl.appendChild(img);
    } else {
      const card = document.createElement('div');
      card.style.cssText = 'display:inline-flex;align-items:center;gap:6px;margin-top:6px;padding:6px 10px;background:#2a2a2a;border:1px solid #444;border-radius:6px;font-size:0.85em;color:#ddd;max-width:240px;';
      card.innerHTML = `<span style="font-size:1.4em;">${_fileIcon(s.filename)}</span><span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${s.filename}</span>`;
      userEl.appendChild(card);
    }
  });
  const typingEl = appendMessage('assistant typing', '');
  const statusPill = createStatusPill();
  messagesEl.appendChild(statusPill);
  scrollBottom();

  state.abortCtl = new AbortController();
  sendBtn.classList.add('is-stop');
  try {
    const res = await fetch(`${BRAIN}/chat`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        model:    state.currentModel,
        message:  text,
        conversation_id: state.conversationId,
        attachments: attachments.length ? attachments : undefined,
      }),
      signal: state.abortCtl.signal,
    });
    if (!res.ok) {
      typingEl.textContent = `[Error ${res.status}]`;
      typingEl.classList.remove('typing');
      return;
    }
    const { id: bubbleId } = await res.json();
    console.log('[DBG] chat sent, bubbleId:', bubbleId);
    state.pendingBubbleId = bubbleId;
    // Live status pill (timer + stall/crash detection) until the turn completes.
    state.statusPill = statusPill;
    state.pendingMsgEl = typingEl;

    // Safety timeout: if we somehow miss the done event, release the UI.
    // Media models (z-image-edit, z-klein, z-video) can take up to 10 minutes.
    const _safetyMs = /^z-(image-edit|klein|video)/i.test(state.currentModel || '') ? 600_000 : 300_000;
    await new Promise(resolve => {
      const doneTimer = setTimeout(resolve, _safetyMs);
      const onDone = () => { clearTimeout(doneTimer); resolve(); };
      state.pendingResolve = onDone;
    });

    // Finalise the bubble
    if (state.pendingMsgEl) {
      state.pendingMsgEl.classList.remove('streaming');
      let raw = state.pendingMsgEl.textContent;

      // For media models (z-image-*, z-klein, z-video), the SSE connection can
      // drop during the long ComfyUI wait (background tab throttling, keepalive
      // timeout).  When that happens we lose both bubble_update and bubble_done,
      // so the textContent won't contain the [[IMAGE:…]] tag.  Fix: always fetch
      // the authoritative response from the DB for media models.
      const _isMedia = /^z-(image|klein|video)/i.test(state.currentModel || '');
      if (_isMedia && !/\[\[(IMAGE|VIDEO):/.test(raw)) {
        console.log('[z-edit-debug] raw missing media tag, fetching from DB…', {bubbleId, dbId: state.pendingDbId, rawLen: raw.length});
        try {
          // Try by db_id first (if bubble_done delivered it), then fall back to
          // fetching the latest assistant message in this conversation.
          const qs = state.conversationId != null
            ? `?conversation_id=${state.conversationId}&limit=5`
            : '?limit=5';
          const msgs = await fetchJson(`${BRAIN}/history${qs}`);
          // Find either by db_id or by matching bubble content prefix
          const saved = state.pendingDbId
            ? msgs?.find(m => m.id === state.pendingDbId)
            : msgs?.find(m => m.role === 'assistant' && /\[\[(IMAGE|VIDEO):/.test(m.content || ''));
          if (saved?.content && /\[\[(IMAGE|VIDEO):/.test(saved.content)) {
            console.log('[z-edit-debug] DB fallback found media tag', saved.content.slice(-80));
            raw = saved.content;
            if (!state.pendingDbId) state.pendingDbId = saved.id;
          } else {
            console.log('[z-edit-debug] DB fallback: no media tag found in history');
          }
        } catch (e) {
          console.error('[z-edit-debug] DB fallback fetch failed', e);
        }
      }

      state.pendingMsgEl.textContent = stripCommandTags(raw);
      addMeta(state.pendingMsgEl, `${state.currentModel} · ${fmtTime(new Date())}${state.pendingCostMeta || ''}`);
      processCommandTags(raw, state.pendingMsgEl);
      if (state.pendingDbId) {
        state.pendingMsgEl.dataset.msgId = state.pendingDbId;
        _attachDeleteButton(state.pendingMsgEl, state.pendingDbId);
      }
      // Also tag the matching user bubble (the previous user-role sibling) so it can be deleted too.
      if (state.pendingUserId) {
        let prev = state.pendingMsgEl.previousElementSibling;
        while (prev && !prev.classList.contains('user')) prev = prev.previousElementSibling;
        if (prev && !prev.dataset.msgId) {
          prev.dataset.msgId = state.pendingUserId;
          _attachDeleteButton(prev, state.pendingUserId);
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      appendMessage('assistant', `[Error: ${e.message}]`);
    }
  } finally {
    if (state.statusPill) { state.statusPill._stop(); state.statusPill = null; }
    state.abortCtl = null;
    state.pendingBubbleId = null;
    state.pendingMsgEl = null;
    state.pendingResolve = null;
    state.pendingDbId = null;
    state.pendingUserId = null;
    state.pendingCostMeta = null;
    sendBtn.classList.remove('is-stop');
  }
}

function addMeta(el, text) {
  const m = document.createElement('span');
  m.className = 'msg-meta';
  m.textContent = text;
  el.appendChild(m);
}

async function sendDocQuery(text) {
  const match = text.match(/^@doc:(\S+)\s*(.*)/s);
  if (!match) return;
  const [, filename, question] = match;
  appendMessage('user', text);
  const bubble = appendMessage('assistant', '', { streaming: true });
  try {
    const res = await fetch(`${BRAIN}/docs/query`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ filename, question }),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let acc = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      acc += dec.decode(value, { stream: true });
      bubble.textContent = acc;
      scrollBottom();
    }
    bubble.classList.remove('streaming');
    addMeta(bubble, `doc:${filename} · ${fmtTime(new Date())}`);
  } catch (e) {
    bubble.classList.remove('streaming');
    bubble.textContent = `[Doc query error: ${e.message}]`;
  }
}

function stopSend() {
  if (state.abortCtl) state.abortCtl.abort();
  // Aborting the fetch only cancels the initial POST — the server-side
  // generation keeps running. Tell the server to kill the in-flight bubble
  // (it cancels the in-flight generation server-side).
  if (state.pendingBubbleId) {
    fetch(`/api/cancel/${state.pendingBubbleId}`, { method: 'POST' }).catch(() => {});
  }
}

// ── Command tags ─────────────────────────────────────────────────────
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
  if (bubble) {
    renderImageTags(bubble, text);
    renderVideoTags(bubble, text);
  }
}

// Render any [[IMAGE:url]] tags from `text` as <img> elements appended to `bubble`.
// URLs starting with "/" are routed through the local server's /argus proxy.
function renderImageTags(bubble, text) {
  if (!bubble) return;
  const re = /\[\[IMAGE:([^\]]+)\]\]/g;
  let m;
  const hasTag = re.test(text);
  re.lastIndex = 0;  // reset after test
  console.log('[z-edit-debug] renderImageTags called', {hasTag, textLen: text?.length, textTail: text?.slice(-80)});
  while ((m = re.exec(text)) !== null) {
    const raw = m[1].trim();
    const src = raw.startsWith('http') ? raw
              : raw.startsWith('/')    ? `${BRAIN}${raw}`
                                       : raw;
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin-top:8px;cursor:pointer;display:inline-block;';
    wrap.title = 'Click to open full size';
    const img = document.createElement('img');
    img.src = src;
    img.style.cssText = 'max-width:512px;max-height:512px;border-radius:8px;display:block;border:1px solid #444;';
    img.alt = 'Generated image';
    wrap.appendChild(img);
    wrap.addEventListener('click', () => window.open(src, '_blank'));
    bubble.appendChild(wrap);
  }
}

// Render any [[VIDEO:url]] tags from `text` as a <video controls> element.
function renderVideoTags(bubble, text) {
  if (!bubble) return;
  const re = /\[\[VIDEO:([^\]]+)\]\]/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const raw = m[1].trim();
    const src = raw.startsWith('http') ? raw
              : raw.startsWith('/')    ? `${BRAIN}${raw}`
                                       : raw;
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin-top:8px;display:inline-block;';
    const vid = document.createElement('video');
    vid.src = src;
    vid.controls = true;
    vid.loop = true;
    vid.style.cssText = 'max-width:640px;max-height:640px;border-radius:8px;display:block;border:1px solid #444;background:#000;';
    wrap.appendChild(vid);
    bubble.appendChild(wrap);
  }
}

// ── SSE events ───────────────────────────────────────────────────────
let globalEvents = null;

function connectEvents() {
  try {
    globalEvents = new EventSource(`${BRAIN}/events`);
    globalEvents.onopen = () => { if(window._dbgLog) window._dbgLog('SSE: connected'); brainDot.classList.remove('down'); };
    globalEvents.onmessage = () => brainDot.classList.add('up');
    globalEvents.addEventListener('bubble_update', e => {
      try {
        const data = JSON.parse(e.data);
        if (data.id !== state.pendingBubbleId || !state.pendingMsgEl) return;
        const content = data.content || '';
        // Heartbeat: tokens are flowing -> keep the pill alive + reveal the bubble.
        if (state.statusPill) state.statusPill._activity();
        if (state.pendingMsgEl.classList.contains('typing')) {
          state.pendingMsgEl.classList.remove('typing');
          state.pendingMsgEl.classList.add('streaming');
        }
        if (/\[\[IMAGE:/.test(content)) console.log('[z-edit-debug] bubble_update has IMAGE tag', content.slice(-80));
        state.pendingMsgEl.textContent = content;
        scrollBottom();
      } catch {}
    });
    globalEvents.addEventListener('bubble_status', e => {
      try {
        const d = JSON.parse(e.data);
        if (d.id !== state.pendingBubbleId || !state.statusPill) return;
        const label = d.phase === 'tool' ? `using ${d.detail}`
                    : d.phase === 'loop' ? `retrying (loop)`
                    : (d.detail || d.phase);
        state.statusPill._status(label);
      } catch {}
    });
    // Rich per-turn activity for the claude-code path: thinking / tool calls /
    // tool results feed the tap-to-expand panel; the done event carries cost+duration.
    globalEvents.addEventListener('bubble_activity', e => {
      try {
        const d = JSON.parse(e.data);
        if (d.id !== state.pendingBubbleId || !state.statusPill) return;
        const ev = d.event || {};
        if (ev.kind === 'done') {
          const meta = fmtCostDuration(ev.cost, ev.duration_ms);
          if (meta) { state.statusPill._cost(' ' + meta); state.pendingCostMeta = ' ' + meta; }
        } else {
          state.statusPill._event(ev);
        }
      } catch {}
    });
    globalEvents.addEventListener('bubble_done', e => {
      let data;
      try { data = JSON.parse(e.data); } catch { return; }
      if(window._dbgLog) window._dbgLog('DONE: id=' + data.id + ' pending=' + state.pendingBubbleId + ' err=' + (data.error || ''));
      if (data.id !== state.pendingBubbleId) return;
      console.log('[z-edit-debug] bubble_done received', {id: data.id, db_id: data.db_id, error: data.error, hasResolve: !!state.pendingResolve});
      // If the backend signaled an error and no content streamed, surface it in the bubble.
      if (data.error && state.pendingMsgEl && !state.pendingMsgEl.textContent.trim()) {
        state.pendingMsgEl.textContent = `[Error: ${data.error}]`;
      }
      if (data.cancelled && state.pendingMsgEl && !state.pendingMsgEl.textContent.trim()) {
        state.pendingMsgEl.textContent = '[Cancelled]';
      }
      if (state.statusPill) state.statusPill._collapse();   // fold the activity panel back up
      state.pendingDbId   = data.db_id   ?? null;
      state.pendingUserId = data.user_id ?? null;
      if (state.pendingResolve) state.pendingResolve();
    });
    globalEvents.onerror = () => { if(window._dbgLog) window._dbgLog('SSE: ERROR, reconnecting...');
      brainDot.classList.add('down');
      if (globalEvents) { globalEvents.close(); globalEvents = null; }
      setTimeout(connectEvents, 3000);
    };
  } catch {
    setTimeout(connectEvents, 3000);
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
  state.conversationId = id;
  renderConversations();
  await loadHistory();
  closeOverlay('conv-drawer');
  switchView('chat');
}

async function newConversation() {
  try {
    const c = await fetchJson(`${BRAIN}/conversations`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ title: 'New conversation' }),
    });
    state.conversationId = c.id || c.conversation_id || null;
  } catch {
    state.conversationId = null;  // fall back to default thread
  }
  await loadConversations();
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
async function loadPulse() {
  const grid = $('machine-grid');
  grid.textContent = 'Pinging…';
  try {
    const { machines } = await fetchJson('/pulse');
    grid.innerHTML = machines.map(m => `
      <div class="machine-card">
        <div class="name">${escHtml(m.name)}</div>
        <div class="role">${escHtml(m.role)}</div>
        <div class="status">
          <span class="dot ${m.online ? 'online' : 'offline'}"></span>
          <span>${m.online ? 'Online' : 'Offline'}</span>
        </div>
        <div class="role" style="margin-top:4px">${escHtml(m.ip)}</div>
      </div>`).join('');
  } catch (e) {
    grid.innerHTML = `<div class="briefing-err">Pulse failed: ${escHtml(e.message)}</div>`;
  }
}

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
      lastModel: state.currentModel,
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
function openOverlay(id)  { $(id)?.classList.remove('hidden'); }
function closeOverlay(id) { $(id)?.classList.add('hidden'); }

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
    case 'memory-browser':  openMemoryBrowser(); break;
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
  if (id === 'network') loadPulse();
  if (id === 'canvas')  onCanvasResize();
  if (id === 'briefing' && !$('briefing-body').textContent.trim()) loadBriefing();
}

// ── UI wiring ───────────────────────────────────────────────────────
function wireUI() {
  // === Critical-path chat wiring FIRST so a later throw never disables send ===
  // Robust send-enabling: decoupled from autosize, fired on multiple events, so
  // the button can never get stuck disabled.
  const refreshSend = () => { sendBtn.disabled = !inputEl.value.trim(); };
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
    state.currentModel = modelSelect.value;
    saveLayout();
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
    if (messagesEl.scrollTop < 200) loadMoreHistory();
    const atBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
    if (scrollPill) scrollPill.classList.toggle('visible', !atBottom);
  }, { passive: true });
  if (scrollPill) {
    scrollPill.addEventListener('click', () => {
      scrollPill.classList.remove('visible');
      messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: 'smooth' });
    });
  }

  // Side nav
  document.querySelectorAll('.nav-tab[data-view]').forEach(t => {
    t.addEventListener('click', () => {
      switchView(t.dataset.view);
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
  $('nav-tools').addEventListener('click', () => {
    sideNav.classList.remove('open');
    const bd = document.getElementById('nav-backdrop');
    if (bd) bd.classList.remove('visible');
    openOverlay('tools-overlay');
  });
  $('nav-convs').addEventListener('click', () => {
    sideNav.classList.remove('open');
    const bd = document.getElementById('nav-backdrop');
    if (bd) bd.classList.remove('visible');
    openOverlay('conv-drawer');
  });

  // Tools overlay buttons
  document.querySelectorAll('#tools-overlay [data-action]').forEach(b => {
    b.addEventListener('click', () => handleToolsAction(b.dataset.action));
  });

  // Overlay closers (X buttons + backdrops)
  document.querySelectorAll('[data-close]').forEach(b => {
    b.addEventListener('click', () => closeOverlay(b.dataset.close));
  });
  $('conv-drawer-backdrop').addEventListener('click', () => closeOverlay('conv-drawer'));
  $('conv-close').addEventListener('click', () => closeOverlay('conv-drawer'));
  $('conv-new').addEventListener('click', () => newConversation());

  // (chat input + send listeners moved to wireUI critical path)

  // Refresh buttons
  $('briefing-refresh').addEventListener('click', loadBriefing);
  $('gpu-refresh').addEventListener('click', loadGpu);
  $('pulse-refresh').addEventListener('click', loadPulse);

  // Add widget
  addWidgetBtn.addEventListener('click', openWidgetPicker);

  // Esc closes any open overlay
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      ['tools-overlay', 'widget-picker-overlay', 'presets-overlay', 'memory-overlay', 'conv-drawer']
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
