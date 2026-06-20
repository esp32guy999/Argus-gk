// ── cc-pair widget ────────────────────────────────────────────────────────────
// Live view of the cc-bridge message bus (two Claude Code instances talking).
// Polls /ccbridge/log?since=<id> for new messages and /ccbridge/peers for the
// registered participants. Lets the human inject messages from the GUI.
class CCPairWidget extends WidgetBase {
    static type     = 'ccpair';
    static label    = 'CC Pair';
    static icon     = '🔗';
    static defaultW = 460;
    static defaultH = 480;

    init() {
        this._sid = localStorage.getItem('ccpair-sid') ||
            ('gui-' + Math.random().toString(36).slice(2, 10));
        localStorage.setItem('ccpair-sid', this._sid);
        this._lastId   = 0;
        this._messages = [];
        this._peers    = [];
        this._renderShell();
        this._registerAlias();
        this.poll(() => this._refresh(), 1500);
    }

    async _registerAlias() {
        try {
            await fetch('/ccbridge/alias', {
                method:  'POST',
                headers: {'Content-Type': 'application/json'},
                body:    JSON.stringify({sid: this._sid, alias: 'gui'}),
            });
        } catch (_) { /* bridge may be down; widget keeps polling */ }
    }

    async _refresh() {
        try {
            const [msgs, peers] = await Promise.all([
                fetch(`/ccbridge/log?since=${this._lastId}&n=200`).then(r => r.json()),
                fetch('/ccbridge/peers').then(r => r.json()),
            ]);
            if (Array.isArray(msgs) && msgs.length) {
                this._messages.push(...msgs);
                this._lastId = msgs[msgs.length - 1].id;
                if (this._messages.length > 500) {
                    this._messages.splice(0, this._messages.length - 500);
                }
            } else if (this._lastId === 0 && Array.isArray(msgs)) {
                this._messages = msgs;
                if (msgs.length) this._lastId = msgs[msgs.length - 1].id;
            }
            this._peers = Array.isArray(peers) ? peers : [];
            this._renderPeers();
            this._renderFeed();
        } catch (e) {
            this._statusBar(`bridge: ${e.message}`, true);
        }
    }

    _renderShell() {
        this.el.innerHTML = `
<div style="display:flex;flex-direction:column;height:100%;gap:8px;padding:8px;box-sizing:border-box;font-size:12px">
  <div data-role="peers" style="display:flex;flex-wrap:wrap;gap:6px"></div>
  <div data-role="feed" style="flex:1;overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.45"></div>
  <div data-role="status" style="font-size:11px;color:var(--text-dim);min-height:14px"></div>
  <div style="display:flex;gap:6px;align-items:stretch">
    <select data-role="to" title="recipient" style="background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 6px"></select>
    <input  data-role="body" placeholder="message…" style="flex:1;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 8px" />
    <button data-role="send" style="background:var(--accent);color:#fff;border:0;border-radius:6px;padding:4px 14px;cursor:pointer;font-weight:600">send</button>
  </div>
</div>`;
        const submit = () => this._send();
        this.el.querySelector('[data-role=send]').addEventListener('click', submit);
        this.el.querySelector('[data-role=body]').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); submit(); }
        });
    }

    _renderPeers() {
        const el = this.el.querySelector('[data-role=peers]');
        if (!el) return;
        const now = Date.now() / 1000;
        if (!this._peers.length) {
            el.innerHTML = '<span style="color:var(--text-dim)">no peers registered yet</span>';
        } else {
            el.innerHTML = this._peers.map(p => {
                const live  = (now - p.last_seen) < 60;
                const dot   = live ? 'var(--green)' : 'var(--text-dim)';
                const label = p.alias || p.sid.slice(0, 8);
                const me    = p.sid === this._sid ? ' (you)' : '';
                return `<span style="display:inline-flex;align-items:center;gap:5px;background:var(--bg3);border:1px solid var(--border);padding:2px 8px;border-radius:999px;font-size:11px">
                    <span style="width:6px;height:6px;background:${dot};border-radius:50%"></span>${label}${me}</span>`;
            }).join('');
        }

        // Populate to-dropdown, preserving selection if still valid.
        const sel  = this.el.querySelector('[data-role=to]');
        const want = sel.value || 'other';
        const opts = ['<option value="other">other</option>'];
        for (const p of this._peers) {
            if (p.sid === this._sid) continue;
            const key   = p.alias || p.sid;
            const label = p.alias || p.sid.slice(0, 8);
            opts.push(`<option value="${this._esc(key)}">${this._esc(label)}</option>`);
        }
        sel.innerHTML = opts.join('');
        sel.value = want;
    }

    _renderFeed() {
        const el = this.el.querySelector('[data-role=feed]');
        if (!el) return;
        const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
        const peerName = (sid) => {
            const p = this._peers.find(p => p.sid === sid);
            return p && p.alias ? p.alias : (sid || '?').slice(0, 8);
        };
        el.innerHTML = this._messages.map(m => {
            const ts        = new Date(m.ts * 1000).toTimeString().slice(0, 8);
            const from      = peerName(m.from);
            const to        = peerName(m.to);
            const fromColor = m.from === this._sid ? 'var(--accent)' : 'var(--blue)';
            return `<div><span style="color:var(--text-dim)">${ts}</span>
                <span style="color:${fromColor}">${this._esc(from)}</span>
                <span style="color:var(--text-dim)">→</span>
                <span style="color:var(--green)">${this._esc(to)}</span>:
                ${this._esc(m.body)}</div>`;
        }).join('') || '<span style="color:var(--text-dim)">empty — no messages yet</span>';
        if (atBottom) el.scrollTop = el.scrollHeight;
    }

    async _send() {
        const to   = this.el.querySelector('[data-role=to]').value;
        const body = this.el.querySelector('[data-role=body]').value.trim();
        if (!body) return;
        try {
            const r = await fetch('/ccbridge/send', {
                method:  'POST',
                headers: {'Content-Type': 'application/json'},
                body:    JSON.stringify({from: this._sid, to, body, alias_from: 'gui'}),
            });
            if (!r.ok) {
                const e = await r.json().catch(() => ({}));
                this._statusBar(`send failed: ${e.error || r.status}`, true);
                return;
            }
            this.el.querySelector('[data-role=body]').value = '';
            this._statusBar('sent', false);
            this._refresh();
        } catch (e) {
            this._statusBar(`send error: ${e.message}`, true);
        }
    }

    _statusBar(msg, err) {
        const el = this.el.querySelector('[data-role=status]');
        if (!el) return;
        el.style.color = err ? 'var(--red)' : 'var(--text-dim)';
        el.textContent = msg;
        if (!err) setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 1500);
    }

    _esc(s) {
        return String(s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }
}
registerWidget(CCPairWidget);
