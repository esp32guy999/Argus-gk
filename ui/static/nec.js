// NEC tab — ask a question, render the relevant NEC page(s) with the answer highlighted,
// plain-English box beneath, side arrows to page between multiple results.
(function () {
  const $ = (id) => document.getElementById(id);
  let hits = [], cur = 0, q = "", transCache = {}, lastAnswer = "";

  function el() {
    return {
      q: $('nec-q'), go: $('nec-go'), prev: $('nec-prev'), next: $('nec-next'),
      img: $('nec-page'), empty: $('nec-empty'), counter: $('nec-counter'),
      trans: $('nec-translate'),
    };
  }

  function renderCurrent() {
    const e = el();
    if (!hits.length) return;
    const h = hits[cur];
    const hl = encodeURIComponent((h.highlights || []).join('|'));
    e.img.hidden = false; e.empty.hidden = true;
    e.img.src = `/nec/render?page=${h.page}&hl=${hl}`;
    e.counter.textContent = hits.length > 1
      ? `Result ${cur + 1} of ${hits.length}${h.section ? ' · ' + h.section : ''} · p.${h.page + 1}`
      : (h.section ? `${h.section} · p.${h.page + 1}` : `p.${h.page + 1}`);
    const many = hits.length > 1;
    e.prev.hidden = !many; e.next.hidden = !many;
    e.prev.disabled = cur === 0; e.next.disabled = cur === hits.length - 1;
    showTranslation();
  }

  function showTranslation() {
    const e = el(), h = hits[cur];
    if (h.page in transCache) {
      e.trans.hidden = false;
      e.trans.innerHTML = `<div class="nec-trans-label">Plain English</div>${escapeHtml(transCache[h.page])}`;
      return;
    }
    e.trans.hidden = false;
    e.trans.innerHTML = `<div class="nec-trans-label">Plain English</div><span class="nec-dim">Translating…</span>`;
    fetch('/nec/translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q, page: h.page, section: h.section || '' }),
    }).then(r => r.json()).then(d => {
      transCache[h.page] = d.translation || d.error || '(no summary)';
      if (hits[cur] && hits[cur].page === h.page) showTranslation();
    }).catch(() => {
      e.trans.innerHTML = `<div class="nec-trans-label">Plain English</div><span class="nec-dim">Couldn't translate.</span>`;
    });
  }

  async function ask() {
    const e = el();
    q = (e.q.value || '').trim();
    if (!q) return;
    hits = []; cur = 0; transCache = {};
    e.go.disabled = true; e.go.textContent = '…';
    e.empty.hidden = false; e.img.hidden = true;
    e.empty.textContent = 'Searching the NEC…';
    e.counter.textContent = ''; e.trans.hidden = true;
    try {
      const d = await fetch('/nec/ask', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ q }),
      }).then(r => r.json());
      if (d.error && !(d.hits || []).length) { e.empty.textContent = 'Error: ' + d.error; return; }
      hits = d.hits || [];
      lastAnswer = d.answer || '';
      if (d.translation && hits.length) transCache[hits[0].page] = d.translation;
      if (!hits.length) {
        e.empty.textContent = d.answer
          ? `${d.answer}\n\n(Couldn't pin it to a page — try rephrasing.)`
          : 'No matching section found — try rephrasing.';
        if (d.answer) { e.trans.hidden = false; e.trans.innerHTML = `<div class="nec-trans-label">Answer</div>${escapeHtml(d.answer)}`; }
        return;
      }
      renderCurrent();
    } catch (err) {
      e.empty.textContent = 'Request failed: ' + err.message;
    } finally {
      e.go.disabled = false; e.go.textContent = 'Ask';
    }
  }

  function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

  // ── Fullscreen lightbox ──────────────────────────────────────────
  function openZoom() {
    if (!hits.length) return;
    const h = hits[cur];
    const img = $('nec-zoom-img'), ans = $('nec-zoom-answer'), ov = $('nec-zoom');
    const hl = encodeURIComponent((h.highlights || []).join('|'));
    img.classList.remove('zoomed');
    img.src = `/nec/render?page=${h.page}&hl=${hl}&dpi=240`;   // crisp for fullscreen
    const summary = lastAnswer || (transCache[h.page] || '').split(/(?<=\.)\s/)[0] || 'Answer';
    ans.textContent = summary;
    ov.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    $('nec-zoom-scroll').scrollTop = 0;
  }
  function closeZoom() {
    $('nec-zoom').classList.add('hidden');
    document.body.style.overflow = '';
  }

  function wire() {
    const e = el();
    if (!e.go) return false;
    e.go.addEventListener('click', ask);
    e.q.addEventListener('keydown', ev => { if (ev.key === 'Enter') ask(); });
    e.prev.addEventListener('click', () => { if (cur > 0) { cur--; renderCurrent(); } });
    e.next.addEventListener('click', () => { if (cur < hits.length - 1) { cur++; renderCurrent(); } });
    e.img.addEventListener('click', openZoom);
    $('nec-zoom-close').addEventListener('click', closeZoom);
    $('nec-zoom-img').addEventListener('click', () => $('nec-zoom-img').classList.toggle('zoomed'));
    document.addEventListener('keydown', ev => {
      if (ev.key === 'Escape' && !$('nec-zoom').classList.contains('hidden')) closeZoom();
    });
    return true;
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
})();
