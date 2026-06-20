// ── Widget Registry ───────────────────────────────────────────────────────────
const WIDGET_REGISTRY = {};

function registerWidget(WidgetClass) {
    WIDGET_REGISTRY[WidgetClass.type] = WidgetClass;
}

class WidgetBase {
    static type     = 'base';
    static label    = 'Widget';
    static icon     = '▪';
    static defaultW = 300;
    static defaultH = 200;

    constructor(panel, containerEl) {
        this.panel = panel;
        this.el    = containerEl;
        this._intervals = [];
    }

    init() {}

    destroy() {
        this._intervals.forEach(clearInterval);
    }

    poll(fn, ms) {
        fn();
        this._intervals.push(setInterval(fn, ms));
    }

    render(html) {
        this.el.innerHTML = html;
    }
}
