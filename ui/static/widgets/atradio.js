// iHeart live radio — search, browse 45 genres, play any station, with a canvas
// visualizer. iHeart's API sends ACAO:* so the browser calls it directly (no proxy).
// Streams send no CORS, so the spectrum is a play-gated driven animation (not real FFT).
class AT40Widget extends WidgetBase {
    static type     = 'atradio';
    static label    = 'iHeart Radio';
    static icon     = '📻';
    static defaultW = 360;
    static defaultH = 460;

    static API = 'https://api.iheart.com/api';
    static DEFAULT = { id: 6545, name: 'Classic American Top 40', desc: 'Casey Kasem' };

    init() {
        this.playing = false;
        this.energy  = 0;
        this.station = (this.panel.config && this.panel.config.station) || AT40Widget.DEFAULT;
        this.el.innerHTML = `
          <div class="at40" style="display:flex;flex-direction:column;height:100%;padding:10px 12px 10px;gap:8px;min-height:0">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-weight:800;font-size:11px;letter-spacing:1px;color:#1a1206;
                     background:#e8b64c;padding:3px 7px;border-radius:6px">iHR</span>
              <div style="min-width:0;flex:1">
                <div class="at40-name" style="font-weight:700;font-size:13px;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div>
                <div class="at40-desc" style="font-size:11px;color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div>
              </div>
              <span class="at40-live" style="display:flex;align-items:center;gap:5px;font-size:10px;
                     letter-spacing:1px;font-weight:700;color:#ff5d5d">
                <span class="dot" style="width:7px;height:7px;border-radius:50%;background:#ff5d5d"></span>LIVE</span>
            </div>
            <canvas class="at40-viz" style="width:100%;height:64px;flex:0 0 auto;border-radius:10px;background:rgba(0,0,0,.25)"></canvas>
            <div style="display:flex;align-items:center;gap:12px">
              <button class="at40-play" aria-label="Play" style="width:48px;height:48px;flex:0 0 auto;border:none;
                     border-radius:50%;cursor:pointer;color:#1a1206;display:flex;align-items:center;justify-content:center;
                     background:radial-gradient(120% 120% at 30% 25%,#ffd679,#e8b64c 55%,#b98a2e);
                     box-shadow:0 6px 16px rgba(232,182,76,.35),inset 0 2px 3px rgba(255,255,255,.5)">
                <svg viewBox="0 0 24 24" fill="currentColor" width="22" height="22"><path d="M8 5v14l11-7z"/></svg>
              </button>
              <div style="flex:1;min-width:0">
                <div class="at40-status" style="font-weight:600;font-size:12px">Tap to tune in</div>
                <input class="at40-vol" type="range" min="0" max="1" step="0.01" value="0.9"
                       style="width:100%;margin-top:5px;accent-color:#e8b64c" aria-label="Volume">
              </div>
            </div>
            <div style="display:flex;gap:6px;padding-top:2px">
              <input class="at40-q" placeholder="Search stations…"
                style="flex:1;min-width:0;background:var(--surface2);border:1px solid var(--border);
                       color:var(--text);border-radius:8px;padding:6px 9px;outline:none;font-size:12px">
              <select class="at40-genre" style="background:var(--surface2);border:1px solid var(--border);
                       color:var(--text);border-radius:8px;padding:6px 6px;outline:none;font-size:12px;max-width:120px">
                <option value="">Genre…</option>
              </select>
            </div>
            <div class="at40-list" style="flex:1;min-height:0;overflow:auto;margin-top:2px"></div>
          </div>`;

        this.au = new Audio(); this.au.preload = 'none';
        this.nameEl = this.el.querySelector('.at40-name');
        this.descEl = this.el.querySelector('.at40-desc');
        this.btn    = this.el.querySelector('.at40-play');
        this.icon   = this.btn.querySelector('path');
        this.status = this.el.querySelector('.at40-status');
        this.cv     = this.el.querySelector('.at40-viz');
        this.ctx    = this.cv.getContext('2d');
        this.dotEl  = this.el.querySelector('.at40-live .dot');
        this.listEl = this.el.querySelector('.at40-list');
        this.qEl    = this.el.querySelector('.at40-q');
        this.genreEl= this.el.querySelector('.at40-genre');
        const vol   = this.el.querySelector('.at40-vol');

        this._setStation(this.station, false);   // show current, don't autoplay
        this.au.volume = parseFloat(vol.value);
        vol.oninput = () => { this.au.volume = parseFloat(vol.value); };

        this.btn.onclick = () => this._toggle();
        this.au.onplaying = () => this._setPlaying(true);
        this.au.onpause   = () => this._setPlaying(false);
        this.au.onwaiting = () => { this.status.textContent = 'Buffering…'; };
        this.au.onerror   = () => {
            this.status.textContent = 'Stream error — retrying';
            setTimeout(() => { if (this.playing) { this.au.load(); this.au.play().catch(() => {}); } }, 2500);
        };

        let t; this.qEl.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => this._search(this.qEl.value), 350); });
        this.genreEl.addEventListener('change', () => { if (this.genreEl.value) { this.qEl.value = ''; this._byGenre(this.genreEl.value); } });

        this._loadGenres();
        this._loop = (ts) => this._draw(ts);
        this._raf  = requestAnimationFrame(this._loop);
    }

    async _api(path) {
        const r = await fetch(AT40Widget.API + path, { headers: { 'Accept': 'application/json' } });
        if (!r.ok) throw new Error(r.status);
        return r.json();
    }

    async _loadGenres() {
        try {
            const d = await this._api('/v2/content/liveStationGenres');
            for (const g of (d.hits || [])) {
                const o = document.createElement('option');
                o.value = g.id; o.textContent = `${g.name} (${g.count})`;
                this.genreEl.appendChild(o);
            }
        } catch {}
    }

    async _search(q) {
        q = (q || '').trim();
        if (!q) { this.listEl.innerHTML = ''; return; }
        this.genreEl.value = '';
        this.listEl.innerHTML = `<div style="color:var(--text-dim);font-size:12px;padding:8px">Searching…</div>`;
        try {
            const d = await this._api(`/v3/search/all?keywords=${encodeURIComponent(q)}&maxRows=25&bundle=false`
                + `&station=true&artist=false&track=false&playlist=false&podcast=false&album=false&genre=false`);
            const sts = ((d.results || {}).stations) || [];
            this._renderList(sts.map(s => ({ id: s.id, name: s.name, desc: s.description || s.callLetters || '' })));
        } catch { this.listEl.innerHTML = `<div style="color:var(--text-dim);font-size:12px;padding:8px">Search failed.</div>`; }
    }

    async _byGenre(id) {
        this.listEl.innerHTML = `<div style="color:var(--text-dim);font-size:12px;padding:8px">Loading…</div>`;
        try {
            const d = await this._api(`/v2/content/liveStations?genreId=${encodeURIComponent(id)}&limit=40`);
            const hits = d.hits || [];
            this._renderList(hits.map(s => ({ id: s.id, name: s.name, desc: s.description || s.callLetters || '',
                                              streams: s.streams })));
        } catch { this.listEl.innerHTML = `<div style="color:var(--text-dim);font-size:12px;padding:8px">Couldn't load genre.</div>`; }
    }

    _renderList(items) {
        if (!items.length) { this.listEl.innerHTML = `<div style="color:var(--text-dim);font-size:12px;padding:8px">No stations.</div>`; return; }
        this.listEl.innerHTML = '';
        for (const s of items) {
            const row = document.createElement('div');
            const on = String(s.id) === String(this.station.id);
            row.style.cssText = `display:flex;align-items:center;gap:8px;padding:8px 8px;border-radius:8px;cursor:pointer;`
                + (on ? 'background:rgba(232,182,76,.14);' : '');
            row.innerHTML = `<span style="font-size:14px">📻</span>
              <span style="min-width:0;flex:1">
                <span style="display:block;font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${this._esc(s.name)}</span>
                <span style="display:block;font-size:11px;color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${this._esc(s.desc)}</span>
              </span>`;
            row.onmouseenter = () => { if (String(s.id) !== String(this.station.id)) row.style.background = 'rgba(255,255,255,.05)'; };
            row.onmouseleave = () => { if (String(s.id) !== String(this.station.id)) row.style.background = ''; };
            row.onclick = () => this._playStation(s);
            this.listEl.appendChild(row);
        }
    }

    _pickStream(streams) {
        if (!streams) return null;
        const u = streams.secure_shoutcast_stream || streams.shoutcast_stream
            || streams.secure_hls_stream || streams.hls_stream || null;
        return u ? u.replace(/^http:\/\//, 'https://') : null;   // avoid mixed-content on HTTPS pages
    }

    async _playStation(s) {
        this._setStation({ id: s.id, name: s.name, desc: s.desc || '' }, false);
        this.status.textContent = 'Tuning in…';
        let url = this._pickStream(s.streams);
        if (!url) {
            try {
                const d = await this._api(`/v2/content/liveStations/${s.id}`);
                const st = (d.hits || [d])[0] || {};
                url = this._pickStream(st.streams);
                if (!this.station.desc && st.description) { this.station.desc = st.description; this.descEl.textContent = st.description; }
            } catch {}
        }
        if (!url) { this.status.textContent = 'No stream for this station'; return; }
        this.au.src = url;
        try { this.au.load(); await this.au.play(); }
        catch { this.status.textContent = 'Tap ▶ to play'; }
        // repaint list highlight
        if (this.genreEl.value) this._byGenre(this.genreEl.value); else if (this.qEl.value) this._search(this.qEl.value);
    }

    _setStation(st, autoplay) {
        this.station = st;
        this.nameEl.textContent = st.name || '';
        this.descEl.textContent = st.desc || '';
        if (this.panel.config) { this.panel.config.station = st; if (typeof saveLayout === 'function') try { saveLayout(); } catch {} }
        if (!this.au.src && st.id === AT40Widget.DEFAULT.id) this.au.src = 'https://stream.revma.ihrhls.com/zc6545';
    }

    async _toggle() {
        if (this.playing) { this.au.pause(); return; }
        if (!this.au.src) { this._playStation(this.station); return; }
        this.status.textContent = 'Tuning in…';
        try { this.au.load(); await this.au.play(); } catch { this.status.textContent = 'Tap again to allow audio'; }
    }

    _setPlaying(p) {
        this.playing = p;
        this.icon.setAttribute('d', p ? 'M6 5h4v14H6zM14 5h4v14h-4z' : 'M8 5v14l11-7z');
        this.btn.setAttribute('aria-label', p ? 'Pause' : 'Play');
        this.status.textContent = p ? `On air — ${this.station.name}`.slice(0, 40) : 'Paused';
        this.dotEl.style.boxShadow = p ? '0 0 8px #ff5d5d' : 'none';
        if (p && 'mediaSession' in navigator) {
            navigator.mediaSession.metadata = new MediaMetadata(
                { title: this.station.name || 'iHeart Radio', artist: this.station.desc || '', album: 'iHeartRadio' });
        }
    }

    _draw(t) {
        const cv = this.cv, ctx = this.ctx, d = Math.min(2, window.devicePixelRatio || 1);
        const w = Math.round(cv.clientWidth * d), h = Math.round(cv.clientHeight * d);
        if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
        ctx.clearRect(0, 0, w, h);
        this.energy += ((this.playing ? 1 : 0) - this.energy) * 0.06;
        const N = 40, bw = w / N;
        for (let i = 0; i < N; i++) {
            const p = i / N;
            let a = Math.sin(t * 0.0022 + i * 0.55) * Math.sin(t * 0.0009 + i * 0.21);
            a = Math.abs(a) * (0.35 + 0.65 * Math.sin(p * Math.PI));
            a = a * this.energy + (this.playing ? Math.random() * 0.06 : 0.015);
            const bh = Math.max(2 * d, a * h * 0.92), x = i * bw + 1.5 * d, y = h - bh;
            const g = ctx.createLinearGradient(0, y, 0, h);
            g.addColorStop(0, '#ffd679'); g.addColorStop(1, '#b9791e');
            ctx.fillStyle = g; ctx.fillRect(x, y, bw - 3 * d, bh);
        }
        this._raf = requestAnimationFrame(this._loop);
    }

    _esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

    destroy() {
        super.destroy();
        if (this._raf) cancelAnimationFrame(this._raf);
        if (this.au) { try { this.au.pause(); } catch {} this.au.src = ''; this.au = null; }
    }
}
registerWidget(AT40Widget);
