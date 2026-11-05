import {afterEach, beforeEach, describe, expect, spyOn, test} from "bun:test";
import {contextMeterCells, createStatusLine, sanitizeStatusText} from "../src/oicoach_runtime/bundled/paveway-statusline.mjs";

const segmenter = new Intl.Segmenter(undefined, {granularity: "grapheme"});
const plain = value => Bun.stripANSI(value);
const visibleWidth = value => Bun.stringWidth(plain(value));
// A small host adapter, not a second layout implementation. Production receives OMP's functions.
function truncateToWidth(value, width) {
    let result = "", cells = 0;
    for (const {segment} of segmenter.segment(plain(value))) {
        const next = visibleWidth(segment);
        if (cells + next > width) break;
        result += segment; cells += next;
    }
    return result;
}
function host(mode = "truecolor", symbols = "unicode") {
    const sep = symbols === "nerd"
        ? {powerlineCapLeft: "\ue0b6", powerlineLeft: "\ue0b0"}
        : {powerlineCapLeft: "", powerlineLeft: "▶"};
    return {visibleWidth, truncateToWidth, theme: {getColorMode: () => mode, getSymbolPreset: () => symbols, sep}};
}
function snapshot(overrides = {}) {
    return {model: {id: "gpt-test", name: "Test model", provider: "openai"}, thinking: "high",
        working: true, motion: true, phase: 0,
        context: {percent: 50, window: 500000, thresholdPercent: 85, speculationPercent: 75, autoCompact: true},
        cost: 0.12, ...overrides};
}
function paveway(overrides = {}) {
    return {coach: "analyst", workflow: "analyst/task", wakeup: {unavailable: false, timing: "1m20s", running: false}, inbox: 3, ...overrides};
}
function renderer(state = paveway(), ui = host()) {
    return createStatusLine(() => state, ui);
}

