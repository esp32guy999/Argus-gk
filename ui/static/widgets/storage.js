class StorageWidget extends WidgetBase {
    static type     = 'storage';
    static label    = 'Storage';
    static icon     = '💾';
    static defaultW = 300;
    static defaultH = 220;

    init() {
        this.render('<div style="color:var(--secondary);font-size:12px">Loading…</div>');
        this.poll(() => this._refresh(), 30000);
    }

    async _refresh() {
        try {
            const data = await fetch('/storage').then(r => r.json());
            this.render(this._buildHTML(data));
        } catch (e) {
            this.render(`<div style="color:#FF3B30;font-size:12px">Error: ${e.message}</div>`);
        }
    }

    _fmt(bytes) {
        if (bytes === undefined || bytes === null) return '—';
        const tb = bytes / 1e12;
        if (tb >= 1) return tb.toFixed(1) + ' TB';
        return (bytes / 1e9).toFixed(0) + ' GB';
    }

    _bar(used, total, color) {
        const pct = total ? Math.min(100, Math.round(used / total * 100)) : 0;
        const c = pct > 90 ? '#FF3B30' : pct > 75 ? '#FF9500' : color;
        return `
<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--secondary);margin-bottom:2px">
    <span>${this._fmt(used)} used</span>
    <span>${this._fmt(total - used)} free · ${pct}%</span>
</div>
<div style="background:var(--bg3);border-radius:4px;height:6px;overflow:hidden;margin-bottom:8px">
    <div style="width:${pct}%;height:100%;background:${c};border-radius:4px;transition:width 0.5s"></div>
</div>`;
    }

    _buildHTML(data) {
        const local = data.local || {};
        const unraid = data.unraid || {};

        let html = '<div style="display:flex;flex-direction:column;gap:4px;font-size:12px">';

        // Local machine
        html += '<div style="color:var(--secondary);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px">Local</div>';
        if (local.error) {
            html += `<div style="color:#FF3B30">${local.error}</div>`;
        } else {
            html += this._bar(local.used, local.total, '#007AFF');
        }

        // Unraid
        html += '<div style="color:var(--secondary);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-top:4px;margin-bottom:2px">Unraid</div>';
        if (unraid.error) {
            html += `<div style="color:#FF3B30">${unraid.error}</div>`;
        } else {
            const user = unraid['/mnt/user'];
            const d1   = unraid['/mnt/disk1'];
            const d2   = unraid['/mnt/disk2'];
            if (user) {
                html += `<div style="color:var(--fg);margin-bottom:1px">Array</div>`;
                html += this._bar(user.used, user.total, '#34C759');
            }
            if (d1) {
                html += `<div style="color:var(--secondary);font-size:11px;margin-bottom:1px">Disk 1</div>`;
                html += this._bar(d1.used, d1.total, '#FF9500');
            }
            if (d2) {
                html += `<div style="color:var(--secondary);font-size:11px;margin-bottom:1px">Disk 2</div>`;
                html += this._bar(d2.used, d2.total, '#34C759');
            }
            if (!user && !d1 && !d2) {
                html += '<div style="color:var(--secondary)">No data</div>';
            }
        }

        html += '</div>';
        return html;
    }
}
registerWidget(StorageWidget);
