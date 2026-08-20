// Argus chat — inbox + poll. Anvil writes the reply.
//
// This module is the only painter for the transcript. It does not open
// an event stream, does not infer a stall from token silence, and does not
// wipe the thread except when switching conversations.
//
// Contract: POST /argus/chat {to, client_msg_id} + GET /history?after_id=
// + GET /turn_status (active is the only heartbeat).
//
// Loaded before app.js. Names like `state` resolve at call time.

const ArgusChat = (() => {
  const PAGE = 40;
  const DOM_CAP = 80;
  const POLL_MS = 4000;
  const STATUS_MS = 1000;

  let pollTimer = null;
  let statusTimer = null;
  let sendGen = 0;
  let wired = false;
  let lastTurnActive = false;
  let follow = true;

  function messagesHost() {
    return (typeof messagesEl !== 'undefined' && messagesEl) || document.getElementById('chat-messages');
  }

  function cid() {
    return (state && state.conversationId) || 'default';
  }

  function historyQuery(extra) {
    const p = new URLSearchParams();
    if (state && state.conversationId != null) p.set('conversation_id', state.conversationId);
    extra = extra || {};
    for (const [k, v] of Object.entries(extra)) {
      if (v != null) p.set(k, v);
    }
    const qs = p.toString();
    return qs ? `?${qs}` : '';
  }

  function newClientMsgId() {
    return (crypto.randomUUID && crypto.randomUUID()) || (Date.now() + '-' + Math.random());
  }

  function brainUp(ok) {
    if (typeof brainDot === 'undefined' || !brainDot) return;
    brainDot.classList.toggle('down', !ok);
    brainDot.classList.toggle('up', !!ok);
  }

  function refreshSendBtn() {
    if (typeof sendBtn === 'undefined' || !sendBtn) return;
    const text = (typeof inputEl !== 'undefined' && inputEl) ? inputEl.value.trim() : '';
    const files = (typeof stagedFiles !== 'undefined' && stagedFiles.length) || 0;
    if (!sendBtn.classList.contains('is-stop')) sendBtn.disabled = !text && !files;
  }

  function scrollBottom() {
    const el = messagesHost();
    if (el) el.scrollTop = el.scrollHeight;
  }

  function atBottom() {
    const el = messagesHost();
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  function scrollPill() {
    return document.getElementById('scroll-pill');
  }

  function updateScrollPill() {
    const pill = scrollPill();
    if (pill) pill.classList.toggle('visible', !follow);
  }

  function setFollow(on) {
    follow = !!on;
    if (follow) {
      scrollBottom();
      requestAnimationFrame(scrollBottom);
    }
    updateScrollPill();
  }

  function reveal() {
    if (follow) {
      scrollBottom();
      requestAnimationFrame(scrollBottom);
    } else {
      updateScrollPill();
    }
  }

  function onScroll() {
    const el = messagesHost();
    if (!el) return;
    if (el.scrollTop < 200) loadMoreHistory();
    follow = atBottom();
    updateScrollPill();
  }

  function jumpToLatest() {
    setFollow(true);
  }

  function trimOld() {
    if (!follow) return;
    const host = messagesHost();
    if (!host) return;
    const rows = [...host.querySelectorAll(':scope > .swipe-row')];
    if (rows.length <= DOM_CAP) return;
    const drop = rows.length - DOM_CAP;
    for (let i = 0; i < drop; i++) {
      const row = rows[i];
      const prev = row.previousElementSibling;
      row.remove();
      if (prev && prev.classList.contains('timestamp-label')) {
        const next = prev.nextElementSibling;
        if (!next || next.classList.contains('timestamp-label')) prev.remove();
      }
    }
    while (host.firstElementChild && host.firstElementChild.classList.contains('timestamp-label')) {
      const next = host.firstElementChild.nextElementSibling;
      if (!next || next.classList.contains('timestamp-label')) host.firstElementChild.remove();
      else break;
    }
    const first = host.querySelector('[data-msg-id]');
    if (first && first.dataset.msgId) state.oldestMsgId = Number(first.dataset.msgId);
    state.historyExhausted = false;
  }

  // ── paint ──────────────────────────────────────────────────────────

  function stripTags(text) {
    if (typeof stripCommandTags === 'function') return stripCommandTags(text);
    return String(text || '').replace(/\[\[(?!CHOICES:|LINKS:)[^\]]+\]\]/g, '').trim();
  }

  function paintMedia(bubble, raw) {
    if (!bubble || !raw) return;
    const host = (typeof BRAIN !== 'undefined' && BRAIN) || '/argus';
    function srcOf(rawUrl) {
      const u = String(rawUrl || '').trim();
      if (/^https?:\/\//i.test(u)) return u;
      if (u.startsWith('/')) return host + u;
      return u;
    }
    let m;
    const imgRe = /\[\[IMAGE:([^\]]+)\]\]/g;
    while ((m = imgRe.exec(raw)) !== null) {
      const src = srcOf(m[1]);
      const wrap = document.createElement('div');
      wrap.style.cssText = 'margin-top:8px;cursor:pointer;display:inline-block;';
      const img = document.createElement('img');
      img.src = src;
      img.alt = 'Generated image';
      img.style.cssText = 'max-width:512px;max-height:512px;border-radius:8px;display:block;border:1px solid #444;';
      img.addEventListener('load', () => { if (follow) scrollBottom(); });
      wrap.appendChild(img);
      wrap.addEventListener('click', () => window.open(src, '_blank'));
      bubble.appendChild(wrap);
    }
    const vidRe = /\[\[VIDEO:([^\]]+)\]\]/g;
    while ((m = vidRe.exec(raw)) !== null) {
      const src = srcOf(m[1]);
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

  function paintLinks(bubble, raw) {
    const m = String(raw || '').match(/\[\[LINKS:([\s\S]*?)\]\]/);
    if (!m) return;
    const wrap = document.createElement('div');
    wrap.className = 'chat-links';
    for (const pair of m[1].split(';;')) {
      const [label, url] = pair.split('|').map(s => s.trim());
      if (!url || !/^https?:\/\//.test(url)) continue;
      const a = document.createElement('a');
      a.className = 'chat-link';
      a.href = url;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = label || url;
      wrap.appendChild(a);
    }
    if (wrap.childElementCount) bubble.appendChild(wrap);
  }

  function paintChoices(bubble, raw) {
    const m = String(raw || '').match(/\[\[CHOICES:([\s\S]*?)\]\]/);
    if (!m) return;
    const wrap = document.createElement('div');
    wrap.className = 'chat-links';
    for (const rawOpt of m[1].split(';;')) {
      const label = rawOpt.trim();
      if (!label) continue;
      const b = document.createElement('button');
      b.className = 'chat-link';
      b.type = 'button';
      b.textContent = label;
      b.addEventListener('click', () => {
        if (typeof inputEl !== 'undefined' && inputEl) inputEl.value = label;
        refreshSendBtn();
        send();
      });
      wrap.appendChild(b);
    }
    if (wrap.childElementCount) bubble.appendChild(wrap);
  }

  function addMeta(el, text) {
    if (!text) return;
    const m = document.createElement('span');
    m.className = 'msg-meta';
    m.textContent = text;
    el.appendChild(m);
  }

  function speakerName(model) {
    if (!model) return '';
    if (typeof RT_SPEAKER !== 'undefined' && RT_SPEAKER[model]) return RT_SPEAKER[model];
    if (typeof modelLabel === 'function') return modelLabel(model);
    return model;
  }

  function wrapSwipe(msgEl) {
    const row = document.createElement('div');
    row.className = 'swipe-row';
    const quoteBg = document.createElement('div');
    quoteBg.className = 'swipe-quote-bg';
    quoteBg.textContent = '⧉ Copy';
    const bg = document.createElement('div');
    bg.className = 'swipe-delete-bg';
    bg.textContent = 'Delete';
    row.appendChild(quoteBg);
    row.appendChild(bg);
    row.appendChild(msgEl);

    let startX = 0, startY = 0, dx = 0, tracking = false, capturedSel = '';
    const THRESHOLD = 0.30;

    row.addEventListener('touchstart', e => {
      const t = e.touches[0];
      startX = t.clientX;
      startY = t.clientY;
      dx = 0;
      tracking = true;
      capturedSel = '';
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim()
          && msgEl.contains(sel.anchorNode) && msgEl.contains(sel.focusNode)) {
        capturedSel = sel.toString().trim();
      }
      row.classList.add('swiping');
    }, { passive: true });

    row.addEventListener('touchmove', e => {
      if (!tracking) return;
      const t = e.touches[0];
      const deltaX = t.clientX - startX;
      const deltaY = t.clientY - startY;
      if (Math.abs(deltaY) > Math.abs(deltaX) && Math.abs(dx) < 10) {
        tracking = false;
        row.classList.remove('swiping');
        msgEl.style.transform = '';
        return;
      }
      dx = deltaX;
      if (Math.abs(dx) > 4) e.preventDefault();
      msgEl.style.transform = `translateX(${dx}px)`;
    }, { passive: false });

    row.addEventListener('touchend', () => {
      if (!tracking) return;
      tracking = false;
      row.classList.remove('swiping');
      const pct = Math.abs(dx) / row.offsetWidth;
      const id = msgEl.dataset.msgId;
      if (pct >= THRESHOLD && dx < 0 && id) {
        msgEl.style.transition = 'transform 0.2s ease';
        msgEl.style.transform = `translateX(-${row.offsetWidth}px)`;
        msgEl.addEventListener('transitionend', async () => {
          try {
            await fetch(`${BRAIN}/history/${encodeURIComponent(id)}`, { method: 'DELETE' });
          } catch {}
          row.remove();
        }, { once: true });
      } else if (pct >= THRESHOLD && dx > 0) {
        if (typeof showCopySheet === 'function') showCopySheet(capturedSel || bubbleText(msgEl));
        msgEl.style.transition = 'transform 0.2s ease';
        msgEl.style.transform = '';
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

  function bubbleText(msgEl) {
    const clone = msgEl.cloneNode(true);
    clone.querySelectorAll('.msg-meta').forEach(e => e.remove());
    return clone.textContent.trim();
  }

  function paintBubble(msg, opts) {
    opts = opts || {};
    const el = document.createElement('div');
    const role = msg.role === 'user' ? 'user' : 'assistant';
    el.className = `message ${role}`;
    if (msg.id != null) el.dataset.msgId = String(msg.id);
    if (msg.client_msg_id) el.dataset.clientMsgId = msg.client_msg_id;
    const raw = msg.content || '';
    el.textContent = role === 'assistant' ? stripTags(raw) : raw;
    if (role === 'assistant') {
      if (msg.model && typeof modelColor === 'function') {
        el.style.setProperty('--msg-accent', modelColor(msg.model));
      }
      paintLinks(el, raw);
      paintChoices(el, raw);
      paintMedia(el, raw);
      // Canvas side-effects only for newly arrived rows — never on a history wipe.
      if (opts.sideEffects && typeof processCommandTags === 'function') {
        try { processCommandTags(raw, el); } catch {}
      }
    }
    const when = msg.timestamp && typeof fmtTime === 'function' ? fmtTime(msg.timestamp) : '';
    const who = role === 'assistant' ? speakerName(msg.model) : '';
    addMeta(el, [who, when].filter(Boolean).join(' · '));
    return wrapSwipe(el);
  }

  function buildNodes(msgs) {
    const frag = document.createDocumentFragment();
    let lastDay = null;
    (msgs || []).forEach(msg => {
      if (msg.timestamp && typeof fmtDay === 'function') {
        const day = fmtDay(msg.timestamp);
        if (day !== lastDay) {
          lastDay = day;
          const lbl = document.createElement('div');
          lbl.className = 'timestamp-label';
          lbl.textContent = day;
          frag.appendChild(lbl);
        }
      }
      frag.appendChild(paintBubble(msg));
    });
    return frag;
  }

  function findBubble(msg) {
    const host = messagesHost();
    if (!host) return null;
    if (msg.id != null) {
      const byId = host.querySelector(`[data-msg-id="${CSS.escape(String(msg.id))}"]`);
      if (byId) return byId;
    }
    if (msg.client_msg_id) {
      return host.querySelector(`[data-client-msg-id="${CSS.escape(String(msg.client_msg_id))}"]`);
    }
    return null;
  }

  function mergeHistory(msgs) {
    const host = messagesHost();
    if (!host || !msgs || !msgs.length) return;
    let added = 0;
    for (const m of msgs) {
      if (m.id != null) state.newestMsgId = Math.max(state.newestMsgId || 0, m.id);
      const existing = findBubble(m);
      if (existing) {
        if (m.id != null) existing.dataset.msgId = String(m.id);
        continue;
      }
      host.appendChild(paintBubble(m, { sideEffects: true }));
      added += 1;
    }
    if (added) {
      trimOld();
      reveal();
    }
  }

  function renderHistory(msgs) {
    const host = messagesHost();
    if (!host) return;
    host.innerHTML = '';
    host.appendChild(buildNodes(msgs));
    if (msgs && msgs.length) {
      const last = msgs[msgs.length - 1];
      if (last && last.id != null) state.newestMsgId = last.id;
    } else {
      state.newestMsgId = null;
    }
    setFollow(true);
  }

  function prependHistory(msgs) {
    const host = messagesHost();
    if (!host || !msgs || !msgs.length) return;
    const prevHeight = host.scrollHeight;
    const prevTop = host.scrollTop;
    host.insertBefore(buildNodes(msgs), host.firstChild);
    host.scrollTop = prevTop + (host.scrollHeight - prevHeight);
  }

  // Local-only notice (presets, errors). Not a stored row.
  function appendMessage(role, text, opts) {
    opts = opts || {};
    const host = messagesHost();
    if (!host) return null;
    const el = document.createElement('div');
    el.className = `message ${role}${opts.streaming ? ' streaming' : ''}`;
    el.textContent = text || '';
    if (opts.meta) addMeta(el, opts.meta);
    host.appendChild(wrapSwipe(el));
    reveal();
    return el;
  }

  // ── history ────────────────────────────────────────────────────────

  async function loadHistory() {
    state.oldestMsgId = null;
    state.historyExhausted = false;
    state.historyLoading = false;
    let msgs;
    try {
      msgs = await fetchJson(`${BRAIN}/history${historyQuery({ limit: PAGE })}`);
      brainUp(true);
    } catch {
      msgs = [];
      brainUp(false);
    }
    renderHistory(msgs);
    if (msgs && msgs.length) state.oldestMsgId = msgs[0].id;
    if (!msgs || msgs.length < PAGE) state.historyExhausted = true;
    await syncTurnChrome();
  }

  async function loadMoreHistory() {
    if (state.historyLoading || state.historyExhausted || state.oldestMsgId == null) return;
    state.historyLoading = true;
    let msgs = [];
    try {
      msgs = await fetchJson(`${BRAIN}/history${historyQuery({ limit: PAGE, before_id: state.oldestMsgId })}`);
    } catch {
      msgs = [];
    }
    if (!msgs || !msgs.length) {
      state.historyExhausted = true;
      state.historyLoading = false;
      return;
    }
    prependHistory(msgs);
    state.oldestMsgId = msgs[0].id;
    if (msgs.length < PAGE) state.historyExhausted = true;
    state.historyLoading = false;
  }

  async function mergeAfter() {
    try {
      const after = state.newestMsgId != null
        ? { after_id: state.newestMsgId, limit: 50 }
        : { limit: PAGE };
      const latest = await fetchJson(`${BRAIN}/history${historyQuery(after)}`);
      brainUp(true);
      if (!latest || !latest.length) return;
      if (state.newestMsgId != null) mergeHistory(latest);
      else renderHistory(latest);
    } catch {
      brainUp(false);
    }
  }

  // ── turn chrome ────────────────────────────────────────────────────

  function heartbeatPill(st) {
    if (!st || !st.active) return;
    if (typeof state === 'undefined' || !state.statusPill || !state.statusPill._heartbeat) return;
    const p = st.phase || '', d = st.detail || '';
    const lab = p === 'tool' ? ('using ' + (d || 'a tool'))
              : p === 'working' ? (d || 'still working')
              : p === 'writing' ? 'writing reply'
              : (d || p || 'working');
    state.statusPill._heartbeat(lab);
  }

  function ensureThinking() {
    if (typeof state === 'undefined') return;
    if (state.statusPill) return;
    if (typeof startThinkingIndicators === 'function') startThinkingIndicators(null);
  }

  function clearThinking() {
    if (typeof stopThinkingIndicators === 'function') stopThinkingIndicators();
  }

  function setStopMode(on) {
    if (typeof sendBtn === 'undefined' || !sendBtn) return;
    sendBtn.classList.toggle('is-stop', !!on);
    if (on) sendBtn.disabled = false;
    else refreshSendBtn();
  }

  async function fetchTurnStatus() {
    const q = '?conversation_id=' + encodeURIComponent(cid());
    const r = await fetch(BRAIN + '/turn_status' + q);
    if (!r.ok) throw new Error('turn_status ' + r.status);
    return r.json();
  }

  async function syncTurnChrome() {
    let st;
    try { st = await fetchTurnStatus(); }
    catch { return null; }
    const active = !!(st && st.active);
    if (active) {
      state.pendingBubbleId = st.bubble_id || state.pendingBubbleId;
      ensureThinking();
      heartbeatPill(st);
      setStopMode(true);
    } else {
      if (lastTurnActive) {
        await mergeAfter();
        clearThinking();
      }
      if (!state.sending) {
        state.pendingBubbleId = null;
        setStopMode(false);
      }
    }
    lastTurnActive = active;
    return st;
  }

  // ── send ───────────────────────────────────────────────────────────

  function snapshotAttachments() {
    const files = (typeof stagedFiles !== 'undefined') ? stagedFiles : [];
    if (files.some(s => !s.dataUrl)) {
      appendMessage('assistant',
        '📎 One or more staged files failed to load into memory. Re-attach and try again.');
      return null;
    }
    return files
      .filter(s => s.dataUrl)
      .map(s => ({ filename: s.filename, isImage: s.isImage, dataUrl: s.dataUrl }));
  }

  function paintOptimisticUser(text, sentFiles, clientMsgId) {
    const label = text || (sentFiles.length === 1 ? sentFiles[0].filename : `(${sentFiles.length} files)`);
    const userEl = appendMessage('user', label);
    userEl.dataset.clientMsgId = clientMsgId;
    (sentFiles || []).forEach(s => {
      if (s.isImage && s.dataUrl) {
        const img = document.createElement('img');
        img.src = s.dataUrl;
        img.style.cssText = 'display:block;margin-top:6px;max-width:240px;max-height:240px;border-radius:6px;border:1px solid #444;';
        userEl.appendChild(img);
      } else {
        const card = document.createElement('div');
        card.style.cssText = 'display:inline-flex;align-items:center;gap:6px;margin-top:6px;padding:6px 10px;background:#2a2a2a;border:1px solid #444;border-radius:6px;font-size:0.85em;color:#ddd;max-width:240px;';
        const icon = (typeof _fileIcon === 'function') ? _fileIcon(s.filename) : '📎';
        card.textContent = `${icon} ${s.filename}`;
        userEl.appendChild(card);
      }
    });
    return userEl;
  }

  async function postChat(body) {
    const res = await fetch(`${BRAIN}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok) {
      const detail = data.detail || data.error || '';
      const err = new Error(detail ? `[Error ${res.status}] ${detail}` : `[Error ${res.status}]`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async function waitUntilIdle(gen, room) {
    for (;;) {
      if (gen !== sendGen || cid() !== room) return null;
      let st;
      try { st = await fetchTurnStatus(); }
      catch {
        await sleep(STATUS_MS);
        continue;
      }
      if (!st.active) return st;
      heartbeatPill(st);
      if (st.bubble_id) state.pendingBubbleId = st.bubble_id;
      await sleep(STATUS_MS);
    }
  }

  function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
  }

  async function send() {
    const text = (typeof inputEl !== 'undefined' && inputEl) ? inputEl.value.trim() : '';
    const targets = (typeof sendTargets === 'function') ? sendTargets() : [];
    if (state.sending) return;
    if (!text && !(typeof stagedFiles !== 'undefined' && stagedFiles.length)) return;
    if (!targets.length) {
      if (typeof showToast === 'function') showToast('Pick a model from the dropdown or tap GK');
      return;
    }

    let st = null;
    try { st = await fetchTurnStatus(); } catch {}
    if (st && st.active) {
      if (typeof showToast === 'function') showToast('Still working — wait or tap stop');
      ensureThinking();
      heartbeatPill(st);
      setStopMode(true);
      return;
    }

    const attachments = snapshotAttachments();
    if (attachments === null) return;

    const sentFiles = (typeof stagedFiles !== 'undefined') ? stagedFiles.slice() : [];
    if (typeof stagedFiles !== 'undefined') {
      stagedFiles.length = 0;
      if (typeof _renderStaging === 'function') _renderStaging();
    }
    if (typeof inputEl !== 'undefined' && inputEl) {
      inputEl.value = '';
      if (typeof autosizeInput === 'function') autosizeInput();
    }
    refreshSendBtn();

    const clientMsgId = newClientMsgId();
    const room = cid();
    const userEl = paintOptimisticUser(text, sentFiles, clientMsgId);
    const gen = ++sendGen;
    state.sending = true;
    setStopMode(true);
    setFollow(true);
    ensureThinking();
    reveal();

    const body = {
      model: (typeof activeModel === 'function') ? activeModel() : targets[0],
      to: targets,
      client_msg_id: clientMsgId,
      message: text,
      conversation_id: state.conversationId,
      persona: state.persona || 'argus',
    };
    if (attachments.length) body.attachments = attachments;

    let data;
    try {
      try {
        data = await postChat(body);
      } catch (e) {
        if (!e.status) {
          // network blip — same client_msg_id, do not add another user bubble
          data = await postChat(body);
        } else {
          throw e;
        }
      }
    } catch (e) {
      if (userEl) {
        const row = userEl.closest('.swipe-row');
        (row || userEl).remove();
      }
      appendMessage('assistant', e.message || String(e));
      if (sentFiles.length && typeof stagedFiles !== 'undefined' && !stagedFiles.length) {
        sentFiles.forEach(s => stagedFiles.push(s));
        if (typeof _renderStaging === 'function') _renderStaging();
      }
      state.sending = false;
      clearThinking();
      setStopMode(false);
      return;
    }

    if (data.user_id && userEl) userEl.dataset.msgId = String(data.user_id);
    if (data.id) state.pendingBubbleId = data.id;
    state.newestMsgId = Math.max(state.newestMsgId || 0, data.user_id || 0);

    if (data.done && data.replay) {
      await mergeAfter();
      state.sending = false;
      clearThinking();
      setStopMode(false);
      return;
    }

    await waitUntilIdle(gen, room);
    if (gen !== sendGen || cid() !== room) {
      state.sending = false;
      return;
    }
    await mergeAfter();
    state.sending = false;
    state.pendingBubbleId = null;
    clearThinking();
    setStopMode(false);
    lastTurnActive = false;
  }

  function leaveTurn() {
    // Room change / abandon UI wait. Anvil keeps generating.
    sendGen += 1;
    state.sending = false;
    clearThinking();
    setStopMode(false);
  }

  async function stop() {
    const id = state.pendingBubbleId;
    leaveTurn();
    if (id) {
      fetch(`/api/cancel/${id}`, { method: 'POST' }).catch(() => {});
    }
    await mergeAfter();
  }

  // ── poll loop ──────────────────────────────────────────────────────

  async function pollTick() {
    if (document.visibilityState !== 'visible') return;
    const st = await syncTurnChrome();
    if (st && st.active) return;
    if (state.sending) return;
    await mergeAfter();
  }

  function startPolling() {
    if (!wired) {
      wired = true;
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          if (typeof _resurfaceLastMessage === 'function') _resurfaceLastMessage();
          pollTick();
        }
      });
    }
    if (!pollTimer) pollTimer = setInterval(pollTick, POLL_MS);
    pollTick();
  }

  return {
    boot: async function boot() {
      await loadHistory();
      startPolling();
    },
    send,
    stop,
    loadHistory,
    loadMoreHistory,
    mergeHistory,
    appendMessage,
    startPolling,
    refreshSendBtn,
    scrollBottom,
    leaveTurn,
    onScroll,
    jumpToLatest,
  };
})();

window.ArgusChat = ArgusChat;
window.send = function send() { return ArgusChat.send(); };
window.stopSend = function stopSend() { return ArgusChat.stop(); };
window.loadHistory = function loadHistory() { return ArgusChat.loadHistory(); };
window.loadMoreHistory = function loadMoreHistory() { return ArgusChat.loadMoreHistory(); };
window.mergeHistory = function mergeHistory(msgs) { return ArgusChat.mergeHistory(msgs); };
window.appendMessage = function appendMessage(role, text, opts) { return ArgusChat.appendMessage(role, text, opts); };
window.scrollBottom = function scrollBottom() { return ArgusChat.scrollBottom(); };