// Decode terminal background state, so tests assert what a consumer sees, not source/escape spelling.
function terminalCells(line) {
    const cells = [];
    let background = "default", foreground = "default", bold = false;
    for (const token of line.matchAll(/\x1b\[([0-9;]*)m|([^\x1b])/gu)) {
        if (token[1] !== undefined) {
            const codes = token[1].split(";").map(Number);
            for (let index = 0; index < codes.length; index++) {
                const code = codes[index];
                if (code === 0) { background = "default"; foreground = "default"; bold = false; }
                else if (code === 1) bold = true;
                else if (code === 22) bold = false;
                else if ((code === 48 || code === 38) && codes[index + 1] === 2) {
                    const color = "#" + codes.slice(index + 2, index + 5).map(value => value.toString(16).padStart(2, "0")).join("");
                    if (code === 48) background = color; else foreground = color;
                    index += 4;
                } else if ((code === 48 || code === 38) && codes[index + 1] === 5) {
                    if (code === 48) background = `256:${codes[index + 2]}`; else foreground = `256:${codes[index + 2]}`;
                    index += 2;
                }
            }
        } else {
            const text = token[2];
            for (let index = 0; index < visibleWidth(text); index++) cells.push({text, background, foreground, bold});
        }
    }
    return cells;
}
function meterFromLine(line, label = "50% · 500K") {
    const cells = terminalCells(line);
    const labelStart = plain(line).indexOf(label);
    expect(labelStart).toBeGreaterThan(1);
    // Read capacity from the bubble boundary, not a color boundary: idle unused
    // capacity must share the capsule surface rather than cut a hole in it.
    const opening = cells.findLastIndex((cell, index) => index < labelStart && ["◖", "\ue0b6", "("].includes(cell.text));
    expect(opening).toBeGreaterThanOrEqual(0);
    expect(cells[opening + 1].text).toBe(" ");
    return cells.slice(opening + 2, labelStart - 1);
}
function bubblesFromLine(line, opening = "◖", closing = "◗") {
    const cells = terminalCells(line);
    const bubbles = [];
    for (let start = 0; start < cells.length; start++) {
        if (cells[start].text !== opening) continue;
        const end = cells.findIndex((cell, index) => index > start && cell.text === closing);
        expect(end).toBeGreaterThan(start);
        expect(cells.slice(start + 1, end).some(cell => cell.text === opening)).toBe(false);
        bubbles.push({start, end, left: cells[start], right: cells[end], inner: cells.slice(start + 1, end)});
        start = end;
    }
    expect(cells.filter(cell => cell.text === opening)).toHaveLength(bubbles.length);
    expect(cells.filter(cell => cell.text === closing)).toHaveLength(bubbles.length);
    return bubbles;
}
function expectIndependentBubbles(line, {opening = "◖", closing = "◗", night = "#161d30"} = {}) {
    const cells = terminalCells(line);
    const bubbles = bubblesFromLine(line, opening, closing);
    expect(bubbles.length).toBeGreaterThan(0);
    for (const [index, bubble] of bubbles.entries()) {
        expect(bubble.inner.length).toBeGreaterThanOrEqual(2);
        expect(bubble.inner[0].text).toBe(" ");
        expect(bubble.inner.at(-1).text).toBe(" ");
        const surface = bubble.inner[0].background;
        expect(bubble.inner.at(-1).background).toBe(surface);
        expect(bubble.left.background).toBe(night);
        expect(bubble.right.background).toBe(night);
        expect(bubble.left.foreground).toBe(surface);
        expect(bubble.right.foreground).toBe(surface);
        const previous = bubbles[index - 1];
        if (previous) {
            expect(bubble.start - previous.end).toBeGreaterThan(1);
            expect(cells.slice(previous.end + 1, bubble.start).every(cell => cell.text === " " && cell.background === night)).toBe(true);
        }
    }
    expect(cells.slice(bubbles.at(-1).end + 1).every(cell => cell.text === " " && cell.background === night)).toBe(true);
    return bubbles;
}

let environment;
beforeEach(() => {
    environment = Object.fromEntries(["NO_COLOR", "TERM", "COLORTERM", "FORCE_COLOR"].map(key => [key, process.env[key]]));
    delete process.env.NO_COLOR; delete process.env.FORCE_COLOR;
    process.env.TERM = "xterm-256color"; process.env.COLORTERM = "";
});
afterEach(() => {
    for (const [key, value] of Object.entries(environment)) {
        if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
});

describe("real context capacity", () => {
    for (const [percent, occupied] of [[0, 0], [1, 1], [9, 1], [50, 8], [99, 15], [100, 16]]) {
        test(`${percent}% preserves endpoints and occupies ${occupied}/16 cells`, () => {
            const cells = contextMeterCells(percent, 16, snapshot());
            expect(cells.filter(cell => cell.occupied)).toHaveLength(occupied);
            expect(cells.slice(0, occupied).every(cell => cell.background === "#67cbd0")).toBe(true);
            const line = renderer().render(160, snapshot({context: {...snapshot().context, percent}}));
            expect(plain(line)).toContain(`${percent}% · 500K`);
            const renderedMeter = meterFromLine(line, `${percent}% · 500K`);
            expect(renderedMeter).toHaveLength(16);
            expect(renderedMeter.filter(cell => cell.background === "#67cbd0")).toHaveLength(occupied);
        });
    }
    test("unknown, NaN and infinities are unknown, not a fabricated zero", () => {
        for (const percent of [null, undefined, NaN, Infinity, -Infinity]) {
            const context = {...snapshot().context, percent};
            const line = renderer().render(160, snapshot({context}));
            expect(plain(line)).toContain("?% · 500K");
            expect(plain(line)).not.toContain("0% · 500K");
            const first = contextMeterCells(percent, 16, snapshot({phase: 0}));
            const surface = terminalCells(line)[plain(line).indexOf("?% · 500K")].background;
            expect(first.every(cell => !cell.occupied && cell.background === surface)).toBe(true);
            const renderedMeter = meterFromLine(line, "?% · 500K");
            expect(renderedMeter).toHaveLength(16);
            expect(renderedMeter.every(cell => cell.background === surface)).toBe(true);
            expect(contextMeterCells(percent, 16, snapshot({phase: 19}))).toEqual(first);
        }
    });
    test("finite ratios clamp to real endpoints", () => {
        expect(contextMeterCells(-20, 16).filter(cell => cell.occupied)).toHaveLength(0);
        expect(contextMeterCells(120, 16).filter(cell => cell.occupied)).toHaveLength(16);
        expect(contextMeterCells(50, 0)).toEqual([]);
        expect(contextMeterCells(50, -4)).toEqual([]);
    });
    test("compaction boundaries remain readable and never repaint occupied capacity", () => {
        const roomy = renderer().render(160, snapshot());
        expect(plain(roomy)).toContain("auto85% (spec75%)");
        const compact = renderer().render(100, snapshot());
        expect(plain(compact)).toContain("│");
        const markerLine = renderer(paveway({coach: "c".repeat(48)})).render(160, snapshot());
        expect(meterFromLine(markerLine)).toHaveLength(16);
        expect(plain(markerLine)).toContain("┊");
        expect(plain(markerLine)).toContain("│");
        const full = snapshot({context: {...snapshot().context, percent: 100}});
        const fullMeter = meterFromLine(renderer().render(160, full), "100% · 500K");
        expect(fullMeter).toHaveLength(16);
        expect(fullMeter.every(cell => cell.background === "#67cbd0" && cell.text === " ")).toBe(true);
        expect(plain(renderer().render(160, snapshot({context: {...snapshot().context, autoCompact: false}})))).toContain("auto off");
    });
});

describe("motion belongs only to unused capacity", () => {
    test("phase changes colors, never text, width, identity or occupied cells", () => {
        const line = renderer();
        const first = line.render(160, snapshot({phase: 0}));
        const later = line.render(160, snapshot({phase: 8}));
        expect(plain(later)).toBe(plain(first));
        expect(visibleWidth(later)).toBe(160);
        const before = terminalCells(first), after = terminalCells(later);
        const end = plain(first).indexOf("50% · 500K") - 1;
        const start = end - meterFromLine(first).length;
        expect(after.slice(0, start + 8)).toEqual(before.slice(0, start + 8));
        expect(after.slice(end)).toEqual(before.slice(end));
        expect(after.slice(start + 8, end)).not.toEqual(before.slice(start + 8, end));
        expect(line.render(160, snapshot({phase: 0}))).toBe(first);
    });
    test("a smooth traveling crest moves left to right and wraps continuously", () => {
        const crest = phase => {
            const cells = contextMeterCells(0, 16, snapshot({phase}));
            const brightness = cell => Number.parseInt(cell.background.slice(1, 3), 16);
            return cells.findIndex(cell => brightness(cell) === Math.max(...cells.map(brightness)));
        };
        expect(crest(0)).toBe(0);
        expect(crest(4)).toBe(2);
        expect(crest(8)).toBe(4);
        expect(contextMeterCells(0, 16, snapshot({phase: 32}))).toEqual(contextMeterCells(0, 16, snapshot({phase: 0})));
        const a = contextMeterCells(0, 16, snapshot({phase: 1}));
        const b = contextMeterCells(0, 16, snapshot({phase: 2}));
        expect(a).not.toEqual(b);
        expect(a.every((cell, index) => Math.abs(Number.parseInt(cell.background.slice(1, 3), 16) - Number.parseInt(b[index].background.slice(1, 3), 16)) < 8)).toBe(true);
    });
    test("the traveling wave starts at the unused left edge at every occupancy", () => {
        for (const percent of [1, 9, 50, 75, 99]) {
            for (const phase of [0, 4, 8, 17, 31]) {
                const unused = contextMeterCells(percent, 16, snapshot({phase})).filter(cell => !cell.occupied);
                const origin = contextMeterCells(0, unused.length, snapshot({phase}));
                expect(unused.map(cell => cell.background)).toEqual(origin.map(cell => cell.background));
            }
        }
        const unused = contextMeterCells(50, 16, snapshot({phase: 0})).filter(cell => !cell.occupied);
        const brightness = cell => Number.parseInt(cell.background.slice(1, 3), 16);
        expect(brightness(unused[0])).toBe(Math.max(...unused.map(brightness)));
        const later = contextMeterCells(50, 16, snapshot({phase: 4})).filter(cell => !cell.occupied);
        expect(brightness(later[1])).toBe(Math.max(...later.map(brightness)));
    });
    for (const lifecycle of [{working: false}, {motion: false}, {working: false, motion: false}]) {
        test(`idle/reduced-motion is static: ${JSON.stringify(lifecycle)}`, () => {
            const line = renderer();
            const first = line.render(120, snapshot({...lifecycle, phase: 0}));
            expect(line.render(120, snapshot({...lifecycle, phase: 18}))).toBe(first);
            const surface = terminalCells(first)[plain(first).indexOf("50% · 500K")].background;
            const cells = contextMeterCells(50, 16, snapshot(lifecycle));
            expect(cells.slice(8).every(cell => cell.background === surface)).toBe(true);
            const renderedMeter = meterFromLine(first);
            const occupied = contextMeterCells(50, renderedMeter.length).filter(cell => cell.occupied).length;
            expect(renderedMeter.slice(occupied).length).toBeGreaterThan(0);
            expect(renderedMeter.slice(occupied).every(cell => cell.background === surface)).toBe(true);
        });
    }
    test("running to idle to reduced-motion changes no occupied cells", () => {
        const line = renderer();
        const occupied = options => meterFromLine(line.render(160, snapshot(options))).slice(0, 8);
        expect(occupied({working: false})).toEqual(occupied({working: true, phase: 10}));
        expect(occupied({motion: false, phase: 29})).toEqual(occupied({working: true, phase: 10}));
    });
});

describe("independent responsive bubbles", () => {
    for (const width of [80, 100, 120, 160]) {
        test(`${width} cells preserves readable model, context and unread bubbles`, () => {
            const rendered = renderer().render(width, snapshot());
            const text = plain(rendered);
            expect(visibleWidth(rendered)).toBe(width);
            expect(text).toContain("Test model");
            expect(text).toContain("50% · 500K");
            expect(text).toContain("✉ 3");
            expect(text).not.toContain("gpt-test");
            expect(rendered).not.toMatch(/[\r\n\uE000-\uF8FF]/u);
            expectIndependentBubbles(rendered);
            const meter = meterFromLine(rendered);
            if (width === 160) expect(meter).toHaveLength(16);
            else expect([4, 8]).toContain(meter.length);
            const occupied = contextMeterCells(50, meter.length, snapshot()).filter(cell => cell.occupied).length;
            expect(meter.filter(cell => cell.background === "#67cbd0")).toHaveLength(occupied);
        });
    }
    test("roomy identity prefers display name and retains coach and thinking detail", () => {
        const rendered = renderer().render(160, snapshot({thinking: "High"}));
        expect(plain(rendered)).toContain("analyst · Test model · High");
        expect(plain(rendered)).toContain("analyst/task");
        expect(plain(rendered)).toContain("PAVEWAY");
        expect(plain(rendered)).toContain("$0.12");
        expect(terminalCells(rendered).find(cell => cell.text === "P").bold).toBe(true);
        for (const model of [{id: "fallback-model"}, {id: "fallback-model", name: " \n "}]) {
            expect(plain(renderer().render(160, snapshot({model})))).toContain("fallback-model");
        }
    });
    for (const mode of ["truecolor", "256color"]) {
        test(`${mode} paints both Nerd caps onto outside background for every component`, () => {
            const ui = host(mode, "nerd");
            // Connected host separators must not turn independent bubbles into a band.
            ui.theme.sep = {powerlineCapLeft: "\ue0b2", powerlineLeft: "\ue0b0"};
            const night = mode === "truecolor" ? "#161d30" : "256:235";
            const expected = mode === "truecolor"
                ? {PAVEWAY: "#8475cc", "Test model": "#35577f", "analyst/task": "#29384e", "50%": "#203f49", "$0.12": "#222c40", "Wakeup !": "#633c4b"}
                : {PAVEWAY: "256:140", "Test model": "256:240", "analyst/task": "256:237", "50%": "256:237", "$0.12": "256:236", "Wakeup !": "256:239"};
            const rendered = renderer(paveway({wakeup: {unavailable: true}}), ui).render(160, snapshot());
            expect(visibleWidth(rendered)).toBe(160);
            expect(rendered).not.toMatch(/[\ue0b0\ue0b2]/u);
            const bubbles = expectIndependentBubbles(rendered, {opening: "\ue0b6", closing: "\ue0b4", night});
            for (const [label, surface] of Object.entries(expected)) {
                const bubble = bubbles.find(item => item.inner.map(cell => cell.text).join("").includes(label));
                expect(bubble, `missing ${label} bubble`).toBeDefined();
                expect(bubble.inner[0].background).toBe(surface);
            }
            const error = bubbles.find(item => item.inner.map(cell => cell.text).join("").includes("Wakeup !"));
            expect(error.inner.find(cell => cell.text === "!").foreground).toBe(mode === "truecolor" ? "#f5dce0" : "256:224");
            const labelStart = plain(rendered).indexOf("50% · 500K");
            expect(terminalCells(rendered)[labelStart].foreground).toBe(mode === "truecolor" ? "#67cbd0" : "256:116");
            if (mode === "256color") expect(rendered).not.toContain(";2;");
        });
    }
    test("non-Nerd fallback uses symmetric Unicode caps without trusting private-use host separators", () => {
        const ui = host();
        ui.theme.sep = {powerlineCapLeft: "\ue0b6", powerlineLeft: "\ue0b0"};
        const rendered = renderer(paveway(), ui).render(160, snapshot());
        expect(rendered).not.toMatch(/[\uE000-\uF8FF]/u);
        expectIndependentBubbles(rendered);
    });
    test("wide Unicode cap glyphs fall back to complete ASCII pairs", () => {
        const ui = host();
        ui.visibleWidth = value => visibleWidth(value) + (plain(value).match(/[◖◗]/gu) ?? []).length;
        const rendered = renderer(paveway(), ui).render(100, snapshot());
        expect(rendered).not.toMatch(/[◖◗]/u);
        expect(visibleWidth(rendered)).toBe(100);
        expectIndependentBubbles(rendered, {opening: "(", closing: ")"});
    });
    for (const dumb of [false, true]) {
        test(`${dumb ? "TERM=dumb" : "ASCII preset"} uses symmetric parentheses and plain instruments`, () => {
            if (dumb) process.env.TERM = "dumb";
            const rendered = renderer(paveway(), host("truecolor", dumb ? "nerd" : "ascii")).render(100, snapshot());
            const text = plain(rendered);
            expect(visibleWidth(rendered)).toBe(100);
            expect(text).not.toMatch(/[^\x20-\x7e]/u);
            expect(text).toContain("Wake");
            expect(text).toContain("In 3");
            expect(text).toContain("50%");
            const bubbles = bubblesFromLine(rendered, "(", ")");
            expect(bubbles.length).toBeGreaterThan(0);
            if (dumb) expect(rendered).not.toContain("\x1b");
        });
    }
    test("no-event wakeup and zero unread are absent, not empty instrument bubbles", () => {
        for (const wakeup of [null, {unavailable: false, running: false, timing: "none"}]) {
            const rendered = renderer(paveway({wakeup, inbox: 0})).render(160, snapshot());
            const text = plain(rendered);
            expect(text).not.toMatch(/Wakeup|Running|◷|✉|Inbox|none/u);
            expect(text).toContain("Test model");
            expectIndependentBubbles(rendered);
        }
    });
    test("latest state switches between countdown, running, absent and error", () => {
        let state = paveway();
        const line = createStatusLine(() => state, host());
        const first = line.render(160, snapshot());
        expect(plain(first)).toContain("◷ 1m20s");
        expect(plain(first)).not.toContain("Wakeup 1m20s");
        expect(line.render(160, snapshot())).toBe(first);
        state = paveway({coach: "geometry", workflow: "geometry/new-task", inbox: 8, wakeup: {unavailable: false, running: true, timing: "old-countdown"}});
        const running = plain(line.render(160, snapshot()));
        expect(running).toContain("geometry/new-task");
        expect(running).toContain("✉ 8");
        expect(running).toContain("◷ Running");
        expect(running).not.toContain("old-countdown");
        state = paveway({inbox: 0, wakeup: null});
        expect(plain(line.render(160, snapshot()))).not.toMatch(/◷|Wakeup|✉|Inbox/u);
        state = paveway({wakeup: {unavailable: true, running: true, timing: "old-countdown"}});
        const failed = line.render(160, snapshot());
        expect(plain(failed)).toContain("Wakeup !");
        expect(plain(failed)).not.toContain("Running");
        const error = bubblesFromLine(failed).find(item => item.inner.some(cell => cell.text === "!"));
        expect(error.inner[0].background).toBe("#633c4b");
        expect(error.inner.find(cell => cell.text === "!").foreground).toBe("#f5dce0");
    });
    test("undefined and uncertain wakeups are warning bubbles, not an invented countdown", () => {
        for (const wakeup of [undefined, {}, {unavailable: false, running: false}]) {
            const rendered = renderer(paveway({wakeup})).render(160, snapshot());
            expect(plain(rendered)).toContain("Wakeup ?");
            expect(plain(rendered)).not.toMatch(/Running|1m20s|Wakeup none/u);
            const warning = bubblesFromLine(rendered).find(item => item.inner.map(cell => cell.text).join("").includes("Wakeup ?"));
            expect(warning.inner[0].background).toBe("#633c4b");
        }
    });
    test("unknown inbox values remain visible warnings rather than fake zero", () => {
        for (const inbox of [undefined, null, NaN, Infinity, -1]) {
            const rendered = renderer(paveway({inbox, wakeup: null})).render(160, snapshot());
            expect(plain(rendered)).toContain("Inbox ?");
            expect(plain(rendered)).not.toMatch(/✉ 0|Inbox 0/u);
            const warning = bubblesFromLine(rendered).find(item => item.inner.map(cell => cell.text).join("").includes("Inbox ?"));
            expect(warning.inner[0].background).toBe("#633c4b");
        }
    });
    test("long secondary identities cannot displace unread, abnormal wakeup or readable model", () => {
        const line = renderer(paveway({coach: "secondary-coach".repeat(40), workflow: "secondary-workflow".repeat(40), inbox: 7, wakeup: {unavailable: true}}));
        for (const width of [80, 100, 120, 160]) {
            const rendered = line.render(width, snapshot({model: {id: "raw-id", name: "Readable reasoning model"}, thinking: "secondary-thinking".repeat(40)}));
            expect(visibleWidth(rendered)).toBe(width);
            expect(plain(rendered)).toContain("Wakeup !");
            expect(plain(rendered)).toContain("✉ 7");
            expect(plain(rendered)).toContain("Readable reasoning");
            expect(plain(rendered)).toContain("50% · 500K");
            expectIndependentBubbles(rendered);
        }
        const compact = plain(line.render(80, snapshot({model: {id: "raw-id", name: "Readable reasoning model"}, thinking: "secondary-thinking".repeat(40)})));
        expect(compact).not.toContain("secondary");
    });
    test("extreme narrow Unicode never emits half a bubble and keeps errors before decoration", () => {
        const line = renderer(paveway({coach: "long-coach".repeat(50), workflow: "long-workflow".repeat(50), inbox: 7, wakeup: {unavailable: true}}));
        expect(plain(line.render(1, snapshot()))).toBe("!");
        for (const width of [0, 2, 3, 4, 7, 12, 16, 20, 32, 40, 56]) {
            const rendered = line.render(width, snapshot());
            const text = plain(rendered);
            expect(visibleWidth(rendered)).toBe(width);
            if (width <= 2) expect(text.trim()).toBe(width ? "!" : "");
            else expect(text.replace(/◖[^◖◗]*◗/gu, "").trim()).toBe("");
            if (width > 0) expect(text).toContain("!");
            bubblesFromLine(rendered);
            if (width >= 32) {
                expect(text).toContain("Wakeup !");
                expect(text).toContain("✉ 7");
            }
        }
    });
    test("long CJK and combining identities stay sanitized and single-line at every width", () => {
        const unsafe = "\x1b[31m教练e\u0301\x1b[0m\n\t\x1b]8;;https://bad.invalid\x07项目\x1b]8;;\x07\u202e".repeat(20);
        const line = renderer(paveway({coach: unsafe, workflow: unsafe, wakeup: {unavailable: false, timing: unsafe, running: false}}));
        for (const width of [0, 1, 2, 3, 4, 7, 12, 20, 40, 56, 71, 72, 80, 100, 120, 160]) {
            const rendered = line.render(width, snapshot({model: {id: unsafe}, thinking: unsafe}));
            expect(visibleWidth(rendered)).toBe(width);
            expect(plain(rendered)).not.toMatch(/[\x00-\x1f\x7f-\x9f\u202e]/u);
            expect(rendered).not.toContain("\x1b]");
            expect(rendered).not.toContain("https://bad.invalid");
        }
    });
    test("pathologically large real numeric values cannot wrap", () => {
        const line = renderer(paveway({inbox: Number.MAX_VALUE, wakeup: {unavailable: false, timing: "x".repeat(100), running: false}}));
        for (const width of [1, 20, 72, 80, 100, 120, 160]) {
            const result = line.render(width, snapshot({cost: Number.MAX_VALUE, context: {...snapshot().context, window: Number.MAX_VALUE}}));
            expect(visibleWidth(result)).toBe(width);
            if (width >= 80) expect(plain(result)).toContain("50%");
        }
    });
    test("unknown model and capacity are never fabricated", () => {
        const text = plain(renderer({}).render(160, snapshot({model: undefined, thinking: undefined, cost: undefined,
            context: {percent: null, window: undefined, thresholdPercent: null, speculationPercent: null, autoCompact: true}})));
        expect(text).toContain("model ?");
        expect(text).toContain("Wakeup ?");
        expect(text).toContain("Inbox ?");
        expect(text).toContain("?% · ?");
        expect(text).not.toContain("$0");
        expect(text).not.toContain("GPT");
    });
    test("compaction warning color only appears at an actual enabled boundary", () => {
        const line = renderer();
        const warning = percent => terminalCells(line.render(160, snapshot({context: {...snapshot().context, percent}}))).some(cell => cell.foreground === "#d5ab68");
        expect(warning(84)).toBe(false); expect(warning(85)).toBe(true); expect(warning(100)).toBe(true);
        expect(warning(null)).toBe(false);
        expect(terminalCells(line.render(160, snapshot({context: {...snapshot().context, percent: 100, autoCompact: false}}))).some(cell => cell.foreground === "#d5ab68")).toBe(false);
    });
});

describe("host color and lifecycle", () => {
    test("ANSI256 preserves layout and solid capacity without truecolor escapes", () => {
        const rendered = renderer(paveway(), host("256color")).render(100, snapshot());
        expect(rendered).toContain("\x1b[48;5;");
        expect(rendered).not.toContain(";2;");
        expect(visibleWidth(rendered)).toBe(100);
        expect(plain(rendered)).toBe(plain(renderer().render(100, snapshot())));
    });
    test("NO_COLOR is honored even when a truecolor host is present", () => {
        const meterWidth = meterFromLine(renderer().render(100, snapshot())).length;
        const occupied = contextMeterCells(50, meterWidth, snapshot()).filter(cell => cell.occupied).length;
        process.env.NO_COLOR = "";
        const line = renderer();
        const rendered = line.render(100, snapshot());
        expect(rendered).not.toContain("\x1b"); expect(visibleWidth(rendered)).toBe(100);
        expect(rendered.match(/━+/u)?.[0]).toHaveLength(occupied);
        expect(line.render(100, snapshot({phase: 20}))).toBe(rendered);
    });
    test("host no-color, dumb terminals and forced no-color never emit SGR", () => {
        expect(renderer(paveway(), host("none")).render(80, snapshot())).not.toContain("\x1b");
        process.env.TERM = "dumb";
        expect(renderer().render(80, snapshot())).not.toContain("\x1b");
        process.env.TERM = "xterm"; process.env.FORCE_COLOR = "0";
        expect(renderer().render(80, snapshot())).not.toContain("\x1b");
    });
    test("no timers, render requests or reads survive disposal", () => {
        const interval = spyOn(globalThis, "setInterval");
        const timeout = spyOn(globalThis, "setTimeout");
        const getter = () => { reads++; return paveway(); };
        let reads = 0, requests = 0;
        const line = createStatusLine(getter, {...host(), requestRender: () => { requests++; }});
        try {
            line.render(100, snapshot()); line.render(100, snapshot({phase: 15}));
            expect(reads).toBe(2); expect(requests).toBe(0);
            expect(interval).not.toHaveBeenCalled(); expect(timeout).not.toHaveBeenCalled();
            line.dispose(); line.dispose();
            expect(line.render(100, snapshot())).toBe(""); expect(reads).toBe(2);
        } finally { interval.mockRestore(); timeout.mockRestore(); }
    });
    test("sanitization strips terminal protocols and direction controls while retaining text", () => {
        expect(sanitizeStatusText("\x1b[1;31m教练e\u0301\x1b[0m\n hello\u202e\x1b]0;title\x07\x1bPpayload\x1b\\")).toBe("教练e\u0301 hello");
        expect(sanitizeStatusText(undefined)).toBe("");
        expect(renderer().render(-1, snapshot())).toBe("");
        expect(renderer().render(NaN, snapshot())).toBe("");
    });
});
