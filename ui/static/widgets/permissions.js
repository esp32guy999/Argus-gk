// ── Model Permissions widget ──────────────────────────────────────────────────
// Toggle the high-blast lanes (shell, code_edit) per model, live. Backed by
// /argus/lane-grants -> config/lane_grants.json (hot-reloaded by loop.py, no
// restart). Hard-denied models (Loki) are excluded server-side and never listed.
class PermissionsWidget extends WidgetBase {
    static type     = 'permissions';
    static label    = 'Model Permissions';
    static icon     = '🔐';
    static defaultW = 380;
    static defaultH = 300;

    init() {
        this.el.addEventListener('click', (e) => this._click(e));
        this.render('<div style="color:var(--secondary);font-size:12px">Loading…</div>');
        this._load();
    }

    async _load() {
        try {
            this._d = await fetch('/argus/lane-grants').then(r => r.json());
            this.render(this._html());
        } catch (e) {
            this.render(`<div style="color:#FF3B30;font-size:12px">Error: ${e.message}</div>`);
        }
    }

    _html() {
        const { lanes, models, grants } = this._d;
        const LABEL = { shell: 'Shell', code_edit: 'Code Edit' };
        if (!models.length)
            return '<div style="color:var(--secondary);font-size:12px">No local models loaded.</div>';
        const head = `<tr><th style="text-align:left;padding:4px 6px">Model</th>`
            + lanes.map(l => `<th style="padding:4px 6px">${LABEL[l] || l}</th>`).join('')
            + `<th style="padding:4px 6px">All</th></tr>`;
        const rows = models.map(m => {
            const g = grants[m.id] || [];
            const cells = lanes.map(l => this._switch(m.id, l, g.includes(l))).join('');
            const full = lanes.every(l => g.includes(l));
            return `<tr><td style="padding:5px 6px;white-space:nowrap">${m.display}</td>${cells}`
                + `<td style="text-align:center"><span class="perm-full" data-model="${m.id}" `
                + `style="cursor:pointer;font-size:11px;color:#007AFF">${full ? 'clear' : 'grant'}</span></td></tr>`;
        }).join('');
        return `
<div style="font-size:12px;display:flex;flex-direction:column;gap:8px;height:100%;overflow:auto">
  <div style="color:var(--secondary);font-size:11px;line-height:1.4">
    ⚠ <b>Shell</b> = any command as shane. <b>Code Edit</b> = read/write source.
    These give a model near-full reach — grant only to models you trust. Loki is
    intentionally excluded.
  </div>
  <table style="border-collapse:collapse;width:100%">${head}${rows}</table>
  <div id="perm-status" style="color:var(--secondary);font-size:11px;min-height:14px"></div>
</div>`;
    }

    _switch(model, lane, on) {
        return `<td style="text-align:center"><span class="perm-cell" data-model="${model}" data-lane="${lane}" `
            + `style="cursor:pointer;display:inline-block;width:34px;height:20px;border-radius:11px;`
            + `background:${on ? '#34C759' : 'var(--bg3)'};position:relative;transition:background .2s;vertical-align:middle">`
            + `<span style="position:absolute;top:2px;left:${on ? '16px' : '2px'};width:16px;height:16px;`
            + `border-radius:50%;background:#fff;transition:left .2s"></span></span></td>`;
    }

    _click(e) {
        const cell = e.target.closest('.perm-cell');
        const full = e.target.closest('.perm-full');
        if (cell) return this._toggle(cell.dataset.model, cell.dataset.lane);
        if (full) return this._toggleFull(full.dataset.model);
    }

    _toggle(model, lane) {
        const g = this._d.grants[model] || (this._d.grants[model] = []);
        const on = g.includes(lane);
        if (!on && lane === 'shell'
            && !confirm(`Grant SHELL (arbitrary commands as shane) to ${model}?`)) return;
        this._d.grants[model] = on ? g.filter(l => l !== lane) : [...g, lane];
        this._save();
    }

    _toggleFull(model) {
        const all = this._d.lanes;
        const g = this._d.grants[model] || [];
        const isFull = all.every(l => g.includes(l));
        if (!isFull && !confirm(`Grant FULL permissions (${all.join(', ')}) to ${model}?`)) return;
        this._d.grants[model] = isFull ? [] : [...all];
        this._save();
    }

    async _save() {
        this.render(this._html());
        const s = this.el.querySelector('#perm-status');
        if (s) s.textContent = 'Saving…';
        try {
            await fetch('/argus/lane-grants', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ grants: this._d.grants }),
            }).then(r => r.json());
            if (s) s.textContent = 'Saved ✓ — live, no restart';
        } catch (e) {
            if (s) s.textContent = 'Save failed: ' + e.message;
        }
    }
}
registerWidget(PermissionsWidget);
