class DocsWidget extends WidgetBase {
    static type     = 'docs';
    static label    = 'Documents';
    static icon     = '📄';
    static defaultW = 280;
    static defaultH = 300;

    init() {
        this.render('<div style="color:var(--secondary);font-size:12px">No docs indexed yet.<br>Drop a PDF, TXT, or MD file here.</div>');
        this._setupDrop();
        this.poll(() => this._refresh(), 30000);
    }

    async _refresh() {
        try {
            const docs = await fetch('/argus/docs').then(r => r.json());
            this._docs = docs;
            this.render(this._buildHTML(docs));
            this._setupDrop();
            this._bindAsk();
        } catch (e) {
            this.render(`<div style="color:var(--secondary);font-size:12px">Docs unavailable</div>`);
        }
    }

    _buildHTML(docs) {
        if (!docs.length) {
            return '<div style="color:var(--secondary);font-size:12px;padding:8px 0">Drop a file to index it.</div>';
        }
        return docs.map(d => `
<div class="doc-row">
    <div>
        <div class="doc-name">${d.filename}</div>
        <div class="doc-meta">${(d.size_bytes/1024).toFixed(0)} KB · ${d.indexed_at?.slice(0,10) ?? ''}</div>
    </div>
    <button class="doc-ask-btn" data-file="${d.filename}">Ask</button>
</div>
<style>
.doc-row{display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:0.5px solid var(--separator)}
.doc-row:last-child{border-bottom:none}
.doc-name{font-size:13px;color:var(--text);word-break:break-all}
.doc-meta{font-size:11px;color:var(--secondary)}
.doc-ask-btn{background:var(--blue);border:none;border-radius:8px;padding:4px 10px;font-size:12px;color:#fff;cursor:pointer;flex-shrink:0}
</style>`).join('');
    }

    _bindAsk() {
        this.el.querySelectorAll('.doc-ask-btn').forEach(btn => {
            btn.onclick = () => {
                const file = btn.dataset.file;
                if (window.app && window.app.prefillChat) {
                    window.app.prefillChat(`@doc:${file} `);
                }
            };
        });
    }

    _setupDrop() {
        this.el.addEventListener('dragover', e => { e.preventDefault(); this.el.style.outline = '2px dashed var(--blue)'; });
        this.el.addEventListener('dragleave', () => { this.el.style.outline = ''; });
        this.el.addEventListener('drop', async e => {
            e.preventDefault();
            this.el.style.outline = '';
            const files = [...e.dataTransfer.files];
            for (const file of files) {
                await this._uploadFile(file);
            }
            this._refresh();
        });
    }

    async _uploadFile(file) {
        const fd = new FormData();
        fd.append('file', file);
        try {
            await fetch('/argus/docs/upload', { method: 'POST', body: fd });
        } catch (e) {
            console.error('Upload failed:', e);
        }
    }
}
registerWidget(DocsWidget);
