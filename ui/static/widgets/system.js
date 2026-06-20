class SystemWidget extends WidgetBase {
    static type     = 'system';
    static label    = 'System Stats';
    static icon     = '📊';
    static defaultW = 300;
    static defaultH = 220;

    init() {
        this._vramHistory = [];
        this._tempHistory = [];
        this.render('<div style="color:var(--secondary);font-size:12px">Loading…</div>');
        this.poll(() => this._refresh(), 5000);
    }

    async _refresh() {
        try {
            const data = await fetch('/argus/status').then(r => r.json());
            const gpu  = data.gpu || {};
            if (gpu.vram_used_mib !== undefined) {
                this._vramHistory.push(gpu.vram_used_mib);
                this._tempHistory.push(gpu.temp_c);
                if (this._vramHistory.length > 60) this._vramHistory.shift();
                if (this._tempHistory.length > 60) this._tempHistory.shift();
            }
            this.render(this._buildHTML(data, gpu));
        } catch (e) {
            this.render(`<div style="color:#FF3B30;font-size:12px">Error: ${e.message}</div>`);
        }
    }

    _buildHTML(data, gpu) {
        const vramPct  = gpu.vram_total_mib ? Math.round(gpu.vram_used_mib / gpu.vram_total_mib * 100) : 0;
        const diskFree = data.disk ? `${(data.disk.free / 1e9).toFixed(0)} GB free` : '';
        return `
<div style="display:flex;flex-direction:column;gap:10px;font-size:12px">
    <div>
        <div style="display:flex;justify-content:space-between;margin-bottom:3px">
            <span style="color:var(--secondary)">VRAM</span>
            <span>${gpu.vram_used_mib ?? '—'}/${gpu.vram_total_mib ?? '—'} MiB (${vramPct}%)</span>
        </div>
        ${this._bar(vramPct, '#007AFF')}
        ${this._sparkline(this._vramHistory, '#007AFF')}
    </div>
    <div>
        <div style="display:flex;justify-content:space-between;margin-bottom:3px">
            <span style="color:var(--secondary)">Temp</span>
            <span>${gpu.temp_c ?? '—'}°C</span>
        </div>
        ${this._bar(gpu.temp_c ?? 0, gpu.temp_c > 80 ? '#FF3B30' : '#34C759', 100)}
        ${this._sparkline(this._tempHistory, '#34C759')}
    </div>
    <div style="display:flex;justify-content:space-between;color:var(--secondary)">
        <span>Util: ${gpu.util_pct ?? '—'}%</span>
        <span>${diskFree}</span>
        <span>${data.message_count ?? '—'} msgs</span>
    </div>
    <div style="color:var(--secondary);font-size:11px">${gpu.name ?? ''}</div>
</div>`;
    }

    _bar(pct, color, max = 100) {
        const w = Math.min(100, Math.round(pct / max * 100));
        return `<div style="background:var(--bg3);border-radius:4px;height:6px;overflow:hidden">
            <div style="width:${w}%;height:100%;background:${color};border-radius:4px;transition:width 0.5s"></div>
        </div>`;
    }

    _sparkline(values, color) {
        if (values.length < 2) return '';
        const w = 200, h = 30;
        const max = Math.max(...values) || 1;
        const pts = values.map((v, i) =>
            `${Math.round(i / (values.length - 1) * w)},${Math.round(h - (v / max) * h)}`
        ).join(' ');
        return `<svg width="100%" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
            <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" opacity="0.7"/>
        </svg>`;
    }
}
registerWidget(SystemWidget);
