class WebWidget extends WidgetBase {
    static type     = 'web';
    static label    = 'Web Frame';
    static icon     = '🌐';
    static defaultW = 400;
    static defaultH = 400;

    init() {
        const url = this.panel.config.url || 'http://127.0.0.1:8095';
        this.el.style.padding = '0';
        this.el.style.overflow = 'hidden';
        this.el.style.display = 'flex';
        this.el.style.flexDirection = 'column';
        this.el.innerHTML = `
<div style="display:flex;gap:4px;padding:6px;background:var(--bg3);border-bottom:0.5px solid var(--separator);flex-shrink:0">
    <input id="url-bar-${this.panel.id}" type="text" value="${url}"
        style="flex:1;background:var(--bg4);border:none;border-radius:6px;padding:4px 8px;font-size:12px;color:var(--text);outline:none"
        onkeydown="if(event.key==='Enter') document.getElementById('iframe-${this.panel.id}').src=this.value">
    <button onclick="document.getElementById('iframe-${this.panel.id}').src=document.getElementById('url-bar-${this.panel.id}').value"
        style="background:var(--blue);border:none;border-radius:6px;padding:4px 8px;font-size:12px;color:#fff;cursor:pointer">Go</button>
</div>
<iframe id="iframe-${this.panel.id}" src="${url}"
    style="flex:1;border:none;width:100%;height:100%"
    sandbox="allow-scripts allow-same-origin allow-forms allow-popups">
</iframe>`;
    }
}
registerWidget(WebWidget);
