class DownloadsWidget extends WidgetBase {
    static type     = 'torrents';
    static label    = 'New Downloads';
    static icon     = '⬇️';
    static defaultW = 420;
    static defaultH = 340;

    init() {
        this.render('<div style="color:var(--secondary);font-size:12px">Loading…</div>');
        this.poll(() => this._refresh(), 10000);
    }

    async _refresh() {
        const [qbt, sab] = await Promise.all([
            this._fetchQbt().catch(() => null),
            this._fetchSab().catch(() => null),
        ]);
        this.render(this._buildHTML(qbt, sab));
    }

    async _fetchQbt() {
        const r = await fetch('/qbt/torrents/info');
        if (!r.ok) return null;
        return r.json();
    }

    async _fetchSab() {
        const [hist, queue] = await Promise.all([
            fetch('/sab/history').then(r => r.ok ? r.json() : null).catch(() => null),
            fetch('/sab/queue').then(r => r.ok ? r.json() : null).catch(() => null),
        ]);
        return { history: hist, queue: queue };
    }

    _buildHTML(qbt, sab) {
        let sections = '';
        const activeRows = [];

        if (qbt) {
            const active = qbt.filter(t =>
                t.state === 'downloading' || t.state === 'metaDL' || t.state === 'allocating' ||
                t.state === 'stalledDL' || t.state === 'queuedDL'
            );
            active.forEach(t => {
                const pct = Math.round(t.progress * 100);
                const color = t.state === 'downloading' ? '#007AFF' : '#FF9500';
                activeRows.push(this._dlRow(t.name, pct, color, this._speed(t.dlspeed), this._size(t.size), 'qBt'));
            });
        }

        if (sab && sab.queue && sab.queue.queue && sab.queue.queue.slots) {
            sab.queue.queue.slots.forEach(s => {
                const pct = Math.round(parseFloat(s.percentage) || 0);
                activeRows.push(this._dlRow(s.filename, pct, '#AF52DE', s.speed || '', s.size, 'SAB'));
            });
        }

        if (activeRows.length) {
            sections += this._section('Active', activeRows.join(''));
        }

        const completed = [];

        if (qbt) {
            qbt.filter(t => t.progress >= 1 && (t.state === 'seeding' || t.state === 'uploading' || t.state === 'stalledUP' || t.state === 'pausedUP'))
                .forEach(t => {
                    completed.push({
                        name: t.name,
                        size: this._size(t.size),
                        time: t.completion_on || t.added_on || 0,
                        source: 'qBt',
                        color: '#34C759',
                    });
                });
        }

        if (sab && sab.history && sab.history.history && sab.history.history.slots) {
            sab.history.history.slots.forEach(s => {
                if (s.status === 'Completed') {
                    completed.push({
                        name: s.name,
                        size: s.size || '',
                        time: s.completed || 0,
                        source: 'SAB',
                        color: '#AF52DE',
                    });
                }
            });
        }

        completed.sort((a, b) => b.time - a.time);
        const recent = completed.slice(0, 15);

        if (recent.length) {
            const rows = recent.map(c => {
                const ago = this._ago(c.time);
                return '<div style="padding:5px 8px;border-bottom:1px solid var(--bg3);display:flex;justify-content:space-between;align-items:center;gap:6px">'
                    + '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1" title="' + this._esc(c.name) + '">' + this._esc(this._trunc(c.name, 40)) + '</span>'
                    + '<span style="flex-shrink:0;font-size:10px;color:var(--secondary)">' + c.size + '</span>'
                    + '<span style="flex-shrink:0;padding:1px 5px;border-radius:3px;font-size:10px;background:' + c.color + '22;color:' + c.color + '">' + c.source + '</span>'
                    + '<span style="flex-shrink:0;font-size:10px;color:var(--secondary)">' + ago + '</span>'
                    + '</div>';
            }).join('');
            sections += this._section('Recent', rows);
        }

        if (!sections) {
            return '<div style="color:var(--secondary);font-size:12px;padding:12px">No downloads</div>';
        }

        const totalActive = activeRows.length;
        const totalDl = qbt ? qbt.reduce((s, t) => s + t.dlspeed, 0) : 0;
        const speedStr = totalDl > 0 ? '<span style="color:#007AFF">\u2193 ' + this._speed(totalDl) + '</span>' : '';
        const summary = '<div style="display:flex;justify-content:space-between;padding:4px 8px 6px;color:var(--secondary);font-size:11px;border-bottom:1px solid var(--bg3);flex-shrink:0">'
            + '<span>' + totalActive + ' active</span>'
            + '<span>' + completed.length + ' completed</span>'
            + speedStr
            + '</div>';

        return '<div style="display:flex;flex-direction:column;height:100%;font-size:12px">'
            + summary
            + '<div style="overflow-y:auto;flex:1">' + sections + '</div>'
            + '</div>';
    }

    _section(title, content) {
        return '<div style="font-size:10px;font-weight:600;color:var(--secondary);text-transform:uppercase;padding:8px 8px 2px;letter-spacing:0.5px">' + title + '</div>' + content;
    }

    _dlRow(name, pct, color, speed, size, source) {
        return '<div style="padding:5px 8px;border-bottom:1px solid var(--bg3)">'
            + '<div style="display:flex;justify-content:space-between;margin-bottom:3px;gap:6px">'
            + '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1" title="' + this._esc(name) + '">' + this._esc(this._trunc(name, 38)) + '</span>'
            + '<span style="flex-shrink:0;padding:1px 5px;border-radius:3px;font-size:10px;background:' + color + '22;color:' + color + '">' + source + '</span>'
            + '</div>'
            + '<div style="background:var(--bg3);border-radius:3px;height:4px;overflow:hidden;margin-bottom:3px">'
            + '<div style="width:' + pct + '%;height:100%;background:' + color + ';border-radius:3px;transition:width 1s"></div>'
            + '</div>'
            + '<div style="display:flex;justify-content:space-between;color:var(--secondary);font-size:11px">'
            + '<span>' + pct + '% of ' + size + '</span>'
            + '<span style="color:var(--text)">' + speed + '</span>'
            + '</div></div>';
    }

    _ago(ts) {
        if (!ts) return '';
        const sec = Math.floor(Date.now() / 1000 - ts);
        if (sec < 60) return 'just now';
        if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
        if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
        return Math.floor(sec / 86400) + 'd ago';
    }

    _trunc(s, n) { return s.length > n ? s.slice(0, n - 1) + '\u2026' : s; }
    _esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

    _speed(bps) {
        if (bps < 1024) return '0 KB/s';
        if (bps < 1024 * 1024) return (bps / 1024).toFixed(0) + ' KB/s';
        return (bps / 1024 / 1024).toFixed(1) + ' MB/s';
    }

    _size(bytes) {
        if (typeof bytes === 'string') return bytes;
        if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(0) + ' MB';
        return (bytes / 1024 / 1024 / 1024).toFixed(1) + ' GB';
    }
}
registerWidget(DownloadsWidget);
