// Audiobook browser — search AudiobookBay (via Argus on the VPN host) + one-tap grab.
class AudiobookWidget extends WidgetBase {
    static type     = 'audiobook';
    static label    = 'Audiobooks';
    static icon     = '🎧';
    static defaultW = 360;   // fits a phone (iPhone 13 = 390px) out of the box
    static defaultH = 520;

    init() {
        this.el.innerHTML = `
          <div class="ab-wrap" style="display:flex;flex-direction:column;height:100%;font-size:13px">
            <div style="display:flex;gap:6px;padding:6px">
              <input class="ab-q" placeholder="Search AudiobookBay…"
                style="flex:1;background:var(--surface2);border:1px solid var(--border);
                       color:var(--text);border-radius:8px;padding:6px 9px;outline:none">
              <button class="ab-go" style="background:var(--accent);border:none;color:#fff;
                       border-radius:8px;padding:6px 12px;cursor:pointer">Search</button>
            </div>
            <div class="ab-chips" style="display:flex;flex-wrap:wrap;gap:4px;padding:0 6px 4px"></div>
            <div class="ab-results" style="flex:1;overflow:auto;padding:6px"></div>
          </div>`;
        this.q       = this.el.querySelector('.ab-q');
        this.results = this.el.querySelector('.ab-results');
        this.el.querySelector('.ab-go').onclick = () => this._search(this.q.value);
        this.q.addEventListener('keydown', e => { if (e.key === 'Enter') this._search(this.q.value); });
        this._loadGenres();
        this._latest();
    }

    async _loadGenres() {
        try {
            const g = await fetch('/argus/audiobook/genres').then(r => r.json());
            this.el.querySelector('.ab-chips').innerHTML = g.map(name =>
                `<span class="ab-chip" style="background:var(--surface2);border:1px solid var(--border);
                  border-radius:12px;padding:2px 9px;font-size:11px;cursor:pointer">${name}</span>`).join('');
            this.el.querySelectorAll('.ab-chip').forEach(c =>
                c.onclick = () => { this.q.value = c.textContent; this._search(c.textContent); });
        } catch {}
    }

    _msg(html) { this.results.innerHTML = `<div style="color:var(--text-dim);padding:8px">${html}</div>`; }

    async _latest() {
        this._msg('Loading latest…');
        try {
            this._render(await fetch('/argus/audiobook/latest').then(r => r.json()), 'Latest on AudiobookBay');
        } catch { this._msg('Could not load AudiobookBay (is the VPN up?)'); }
    }

    async _search(query) {
        if (!query || !query.trim()) return this._latest();
        this._msg(`Searching “${query}”…`);
        try {
            const res = await fetch(`/argus/audiobook/search?q=${encodeURIComponent(query)}`).then(r => r.json());
            if (!res.length) return this._msg(`No results for “${query}”. AudiobookBay rate-limits — try again in a moment.`);
            this._render(res, `Results for “${query}”`);
        } catch { this._msg('Search failed (AudiobookBay unreachable / rate-limited). Try again shortly.'); }
    }

    _render(items, heading) {
        const cards = items.map((it, i) => `
          <div style="display:flex;gap:8px;padding:6px;border-bottom:1px solid var(--border);align-items:center">
            ${it.cover ? `<img src="${it.cover}" loading="lazy"
                 style="width:42px;height:60px;object-fit:cover;border-radius:4px;flex:none"
                 onerror="this.style.display='none'">` :
                `<div style="width:42px;height:60px;border-radius:4px;background:var(--surface2);flex:none"></div>`}
            <div style="flex:1;min-width:0;line-height:1.3">${this._esc(it.title)}</div>
            <button data-i="${i}" class="ab-get" style="background:var(--surface2);border:1px solid var(--border);
              color:var(--text);border-radius:8px;padding:5px 10px;cursor:pointer;flex:none">⬇ Get</button>
          </div>`).join('');
        this.results.innerHTML = `<div style="color:var(--text-dim);font-size:11px;padding:2px 6px 6px">${heading}</div>${cards}`;
        this.results.querySelectorAll('.ab-get').forEach(btn =>
            btn.onclick = () => this._grab(items[+btn.dataset.i], btn));
    }

    async _grab(item, btn) {
        btn.textContent = '…'; btn.disabled = true;
        try {
            const r = await fetch('/argus/audiobook/grab', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: item.url, title: item.title }),
            });
            if (!r.ok) throw new Error(await r.text());
            btn.textContent = '✓ Downloading';
            btn.style.borderColor = 'var(--green)'; btn.style.color = 'var(--green)';
        } catch (e) {
            btn.textContent = '✕ Failed'; btn.disabled = false;
            btn.style.color = 'var(--red)';
        }
    }

    _esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
}
registerWidget(AudiobookWidget);
