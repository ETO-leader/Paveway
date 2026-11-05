// Independent Windline Bubble surfaces; motion belongs only to unused context capacity.
const TOKENS = Object.freeze({
    brand: "#8475cc", identity: "#35577f", workflow: "#29384e",
    ink: "#dae7f3", cyan: "#67cbd0", warning: "#d5ab68", error: "#f5dce0",
    context: "#203f49", cost: "#222c40", event: "#30505c", fault: "#633c4b",
});
// Independent visual decision: terminal font coverage is not a UI-symbol preset.
const CAPS = Object.freeze({
    powerline: Object.freeze(["\ue0b6", "\ue0b4"]),
    plain: Object.freeze(["(", ")"]),
});
const ESCAPES = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\|$)|[PX^_][\s\S]*?(?:\x1b\\|$)|[@-_])/g;
const UNSAFE = /[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;

export function sanitizeStatusText(value) {
    return typeof value === "string" ? value.replace(ESCAPES, "").replace(/\s+/gu, " ").replace(UNSAFE, "").trim() : "";
}

function knownPercent(value) {
    return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
}

function rgb(hex) {
    return [1, 3, 5].map(index => Number.parseInt(hex.slice(index, index + 2), 16));
}

function blend(from, to, amount) {
    const a = rgb(from), b = rgb(to);
    return "#" + a.map((value, index) => Math.round(value + (b[index] - value) * amount).toString(16).padStart(2, "0")).join("");
}

// Endpoint-preserving quantization: a nonempty window is visible, but 99% is not full.
export function contextMeterCells(percent, count, snapshot = {}) {
    count = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
    const ratio = knownPercent(percent);
    const occupied = ratio === null || ratio === 0 ? 0 : ratio === 100 ? count
        : Math.min(Math.max(0, count - 1), Math.max(1, Math.floor(count * ratio / 100)));
    const remaining = count - occupied;
    const moving = ratio !== null && snapshot.working === true && snapshot.motion !== false;
    const phase = Number.isFinite(snapshot.phase) ? snapshot.phase : 0;
    return Array.from({length: count}, (_, index) => ({
        occupied: index < occupied,
        background: index < occupied ? TOKENS.cyan : moving
            ? blend(TOKENS.context, TOKENS.cyan, 0.08 + 0.26 * (1 + Math.cos(2 * Math.PI * ((index - occupied) / Math.max(1, remaining) - phase / 32))) / 2)
            : TOKENS.context,
    }));
}

function shortNumber(value) {
    if (!Number.isFinite(value) || value < 0) return "?";
    if (value >= 1e12) return value.toExponential(1).replace(".0e", "e").replace("e+", "e");
    for (const [scale, suffix] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
        if (value >= scale) return `${Number((value / scale).toFixed(1))}${suffix}`;
    }
    return String(Math.round(value));
}

function percentText(value) {
    const percent = knownPercent(value);
    return percent === null ? "?%" : percent > 0 && percent < 1 ? "<1%" : `${percent === 100 ? 100 : Math.min(99, Math.round(percent))}%`;
}

function colorMode(ui) {
    if (process.env.NO_COLOR !== undefined || process.env.TERM === "dumb" || process.env.FORCE_COLOR === "0") return "none";
    const host = ui.theme?.getColorMode?.() ?? ui.colorMode;
    if (host === "none" || host === false) return "none";
    if (host === "truecolor" || host === "256color") return host;
    if (ui.TERMINAL?.trueColor === true || /truecolor|24bit/i.test(process.env.COLORTERM ?? "")) return "truecolor";
    return "256color";
}

const XTERM_LEVELS = Object.freeze([0, 95, 135, 175, 215, 255]);
// The ANSI-256 cube cannot represent Paveway's slate and cyan surfaces: nearest-by-RGB
// would turn identity and Workflow into neutral grays. Pin the semantic tokens instead.
const ANSI_TOKENS = Object.freeze(new Map([
    [TOKENS.brand, 98], [TOKENS.identity, 60], [TOKENS.workflow, 17], [TOKENS.context, 23],
    [TOKENS.cost, 235], [TOKENS.event, 24], [TOKENS.fault, 95], [TOKENS.ink, 189],
    [TOKENS.cyan, 80], [TOKENS.warning, 179], [TOKENS.error, 224],
]));
// The wave blends context into cyan in fixed 256-color steps, never into gray.
const CYAN_RAMP = Object.freeze([23, 24, 30, 31, 37, 38, 44, 45, 51, 80, 116, 152, 159, 195]);
const CUBE = Object.freeze(Array.from({length: 216}, (_, index) => index + 16));
const quantized = new Map();

function xtermRgb(index) {
    if (index >= 232) {
        const level = 8 + (index - 232) * 10;
        return [level, level, level];
    }
    const value = index - 16;
    return [XTERM_LEVELS[Math.floor(value / 36)], XTERM_LEVELS[Math.floor(value % 36 / 6)], XTERM_LEVELS[value % 6]];
}

function nearestIndex(channels, indices) {
    let best = indices[0], error = Infinity;
    for (const index of indices) {
        const level = xtermRgb(index);
        const distance = channels.reduce((sum, value, position) => sum + (value - level[position]) ** 2, 0);
        if (distance < error) { best = index; error = distance; }
    }
    return best;
}

function hue([red, green, blue]) {
    const high = Math.max(red, green, blue), low = Math.min(red, green, blue), delta = high - low;
    if (!delta) return 0;
    const sector = high === red ? (green - blue) / delta
        : high === green ? (blue - red) / delta + 2 : (red - green) / delta + 4;
    return (sector * 60 + 360) % 360;
}

function ansi256(hex) {
    const cached = quantized.get(hex);
    if (cached !== undefined) return cached;
    const channels = rgb(hex);
    const chroma = Math.max(...channels) - Math.min(...channels);
    let index = ANSI_TOKENS.get(hex);
    if (index === undefined) {
        const angle = hue(channels);
        if (chroma > 20 && angle >= 150 && angle <= 210) index = nearestIndex(channels, CYAN_RAMP);
        // Only near-neutral colors may use the gray ramp; tinted colors keep their hue.
        else if (chroma <= 16) index = 232 + Math.max(0, Math.min(23,
            Math.round((channels.reduce((sum, value) => sum + value, 0) / 3 - 8) / 10)));
        else index = nearestIndex(channels, CUBE);
    }
    quantized.set(hex, index);
    return index;
}

function palette(mode) {
    // null is the terminal's own foreground/background, not a guessed night color.
    const code = (value, background) => value === null
        ? `\x1b[${background ? 49 : 39}m`
        : mode === "truecolor"
            ? `\x1b[${background ? 48 : 38};2;${rgb(value).join(";")}m`
            : `\x1b[${background ? 48 : 38};5;${ansi256(value)}m`;
    return (text, background = null, foreground = TOKENS.ink, bold = false) => {
        if (!text || mode === "none") return text;
        return `${code(background, true)}${code(foreground, false)}${bold ? "\x1b[1m" : "\x1b[22m"}${text}\x1b[22m\x1b[39;49m`;
    };
}

// Precedence: explicit environment override, explicit Paveway setting, then the
// symbol-preset default. Only the ASCII preset chooses plain caps; TERM and
// COLORTERM describe ANSI/color capability, not glyph coverage.
function capStyle(ui, configured) {
    for (const value of [process.env.PAVEWAY_STATUSLINE_CAPS, configured]) {
        if (value === "powerline" || value === "plain") return value;
    }
    return ui.theme?.getSymbolPreset?.() === "ascii" ? "plain" : "powerline";
}

export function createStatusLine(getPavewayState, ui, options = {}) {
    const measure = ui.visibleWidth;
    const clip = (text, width) => width <= 0 ? "" : sanitizeStatusText(ui.truncateToWidth(text, width, ""));
    const shorten = (text, width, ascii) => measure(text) <= width ? text
        : clip(text, Math.max(0, width - 1)) + (ascii ? "~" : "…");
    let disposed = false;
    return {
        dispose() { disposed = true; },
        render(requestedWidth, snapshot = {}) {
            const width = Number.isFinite(requestedWidth) ? Math.max(0, Math.floor(requestedWidth)) : 0;
            if (disposed || width === 0) return "";
            const state = getPavewayState() ?? {};
            const context = snapshot.context ?? {};
            const mode = colorMode(ui);
            const paint = palette(mode);
            const preset = ui.theme?.getSymbolPreset?.();
            const ascii = preset === "ascii" || process.env.TERM === "dumb";
            // Ask for Powerline glyphs without claiming whether a font or terminal
            // actually draws them; width is a layout budget, not font detection.
            const caps = CAPS[capStyle(ui, options.capStyle)];
            const [leftCap, rightCap] = caps.every(cap => measure(cap) === 1) ? caps : CAPS.plain;
            const padding = width >= 64 ? 1 : 0;
            const overhead = 2 + padding * 2;
            const gap = paint(" ", null, null);
            const model = sanitizeStatusText(snapshot.model?.name) || sanitizeStatusText(snapshot.model?.id) || "model ?";
            const coach = sanitizeStatusText(state.coach) || "coach ?";
            const thinking = sanitizeStatusText(snapshot.thinking);
            const workflow = sanitizeStatusText(state.workflow) || "workflow ?";
            const percent = percentText(context.percent);
            const window = shortNumber(context.window);
            const separator = ascii ? " / " : " · ";
            const contextLabel = `${percent}${separator}${window}`;
            const boundary = context.autoCompact === true && knownPercent(context.thresholdPercent) !== null
                ? `auto${percentText(context.thresholdPercent)}${knownPercent(context.speculationPercent) !== null ? ` (spec${percentText(context.speculationPercent)})` : ""}`
                : context.autoCompact === false ? "auto off" : "";
            const threshold = knownPercent(context.thresholdPercent);
            const abnormal = context.autoCompact === true && threshold !== null
                && knownPercent(context.percent) !== null && context.percent >= threshold;
            const wakeup = state.wakeup;
            const timing = sanitizeStatusText(wakeup?.timing);
            const wakeFault = wakeup?.unavailable ? "Wakeup !" : wakeup === undefined
                || (wakeup !== null && !wakeup.running && !timing) ? "Wakeup ?" : "";
            const inboxKnown = Number.isFinite(state.inbox) && state.inbox >= 0;
            const unread = inboxKnown && state.inbox > 0;
            const inbox = inboxKnown ? shortNumber(Math.floor(state.inbox)) : "?";
            const cost = Number.isFinite(snapshot.cost) && snapshot.cost >= 0
                ? `$${snapshot.cost < 10 ? snapshot.cost.toFixed(2) : snapshot.cost < 1000 ? snapshot.cost.toFixed(1) : shortNumber(snapshot.cost)}` : "";

            // One primitive for both sides: caps are on the bar, never on another bubble.
            const bubble = (item, content) => paint(leftCap, null, item.background)
                + paint(" ".repeat(padding), item.background, item.foreground)
                + (content ?? paint(item.text, item.background, item.foreground, item.bold))
                + paint(" ".repeat(padding), item.background, item.foreground)
                + paint(rightCap, null, item.background);
            const chosen = [];
            const size = item => measure(item.text) + overhead;
            const used = () => chosen.reduce((sum, item) => sum + size(item), 0) + Math.max(0, chosen.length - 1);
            const add = (id, text, background, side, foreground = TOKENS.ink, bold = false) => {
                const item = {id, text, background, foreground, side, bold};
                if (used() + size(item) + (chosen.length ? 1 : 0) > width) return;
                chosen.push(item);
                return item;
            };
            const fault = text => add(text, text, TOKENS.fault, "right", TOKENS.error, true)
                ?? add(text, text.startsWith("Wakeup") ? `Wake${text.endsWith("!") ? "!" : "?"}` : "In?",
                    TOKENS.fault, "right", TOKENS.error, true)
                ?? add(text, "!", TOKENS.fault, "right", TOKENS.error, true);
            if (wakeFault) fault(wakeFault);
            if (!inboxKnown) fault("Inbox ?");
            if (unread) add("inbox", `${ascii ? "In" : "✉"} ${inbox}`, TOKENS.event, "right", TOKENS.cyan, true);

            // Very small terminals still show a real fault, not a clipped cap.
            if (!chosen.length && (wakeFault || !inboxKnown) && width < measure(wakeFault || "Inbox ?") + overhead) {
                return paint("!" + " ".repeat(width - 1), null, TOKENS.error, true);
            }
            const modelBudget = Math.max(1, Math.min(width >= 80 ? 20 : 14, width - used() - overhead - (chosen.length ? 1 : 0)));
            const identity = add("identity", shorten(model, modelBudget, ascii), TOKENS.identity, "left");
            let meterWidth = width >= 64 ? 4 : 0;
            let capacity = add("context", meterWidth ? " ".repeat(meterWidth + 1) + contextLabel : percent,
                TOKENS.context, "right", abnormal ? TOKENS.warning : TOKENS.cyan);
            if (!capacity) {
                meterWidth = 0;
                capacity = add("context", percent, TOKENS.context, "right", abnormal ? TOKENS.warning : TOKENS.cyan);
            }
            const clock = ascii ? "Wake" : "◷";
            if (!wakeFault && wakeup && (wakeup.running || (timing && timing !== "none"))) {
                add("wake", `${clock} ${wakeup.running ? "Running" : shorten(timing, 12, ascii)}`, TOKENS.event, "right");
            }
            add("brand", "PAVEWAY", TOKENS.brand, "left", TOKENS.ink, true);
            // A narrow layout is composed afresh, not a clipped wide status bar.
            const flow = width >= 100
                ? add("workflow", shorten(workflow, Math.min(16, measure(workflow)), ascii), TOKENS.workflow, "left") : undefined;
            if (cost) add("cost", cost, TOKENS.cost, "right");
            const upgrade = (item, text) => {
                if (item && measure(text) - measure(item.text) <= width - used()) item.text = text;
            };
            // Reserve the compact meter before spending spare cells on identity detail.
            if (capacity) {
                if (!meterWidth) upgrade(capacity, contextLabel);
                for (const count of [4, 8, ...(width >= 160 ? [16] : [])]) {
                    const label = " ".repeat(count + 1) + contextLabel;
                    if (measure(label) - measure(capacity.text) <= width - used()) {
                        capacity.text = label;
                        meterWidth = count;
                    }
                }
            }
            if (identity) {
                const available = measure(identity.text) + width - used();
                upgrade(identity, shorten(model, Math.min(32, available), ascii));
            }
            if (identity) {
                for (const text of [model, `${coach}${separator}${model}`, `${coach}${separator}${model}${thinking ? separator + thinking : ""}`]) {
                    upgrade(identity, text);
                }
            }
            upgrade(flow, workflow);
            let boundaryLabel = "";
            if (capacity && width >= 160 && boundary) {
                const text = capacity.text + " " + boundary;
                if (measure(text) - measure(capacity.text) <= width - used()) {
                    capacity.text = text;
                    boundaryLabel = " " + boundary;
                }
            }
            const renderCapacity = () => {
                const cells = contextMeterCells(context.percent, meterWidth, snapshot);
                const markers = new Map();
                if (!boundaryLabel && context.autoCompact === true) {
                    for (const [value, glyph] of [[context.speculationPercent, ascii ? ":" : "┊"], [context.thresholdPercent, ascii ? "|" : "│"]]) {
                        const limit = knownPercent(value);
                        if (limit !== null) markers.set(Math.min(meterWidth - 1, Math.floor(meterWidth * limit / 100)), glyph);
                    }
                }
                const meter = cells.map((cell, index) => paint(
                    !cell.occupied && markers.has(index) ? markers.get(index)
                        : mode === "none" ? cell.occupied ? ascii ? "=" : "━" : ascii ? "-" : "─" : " ",
                    cell.background, TOKENS.cyan,
                )).join("");
                return bubble(capacity, meter
                    + paint(meterWidth ? " " : "", capacity.background, capacity.foreground)
                    + paint(capacity.text.trim(), capacity.background, capacity.foreground));
            };
            const order = ["brand", "identity", "workflow", "Wakeup !", "Wakeup ?", "Inbox ?", "wake", "inbox", "cost", "context"];
            chosen.sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
            const group = side => chosen.filter(item => item.side === side)
                .map(item => item === capacity ? renderCapacity() : bubble(item)).join(gap);
            const left = group("left"), right = group("right");
            const space = Math.max(0, width - measure(left) - measure(right));
            return left + paint(" ".repeat(space), null, null) + right;
        },
    };
}
