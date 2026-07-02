// Classic AT40 — live iHeart stream (Casey Kasem) with a canvas visualizer.
// The stream sends no CORS headers, so the spectrum is a play-gated driven animation
// (not real FFT). Swap to a CORS proxy on anvil if a true spectrum is ever wanted.
class AT40Widget extends WidgetBase {
    static type     = 'atradio';
    static label    = 'Classic AT40';
    static icon     = '📻';
    static defaultW = 340;
    static defaultH = 280;

    static STREAM = 'https://stream.revma.ihrhls.com/zc6545';

    init() {
        this.playing = false;
        this.energy  = 0;
        this.el.innerHTML = `
          <div class="at40" style="display:flex;flex-direction:column;height:100%;padding:10px 12px 12px;gap:8px">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-weight:800;font-size:11px;letter-spacing:1px;color:#1a1206;
                     background:#e8b64c;padding:3px 7px;border-radius:6px">AT 40</span>
              <div style="min-width:0;flex:1">
                <div style="font-weight:700;font-size:13px;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Classic American Top 40</div>
                <div style="font-size:11px;color:var(--text-dim)">Casey Kasem</div>
              </div>
              <span class="at40-live" style="display:flex;align-items:center;gap:5px;font-size:10px;
                     letter-spacing:1px;font-weight:700;color:#ff5d5d">
                <span class="dot" style="width:7px;height:7px;border-radius:50%;background:#ff5d5d"></span>LIVE</span>
            </div>
            <canvas class="at40-viz" style="width:100%;flex:1;min-height:70px;border-radius:10px;background:rgba(0,0,0,.25)"></canvas>
            <div style="display:flex;align-items:center;gap:12px">
              <button class="at40-play" aria-label="Play" style="width:52px;height:52px;flex:0 0 auto;border:none;
                     border-radius:50%;cursor:pointer;color:#1a1206;display:flex;align-items:center;justify-content:center;
                     background:radial-gradient(120% 120% at 30% 25%,#ffd679,#e8b64c 55%,#b98a2e);
                     box-shadow:0 6px 16px rgba(232,182,76,.35),inset 0 2px 3px rgba(255,255,255,.5)">
                <svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24"><path d="M8 5v14l11-7z"/></svg>
              </button>
              <div style="flex:1;min-width:0">
                <div class="at40-status" style="font-weight:600;font-size:13px">Tap to tune in</div>
                <input class="at40-vol" type="range" min="0" max="1" step="0.01" value="0.9"
                       style="width:100%;margin-top:6px;accent-color:#e8b64c" aria-label="Volume">
              </div>
            </div>
          </div>`;

        this.au = new Audio();
        this.au.preload = 'none';
        this.au.src = AT40Widget.STREAM;
        this.btn    = this.el.querySelector('.at40-play');
        this.icon   = this.btn.querySelector('path');
        this.status = this.el.querySelector('.at40-status');
        this.cv     = this.el.querySelector('.at40-viz');
        this.ctx    = this.cv.getContext('2d');
        this.dotEl  = this.el.querySelector('.at40-live .dot');
        const vol   = this.el.querySelector('.at40-vol');

        this.au.volume = parseFloat(vol.value);
        vol.oninput = () => { this.au.volume = parseFloat(vol.value); };

        this.btn.onclick = async () => {
            if (this.playing) { this.au.pause(); return; }
            this.status.textContent = 'Tuning in…';
            try { this.au.load(); await this.au.play(); }
            catch { this.status.textContent = 'Tap again to allow audio'; }
        };
        this.au.onplaying = () => this._setPlaying(true);
        this.au.onpause   = () => this._setPlaying(false);
        this.au.onwaiting = () => { this.status.textContent = 'Buffering…'; };
        this.au.onerror   = () => {
            this.status.textContent = 'Stream error — retrying';
            setTimeout(() => { if (this.playing) { this.au.load(); this.au.play().catch(() => {}); } }, 2500);
        };

        this._loop = (t) => this._draw(t);
        this._raf  = requestAnimationFrame(this._loop);
    }

    _setPlaying(p) {
        this.playing = p;
        this.icon.setAttribute('d', p ? 'M6 5h4v14H6zM14 5h4v14h-4z' : 'M8 5v14l11-7z');
        this.btn.setAttribute('aria-label', p ? 'Pause' : 'Play');
        this.status.textContent = p ? 'On air — Casey Kasem' : 'Paused';
        this.dotEl.style.boxShadow = p ? '0 0 8px #ff5d5d' : 'none';
        if (p && 'mediaSession' in navigator) {
            navigator.mediaSession.metadata = new MediaMetadata(
                { title: 'Classic American Top 40', artist: 'Casey Kasem', album: 'iHeartRadio' });
        }
    }

    _draw(t) {
        const cv = this.cv, ctx = this.ctx;
        const d = Math.min(2, window.devicePixelRatio || 1);
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
            ctx.fillStyle = g;
            ctx.fillRect(x, y, bw - 3 * d, bh);
        }
        this._raf = requestAnimationFrame(this._loop);
    }

    destroy() {
        super.destroy();
        if (this._raf) cancelAnimationFrame(this._raf);
        if (this.au) { try { this.au.pause(); } catch {} this.au.src = ''; this.au = null; }
    }
}
registerWidget(AT40Widget);
