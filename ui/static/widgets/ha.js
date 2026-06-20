class HAWidget extends WidgetBase {
    static type     = 'ha';
    static label    = 'Home Assistant';
    static icon     = '🏠';
    static defaultW = 320;
    static defaultH = 280;

    init() {
        this.render('<div style="color:var(--secondary);font-size:12px">Loading HA…</div>');
        this.poll(() => this._refresh(), 30000);
    }

    async _refresh() {
        try {
            const entities = [
                'switch.workshop_light',
                'switch.porch_lights',
                'light.p1s_01p00a3c2300626_chamber_light',
                'sensor.p1s_01p00a3c2300626_print_progress',
                'sensor.p1s_01p00a3c2300626_nozzle_temperature',
            ];
            const states = await Promise.all(
                entities.map(e => fetch(`/ha/states/${e}`).then(r => r.ok ? r.json() : null).catch(() => null))
            );
            const map = {};
            entities.forEach((e, i) => { map[e] = states[i]; });
            this.render(this._buildHTML(map));
            this._bindToggles();
        } catch (e) {
            this.render(`<div style="color:#FF3B30;font-size:12px">HA error: ${e.message}</div>`);
        }
    }

    _buildHTML(map) {
        const sw = (id, label) => {
            const s = map[id];
            const on = s?.state === 'on';
            return `<div class="ha-row">
                <span class="ha-label">${label}</span>
                <button class="ha-toggle ${on ? 'on' : ''}" data-entity="${id}" data-state="${s?.state}">
                    ${on ? 'ON' : 'OFF'}
                </button>
            </div>`;
        };

        const progress = map['sensor.p1s_01p00a3c2300626_print_progress']?.state ?? '—';
        const nozzle   = map['sensor.p1s_01p00a3c2300626_nozzle_temperature']?.state ?? '—';

        return `
<style>
.ha-row{display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:0.5px solid var(--separator)}
.ha-row:last-child{border-bottom:none}
.ha-label{font-size:13px;color:var(--text)}
.ha-toggle{background:var(--bg3);border:none;border-radius:12px;padding:4px 12px;font-size:12px;font-weight:600;color:var(--secondary);cursor:pointer}
.ha-toggle.on{background:#34C759;color:#fff}
.ha-section{font-size:11px;font-weight:600;color:var(--secondary);text-transform:uppercase;margin:8px 0 2px}
.ha-stat{font-size:13px;color:var(--text)}
</style>
<div style="font-size:13px">
    <div class="ha-section">Lights</div>
    ${sw('switch.workshop_light', '🔧 Workshop')}
    ${sw('switch.porch_lights', '🏠 Porch')}
    <div class="ha-section">Bambu P1S</div>
    <div class="ha-row">
        <span class="ha-label">Progress</span>
        <span class="ha-stat">${progress}%</span>
    </div>
    <div class="ha-row">
        <span class="ha-label">Nozzle</span>
        <span class="ha-stat">${nozzle}°C</span>
    </div>
</div>`;
    }

    _bindToggles() {
        this.el.querySelectorAll('.ha-toggle').forEach(btn => {
            btn.onclick = async () => {
                const entity = btn.dataset.entity;
                const on     = btn.dataset.state === 'on';
                const svc    = entity.split('.')[0];
                const action = on ? 'turn_off' : 'turn_on';
                try {
                    await fetch(`/ha/services/${svc}/${action}`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({entity_id: entity}),
                    });
                    setTimeout(() => this._refresh(), 800);
                } catch (e) {
                    console.error('HA toggle failed:', e);
                }
            };
        });
    }
}
registerWidget(HAWidget);
