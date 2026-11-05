// Windline: connected instrument panels; only unused context capacity carries motion.
const TOKENS = Object.freeze({
    night: "#161d30", brand: "#8475cc", identity: "#35577f", workflow: "#29384e",
    ink: "#dae7f3", cyan: "#67cbd0", warning: "#d5ab68", error: "#d67d87",
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
            ? blend(TOKENS.night, TOKENS.cyan, 0.08 + 0.26 * (1 + Math.cos(2 * Math.PI * ((index - occupied) / Math.max(1, remaining) - phase / 32))) / 2)
            : TOKENS.night,
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

function ansi256(hex) {
    const channels = rgb(hex);
    const cube = channels.map(value => Math.round(value / 255 * 5));
    const levels = [0, 95, 135, 175, 215, 255];
    const cubeError = channels.reduce((sum, value, index) => sum + (value - levels[cube[index]]) ** 2, 0);
    const gray = Math.max(0, Math.min(23, Math.round((channels.reduce((a, b) => a + b, 0) / 3 - 8) / 10)));
    const grayError = channels.reduce((sum, value) => sum + (value - (8 + gray * 10)) ** 2, 0);
    return grayError < cubeError ? 232 + gray : 16 + cube[0] * 36 + cube[1] * 6 + cube[2];
}

function palette(mode) {
    const code = (hex, background) => mode === "truecolor"
        ? `\x1b[${background ? 48 : 38};2;${rgb(hex).join(";")}m`
        : `\x1b[${background ? 48 : 38};5;${ansi256(hex)}m`;
    return (text, background = TOKENS.night, foreground = TOKENS.ink, bold = false) => {
        if (!text || mode === "none") return text;
        return `${code(background, true)}${code(foreground, false)}${bold ? "\x1b[1m" : "\x1b[22m"}${text}\x1b[0m`;
    };
}

export function createStatusLine(getPavewayState, ui) {
    const measure = ui.visibleWidth;
    // OMP's empty third argument requests cell-aware clipping without an ellipsis.
    const clip = (text, width) => width <= 0 ? "" : sanitizeStatusText(ui.truncateToWidth(text, width, ""));
    let disposed = false;
    return {
        dispose() { disposed = true; },
        render(requestedWidth, snapshot = {}) {
            const width = Number.isFinite(requestedWidth) ? Math.max(0, Math.floor(requestedWidth)) : 0;
            if (disposed || width === 0) return "";
            const state = getPavewayState() ?? {};
            const context = snapshot.context ?? {};
            const paint = palette(colorMode(ui));
            const model = sanitizeStatusText(snapshot.model?.id) || sanitizeStatusText(snapshot.model?.name) || "model ?";
            const thinking = sanitizeStatusText(snapshot.thinking);
            const coach = sanitizeStatusText(state.coach) || "coach ?";
            const workflow = sanitizeStatusText(state.workflow) || "workflow ?";
            const percent = percentText(context.percent);
            const window = shortNumber(context.window);
            const inboxKnown = Number.isFinite(state.inbox) && state.inbox >= 0;
            const inbox = inboxKnown ? shortNumber(Math.floor(state.inbox)) : "?";
            const wakeup = state.wakeup;
            const wake = wakeup?.unavailable ? "!" : wakeup === undefined ? "?"
                : wakeup.running ? "running" : sanitizeStatusText(wakeup.timing) || "none";
            const cost = Number.isFinite(snapshot.cost) && snapshot.cost >= 0
                ? `$${snapshot.cost < 10 ? snapshot.cost.toFixed(2) : snapshot.cost < 1000 ? snapshot.cost.toFixed(1) : shortNumber(snapshot.cost)}` : "";
            const finish = line => {
                const fitted = measure(line) > width ? ui.truncateToWidth(line, width, "") : line;
                const remaining = Math.max(0, width - measure(fitted));
                return fitted + paint(" ".repeat(remaining));
            };
            const inboxColor = inboxKnown && state.inbox > 0 ? TOKENS.cyan : TOKENS.ink;
            if (width < 72) {
                // Reserve abnormalities and unread counts before spending cells on identity.
                const candidates = [
                    ...(wakeup?.unavailable ? [{text: "wake!", color: TOKENS.error, tiny: "!"}] : []),
                    ...(inboxKnown && state.inbox > 0 ? [{text: `in ${inbox}`, color: inboxColor, tiny: `i${inbox}`}] : []),
                    {text: percent, color: TOKENS.ink, tiny: percent},
                ];
                const right = [];
                let used = 0;
                for (const item of candidates) {
                    const budget = width - used - (right.length ? 1 : 0);
                    const text = measure(item.text) <= budget ? item.text : measure(item.tiny) <= budget ? item.tiny : "";
                    if (text) { right.push({...item, text}); used += measure(text) + (right.length > 1 ? 1 : 0); }
                }
                if (width >= 40) {
                    const extra = `/${window}`;
                    const item = right.find(item => item.text === percent);
                    if (item && used + measure(extra) <= width - 12) { item.text += extra; used += measure(extra); }
                }
                for (const text of [width >= 48 && !wakeup?.unavailable ? `wake ${clip(wake, 10)}` : "", width >= 56 ? cost : ""]) {
                    if (text && used + measure(text) + 1 <= width - 16) {
                        right.unshift({text, color: TOKENS.ink}); used += measure(text) + 1;
                    }
                }
                const available = Math.max(0, width - used - (used < width ? 1 : 0));
                const identity = clip(`${width >= 56 ? "PAVEWAY " : ""}${model}${thinking ? ` · ${thinking}` : ""}`, available);
                const gap = Math.max(0, width - measure(identity) - used);
                return finish(paint(identity) + paint(" ".repeat(gap)) + right.map(item => paint(item.text, TOKENS.night, item.color)).join(paint(" ")));
            }

            const fields = [
                {text: "PAVEWAY", background: TOKENS.brand, bold: true, min: 7},
                {text: coach, background: TOKENS.identity, min: 3},
                {text: `${model}${thinking ? ` · ${thinking}` : ""}`, background: TOKENS.identity, min: 8},
                {text: workflow, background: TOKENS.workflow, min: 3},
            ];
            const minimumLeft = fields.reduce((sum, field) => sum + field.min + 2, 0);
            const contextLabel = ` ${percent}/${window} `;
            const metadata = [
                {text: `wake ${clip(wake, 12)}`, color: wakeup?.unavailable ? TOKENS.error : TOKENS.ink},
                {text: `${width >= 120 ? "Inbox" : "in"} ${inbox}`, color: inboxColor},
                ...(cost ? [{text: cost, color: TOKENS.ink}] : []),
            ];
            const metadataSize = () => metadata.reduce((sum, field) => sum + measure(field.text) + 1, 0);
            const metadataBudget = width - minimumLeft - measure(contextLabel);
            if (metadataSize() > metadataBudget && cost) metadata.pop();
            if (metadataSize() > metadataBudget) {
                metadata[0].text = clip(metadata[0].text, Math.max(0, measure(metadata[0].text) - metadataSize() + metadataBudget));
            }
            const metadataWidth = metadataSize();
            const boundary = context.autoCompact === true && knownPercent(context.thresholdPercent) !== null
                ? `auto${percentText(context.thresholdPercent)}${knownPercent(context.speculationPercent) !== null ? ` (spec${percentText(context.speculationPercent)})` : ""}`
                : context.autoCompact === false ? "auto off" : "";
            const boundaryLabel = width >= 160 && boundary ? ` ${boundary}` : "";
            const meterWidth = Math.max(0, Math.min(16, width - minimumLeft - metadataWidth - measure(contextLabel) - measure(boundaryLabel)));
            const rightWidth = metadataWidth + meterWidth + measure(contextLabel) + measure(boundaryLabel);
            const leftBudget = Math.max(0, width - rightWidth);
            const coachField = fields[1], modelField = fields[2], workflowField = fields[3];
            coachField.min = Math.min(11, measure(coachField.text));
            modelField.min = Math.min(width >= 100 ? 24 : 16, measure(modelField.text));
            workflowField.min = Math.min(16, measure(workflowField.text));
            const allocatedWidth = () => fields.reduce((sum, field) => sum + field.min + 2, 0);
            // Hide secondary identity before it degenerates into unreadable fragments.
            if (allocatedWidth() > leftBudget) fields.pop();
            if (allocatedWidth() > leftBudget) fields.splice(1, 1);
            modelField.min = Math.max(1, modelField.min - Math.max(0, allocatedWidth() - leftBudget));
            const allocations = fields.map(field => field.min);
            let spare = Math.max(0, leftBudget - allocatedWidth());
            for (const field of [modelField, workflowField, coachField]) {
                const index = fields.indexOf(field);
                if (index < 0) continue;
                const desired = Math.min(measure(field.text), field === modelField ? 32 : Infinity);
                const extra = Math.max(0, Math.min(spare, desired - allocations[index]));
                allocations[index] += extra; spare -= extra;
            }
            const nerd = ui.theme?.getSymbolPreset?.() === "nerd";
            const cap = (candidate, fallback) => {
                const text = sanitizeStatusText(candidate);
                return measure(text) === 1 && (nerd || !/[\uE000-\uF8FF]/u.test(text)) ? text : fallback;
            };
            const separators = ui.theme?.sep;
            const opening = cap(separators?.powerlineCapLeft, nerd ? "\ue0b6" : "◖");
            const connection = cap(nerd ? separators?.powerlineLeft : undefined, nerd ? "\ue0b0" : "◗");
            const left = fields.map((field, index) => {
                const text = measure(field.text) > allocations[index]
                    ? clip(field.text, Math.max(0, allocations[index] - 1)) + "…"
                    : field.text;
                const leading = index === 0 ? paint(opening, TOKENS.night, field.background) : paint(" ", field.background);
                const content = text + " ".repeat(Math.max(0, allocations[index] - measure(text)));
                const nextBackground = fields[index + 1]?.background ?? TOKENS.night;
                // The outgoing cap is painted onto the next surface: one connected band.
                return leading + paint(content, field.background, TOKENS.ink, field.bold)
                    + paint(connection, nextBackground, field.background);
            }).join("");
            const cells = contextMeterCells(context.percent, meterWidth, snapshot);
            const markers = new Map();
            if (!boundaryLabel && context.autoCompact === true) {
                for (const [value, glyph] of [[context.speculationPercent, "┊"], [context.thresholdPercent, "│"]]) {
                    const threshold = knownPercent(value);
                    if (threshold !== null) markers.set(Math.min(meterWidth - 1, Math.floor(meterWidth * threshold / 100)), glyph);
                }
            }
            const mode = colorMode(ui);
            const meter = cells.map((cell, index) => paint(
                !cell.occupied && markers.has(index) ? markers.get(index) : mode === "none" ? cell.occupied ? "━" : "─" : " ",
                cell.background, TOKENS.cyan,
            )).join("");
            const threshold = knownPercent(context.thresholdPercent);
            const abnormal = context.autoCompact === true && threshold !== null && knownPercent(context.percent) !== null && context.percent >= threshold;
            const right = metadata.map(field => paint(`${field.text} `, TOKENS.night, field.color)).join("")
                + meter + paint(contextLabel, TOKENS.night, abnormal ? TOKENS.warning : TOKENS.ink)
                + paint(boundaryLabel);
            return finish(left + paint(" ".repeat(Math.max(0, spare))) + right);
        },
    };
}
