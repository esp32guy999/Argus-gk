// Claude Re-login — finish a forced `claude auth login` from your phone.
// Flow: Start → tap the auth link, approve on claude.com, copy the code →
// paste it back → backend completes the token exchange + restarts claude-shim.
class ReloginWidget extends WidgetBase {
    static type     = 'relogin';
    static label    = 'Claude Re-login';
    static icon     = '🔑';
    static defaultW = 340;
    static defaultH = 320;

    init() {
        const btn = 'border:none;border-radius:8px;padding:8px 12px;cursor:pointer;font-size:13px';
        this.el.innerHTML = `
          <div style="display:flex;flex-direction:column;height:100%;font-size:13px;padding:10px;gap:10px;box-sizing:border-box">
            <div class="rl-status" style="color:var(--text-dim);font-size:12px">Checking login…</div>

            <button class="rl-start" style="background:var(--accent);color:#fff;${btn}">Start re-login</button>

            <div class="rl-step2" style="display:none;flex-direction:column;gap:8px">
              <a class="rl-link" target="_blank" rel="noopener"
                 style="background:var(--surface2);border:1px solid var(--border);color:var(--text);
                        ${btn};text-align:center;text-decoration:none;display:block">
                 🔗 Approve Claude Login</a>
              <div style="color:var(--text-dim);font-size:11px">Approve on claude.com, copy the code, paste it below.</div>
              <div style="display:flex;gap:6px">
                <input class="rl-code" placeholder="paste code"
                  style="flex:1;background:var(--surface2);border:1px solid var(--border);
                         color:var(--text);border-radius:8px;padding:8px 10px;outline:none">
                <button class="rl-submit" style="background:var(--accent);color:#fff;${btn}">Submit</button>
              </div>
            </div>

            <div class="rl-msg" style="color:var(--text-dim);font-size:12px;white-space:pre-wrap"></div>
          </div>`;

        this.elStatus = this.el.querySelector('.rl-status');
        this.elStart  = this.el.querySelector('.rl-start');
        this.elStep2  = this.el.querySelector('.rl-step2');
        this.elLink   = this.el.querySelector('.rl-link');
        this.elCode   = this.el.querySelector('.rl-code');
        this.elSubmit = this.el.querySelector('.rl-submit');
        this.elMsg    = this.el.querySelector('.rl-msg');
        this.sid      = null;

        this.elStart.onclick  = () => this._start();
        this.elSubmit.onclick = () => this._submit();
        this.elCode.addEventListener('keydown', e => { if (e.key === 'Enter') this._submit(); });

        this._refreshStatus();
    }

    async _refreshStatus() {
        try {
            const s = await fetch('/relogin/status').then(r => r.json());
            if (s.loggedIn) {
                this.elStatus.innerHTML = `✅ Logged in — ${this._esc(s.email || '')}` +
                    (s.subscriptionType ? ` · ${this._esc(s.subscriptionType)}` : '');
                this.elStatus.style.color = 'var(--text-dim)';
            } else {
                this.elStatus.textContent = '⚠️ Not logged in' + (s.error ? ` (${s.error})` : '');
                this.elStatus.style.color = 'var(--accent)';
            }
        } catch {
            this.elStatus.textContent = 'Could not reach re-login service.';
        }
    }

    async _start() {
        this.elStart.disabled = true;
        this.elMsg.textContent = 'Starting login…';
        try {
            const d = await fetch('/relogin/start', { method: 'POST' })
                .then(r => r.json().then(j => { if (!r.ok) throw new Error(j.detail || r.status); return j; }));
            this.sid = d.session_id;
            this.elLink.href = d.url;
            this.elStep2.style.display = 'flex';
            this.elStart.style.display = 'none';
            this.elMsg.textContent = '';
            this.elCode.focus();
        } catch (e) {
            this.elMsg.textContent = 'Start failed: ' + e.message;
            this.elStart.disabled = false;
        }
    }

    async _submit() {
        const code = this.elCode.value.trim();
        if (!code) return;
        this.elSubmit.disabled = true;
        this.elMsg.textContent = 'Completing login + restarting shim…';
        try {
            const d = await fetch('/relogin/code', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: this.sid, code })
            }).then(r => r.json().then(j => { if (!r.ok) throw new Error(j.detail || r.status); return j; }));

            if (d.loggedIn) {
                const shim = d.shim && d.shim.ok ? 'shim restarted' : 'shim NOT restarted';
                this.elMsg.textContent = `✅ Logged in — ${shim}.`;
                this.elStep2.style.display = 'none';
                this.elStart.style.display = 'block';
                this.elStart.disabled = false;
                this.elCode.value = '';
                this._refreshStatus();
            } else {
                this.elMsg.textContent = 'Login did not complete. Tail:\n' + (d.tail || '(no output)');
                this.elSubmit.disabled = false;
            }
        } catch (e) {
            this.elMsg.textContent = 'Submit failed: ' + e.message;
            this.elSubmit.disabled = false;
        }
    }

    _esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
}
registerWidget(ReloginWidget);
