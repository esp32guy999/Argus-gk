// Weather — current conditions + 5-day forecast (Open-Meteo). Defaults to Dahlonega, GA.
class WeatherWidget extends WidgetBase {
    static type     = 'weather';
    static label    = 'Weather';
    static icon     = '🌤️';
    static defaultW = 320;
    static defaultH = 300;

    init() {
        this.location = (this.panel.config && this.panel.config.location) || 'Dahlonega, GA';
        this.el.innerHTML = `
          <div class="wx-wrap" style="display:flex;flex-direction:column;height:100%;font-size:13px">
            <div style="display:flex;gap:6px;padding:6px">
              <input class="wx-loc" placeholder="City, ST"
                style="flex:1;background:var(--surface2);border:1px solid var(--border);
                       color:var(--text);border-radius:8px;padding:6px 9px;outline:none">
              <button class="wx-go" style="background:var(--accent);border:none;color:#fff;
                       border-radius:8px;padding:6px 12px;cursor:pointer">Go</button>
            </div>
            <div class="wx-body" style="flex:1;overflow:auto;padding:4px 8px 8px"></div>
          </div>`;
        this.body = this.el.querySelector('.wx-body');
        this.loc  = this.el.querySelector('.wx-loc');
        this.loc.value = this.location;
        this.el.querySelector('.wx-go').onclick = () => this._setLocation(this.loc.value);
        this.loc.addEventListener('keydown', e => { if (e.key === 'Enter') this._setLocation(this.loc.value); });
        this.poll(() => this._refresh(), 600000);   // refresh every 10 min
    }

    _setLocation(loc) {
        if (!loc || !loc.trim()) return;
        this.location = loc.trim();
        if (this.panel.config) this.panel.config.location = this.location;  // persist in layout
        if (typeof saveLayout === 'function') try { saveLayout(); } catch {}
        this._refresh();
    }

    async _refresh() {
        this.body.innerHTML = `<div style="color:var(--text-dim);padding:8px">Loading weather…</div>`;
        try {
            const d = await fetch(`/argus/weather/forecast?location=${encodeURIComponent(this.location)}&days=5`)
                .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
            this.body.innerHTML = this._render(d);
        } catch {
            this.body.innerHTML = `<div style="color:var(--text-dim);padding:8px">Couldn't load weather for
                “${this._esc(this.location)}”. Check the name and try again.</div>`;
        }
    }

    _render(d) {
        const c = d.current || {};
        const days = (d.days || []).map(day => {
            const dow = new Date(day.date + 'T00:00').toLocaleDateString(undefined, { weekday: 'short' });
            const rain = (day.precip_pct != null && day.precip_pct > 0)
                ? `<span style="color:var(--accent);font-size:10px">${day.precip_pct}%</span>` : '';
            return `
              <div style="display:flex;align-items:center;gap:8px;padding:4px 2px;border-top:1px solid var(--border)">
                <span style="width:34px;color:var(--text-dim)">${dow}</span>
                <span style="font-size:16px;width:24px;text-align:center">${day.emoji}</span>
                <span style="flex:1;color:var(--text-dim);font-size:11px">${this._esc(day.condition)} ${rain}</span>
                <span style="font-weight:600">${day.high}°</span>
                <span style="color:var(--text-dim);width:24px;text-align:right">${day.low}°</span>
              </div>`;
        }).join('');
        return `
          <div style="display:flex;align-items:center;gap:12px;padding:4px 2px 8px">
            <div style="font-size:42px;line-height:1">${c.emoji || '❔'}</div>
            <div style="flex:1;min-width:0">
              <div style="font-size:13px;color:var(--text-dim)">${this._esc(d.location || this.location)}</div>
              <div style="font-size:30px;font-weight:700;line-height:1.1">${c.temp}°<span style="font-size:14px;color:var(--text-dim)">F</span></div>
              <div style="font-size:11px;color:var(--text-dim)">${this._esc(c.condition || '')} · feels ${c.feels_like}°</div>
            </div>
            <div style="font-size:11px;color:var(--text-dim);text-align:right;line-height:1.6">
              💧 ${c.humidity != null ? c.humidity + '%' : '—'}<br>🌬 ${c.wind_mph} mph
            </div>
          </div>
          ${days}`;
    }

    _esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
}
registerWidget(WeatherWidget);
