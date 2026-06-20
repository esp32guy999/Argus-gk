class GeneratedWidget extends WidgetBase {
    static type     = 'generated';
    static label    = 'AI Widget';
    static icon     = '✨';
    static defaultW = 340;
    static defaultH = 280;

    init() {
        this.el.style.padding = '0';
        this.el.style.overflow = 'hidden';
        this.el.style.display = 'flex';
        this.el.style.flexDirection = 'column';
        this._render();
    }

    _render() {
        const html = this.panel.config.html || '<p style="color:var(--secondary);padding:12px;font-size:13px">No content yet.</p>';
        const prompt = this.panel.config.prompt || '';
        this.el.innerHTML = `
<div style="display:flex;gap:4px;padding:4px 6px;background:var(--bg3);border-bottom:0.5px solid var(--separator);flex-shrink:0">
    <span style="flex:1;font-size:11px;color:var(--secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${prompt}</span>
    <button onclick="window._genWidgets['${this.panel.id}']._regenerate()"
        style="background:none;border:none;font-size:11px;color:var(--blue);cursor:pointer">Regenerate</button>
</div>
<iframe srcdoc="${escAttr(html)}"
    sandbox="allow-scripts"
    style="flex:1;border:none;width:100%;height:100%;background:var(--bg)">
</iframe>`;
        // Register instance for button access
        if (!window._genWidgets) window._genWidgets = {};
        window._genWidgets[this.panel.id] = this;
    }

    async _regenerate() {
        const prompt = this.panel.config.prompt;
        if (!prompt || !window.app) return;
        window.app.prefillChat(`Make a widget: ${prompt}`);
    }

    updateHTML(html, prompt) {
        this.panel.config.html   = html;
        this.panel.config.prompt = prompt;
        this._render();
    }

    destroy() {
        super.destroy();
        if (window._genWidgets) delete window._genWidgets[this.panel.id];
    }
}
registerWidget(GeneratedWidget);

function escAttr(s) {
    return s.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
