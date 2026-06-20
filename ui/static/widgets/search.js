class SearchWidget extends WidgetBase {
    static type     = 'search';
    static label    = 'Search Results';
    static icon     = '🔍';
    static defaultW = 320;
    static defaultH = 300;

    init() {
        this.render('<div style="color:var(--secondary);font-size:12px">Search results appear here.</div>');
        // Register globally so chat can populate it
        window._searchWidget = this;
    }

    showResults(hits) {
        if (!hits.length) {
            this.render('<div style="color:var(--secondary);font-size:12px">No results.</div>');
            return;
        }
        this.render(hits.map(h => {
            const time = new Date(h.timestamp).toLocaleString('en-US',
                {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
            const model = h.model ? ` · ${h.model}` : '';
            const snippet = h.content.length > 150 ? h.content.slice(0, 150) + '…' : h.content;
            return `
<div style="padding:8px 0;border-bottom:0.5px solid var(--separator)">
    <div style="font-size:11px;color:var(--secondary);margin-bottom:3px">${h.role}${model} · ${time}</div>
    <div style="font-size:13px;color:var(--text)">${escHtml(snippet)}</div>
</div>`;
        }).join(''));
    }

    destroy() {
        super.destroy();
        if (window._searchWidget === this) window._searchWidget = null;
    }
}
registerWidget(SearchWidget);

function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
