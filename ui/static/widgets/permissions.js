// ── Model Permissions widget ──────────────────────────────────────────────────
// Per-model, per-TOOL toggles. Auto-lists EVERY registry tool + EVERY served model
// from /argus/tool-grants (config/tool_grants.json, hot-reloaded by loop.py — no
// restart). A tool with no explicit toggle follows its lane default; Loki's gated-lane
// floor still applies at run time. New tools/models appear here automatically.
const PERM_DANGER = new Set(['shell', 'code_edit', 'fs', 'run_code']);

class PermissionsWidget extends WidgetBase {
    static type     = 'permissions';
    static label    = 'Model Permissions';
    static icon     = '🔐';
    static defaultW = 420;
    static defaultH = 460;

    init() {
        this.el.addEventListener('click', (e) => this._click(e));
        this.el.addEventListener('change', (e) => this._change(e));
        this.render('<div style="color:var(--secondary);font-size:12px">Loading…</div>');
        this._load();
    }

    async _load() {
        try {
            this._d = await fetch('/argus/tool-grants').then(r => r.json());
            this._model = this._d.models[0]?.id || null;
            this.render(this._html());
        } catch (e) {
            this.render(`<div style="color:#FF3B30;font-size:12px">Error: ${e.message}</div>`);
        }
    }

    _html() {
        const { models, tools } = this._d;
        if (!models.length)
            return '<div style="color:var(--secondary);font-size:12px">No local models loaded.</div>';
        const eff = this._d.effective[this._model] || {};
        const opts = models.map(m =>
            `<option value="${m.id}" ${m.id === this._model ? 'selected' : ''}>${m.display}</option>`).join('');
        // group tools by provider (lane)
        const byLane = {};
        tools.forEach(t => (byLane[t.provider] ||= []).push(t));
        const sections = Object.keys(byLane).sort().map(lane => {
            const rows = byLane[lane].map(t => {
                const on = eff[t.name] !== false;
                return `<tr><td style="padding:4px 6px" title="${(t.description||'').replace(/"/g,'')}">${t.name}</td>`
                    + `<td style="text-align:right;padding:4px 6px">${this._switch(t.name, on)}</td></tr>`;
            }).join('');
            const danger = PERM_DANGER.has(lane) ? ' ⚠' : '';
            return `<tr><td colspan="2" style="padding:8px 6px 2px;color:var(--secondary);`
                + `font-size:11px;font-weight:600;text-transform:uppercase">${lane}${danger}</td></tr>${rows}`;
        }).join('');
        return `
<div style="font-size:12px;display:flex;flex-direction:column;gap:6px;height:100%;overflow:auto">
  <div style="display:flex;align-items:center;gap:6px">
    <span style="color:var(--secondary)">Model:</span>
    <select id="perm-model" style="flex:1;padding:4px;background:var(--bg2);color:var(--text);border:1px solid var(--bg3);border-radius:6px">${opts}</select>
  </div>
  <div style="color:var(--secondary);font-size:11px;line-height:1.3">
    Toggle any tool for this model. ⚠ lanes (shell/code_edit/fs/run_code) give near-full reach.
  </div>
  <table style="border-collapse:collapse;width:100%">${sections}</table>
  <div id="perm-status" style="color:var(--secondary);font-size:11px;min-height:14px"></div>
</div>`;
    }

    _switch(tool, on) {
        return `<span class="perm-cell" data-tool="${tool}" `
            + `style="cursor:pointer;display:inline-block;width:34px;height:20px;border-radius:11px;`
            + `background:${on ? '#34C759' : 'var(--bg3)'};position:relative;transition:background .2s;vertical-align:middle">`
            + `<span style="position:absolute;top:2px;left:${on ? '16px' : '2px'};width:16px;height:16px;`
            + `border-radius:50%;background:#fff;transition:left .2s"></span></span>`;
    }

    _change(e) {
        if (e.target.id === 'perm-model') { this._model = e.target.value; this.render(this._html()); }
    }

    _click(e) {
        const cell = e.target.closest('.perm-cell');
        if (!cell) return;
        const tool = cell.dataset.tool;
        const eff = this._d.effective[this._model] || (this._d.effective[this._model] = {});
        const on = eff[tool] !== false;
        const prov = (this._d.tools.find(t => t.name === tool) || {}).provider;
        if (!on && PERM_DANGER.has(prov)
            && !confirm(`Enable ${tool} (${prov} — near-full reach) for ${this._model}?`)) return;
        eff[tool] = !on;
        const ov = this._d.overrides[this._model] || (this._d.overrides[this._model] = {});
        ov[tool] = !on;
        this._save();
    }

    async _save() {
        this.render(this._html());
        const s = this.el.querySelector('#perm-status');
        if (s) s.textContent = 'Saving…';
        try {
            await fetch('/argus/tool-grants', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ grants: this._d.overrides }),
            }).then(r => r.json());
            if (s) s.textContent = 'Saved ✓ — live, no restart';
        } catch (e) {
            if (s) s.textContent = 'Save failed: ' + e.message;
        }
    }
}
registerWidget(PermissionsWidget);
