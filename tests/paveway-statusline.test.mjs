import {afterEach, beforeEach, describe, expect, spyOn, test} from "bun:test";
import {contextMeterCells, createStatusLine, sanitizeStatusText} from "../src/oicoach_runtime/bundled/paveway-statusline.mjs";

const segmenter = new Intl.Segmenter(undefined, {granularity: "grapheme"});
const plain = value => Bun.stripANSI(value);
// Independent perceptual brightness, never the renderer's palette order.
function luminance(hex) {
    const [red, green, blue] = [1, 3, 5].map(index => Number.parseInt(hex.slice(index, index + 2), 16));
    return 0.299 * red + 0.587 * green + 0.114 * blue;
}
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
function host(mode = "truecolor", symbols = "nerd") {
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
    let background = "default", foreground = "default", bold = false, underline = false;
    for (const token of line.matchAll(/\x1b\[([0-9;]*)m|([^\x1b])/gu)) {
        if (token[1] !== undefined) {
            const codes = token[1].split(";").map(Number);
            for (let index = 0; index < codes.length; index++) {
                const code = codes[index];
                if (code === 0) { background = "default"; foreground = "default"; bold = false; underline = false; }
                else if (code === 1) bold = true;
                else if (code === 22) bold = false;
                else if (code === 4) underline = true;
                else if (code === 24) underline = false;
                else if (code === 39) foreground = "default";
                else if (code === 49) background = "default";
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
            for (let index = 0; index < visibleWidth(text); index++) cells.push({text, background, foreground, bold, underline});
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
    const opening = cells.findLastIndex((cell, index) => index < labelStart && ["\ue0b6", "("].includes(cell.text));
    expect(opening).toBeGreaterThanOrEqual(0);
    expect(cells[opening + 1].text).toBe(" ");
    return cells.slice(opening + 2, labelStart - 1);
}
function bubblesFromLine(line, opening = "\ue0b6", closing = "\ue0b4") {
    const cells = terminalCells(line);
    const bubbles = [];
    const isCap = (cell, glyph) => cell.text === glyph && cell.background === "default";
    for (let start = 0; start < cells.length; start++) {
        if (!isCap(cells[start], opening)) continue;
        const end = cells.findIndex((cell, index) => index > start && isCap(cell, closing));
        expect(end).toBeGreaterThan(start);
        expect(cells.slice(start + 1, end).some(cell => isCap(cell, opening))).toBe(false);
        bubbles.push({start, end, left: cells[start], right: cells[end], inner: cells.slice(start + 1, end)});
        start = end;
    }
    expect(cells.filter(cell => isCap(cell, opening))).toHaveLength(bubbles.length);
    expect(cells.filter(cell => isCap(cell, closing))).toHaveLength(bubbles.length);
    return bubbles;
}
function expectIndependentBubbles(line, {opening = "\ue0b6", closing = "\ue0b4"} = {}) {
    const cells = terminalCells(line);
    const bubbles = bubblesFromLine(line, opening, closing);
    expect(bubbles.length).toBeGreaterThan(0);
    for (const [index, bubble] of bubbles.entries()) {
        expect(bubble.inner.length).toBeGreaterThanOrEqual(2);
        expect(bubble.inner[0].text).toBe(" ");
        expect(bubble.inner.at(-1).text).toBe(" ");
        const surface = bubble.inner[0].background;
        expect(bubble.inner.at(-1).background).toBe(surface);
        // Terminal default, never a guessed bar color behind the caps.
        expect(bubble.left.background).toBe("default");
        expect(bubble.right.background).toBe("default");
        expect(bubble.left.foreground).toBe(surface);
        expect(bubble.right.foreground).toBe(surface);
        const previous = bubbles[index - 1];
        if (previous) {
            expect(bubble.start - previous.end).toBeGreaterThan(1);
            expect(cells.slice(previous.end + 1, bubble.start).every(cell => cell.text === " " && cell.background === "default")).toBe(true);
        }
    }
    expect(cells.slice(bubbles.at(-1).end + 1).every(cell => cell.text === " " && cell.background === "default")).toBe(true);
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
        expect(meterFromLine(compact).some(cell => cell.underline || cell.bold)).toBe(true);
        const markerLine = renderer(paveway({coach: "c".repeat(48)})).render(160, snapshot());
        expect(meterFromLine(markerLine)).toHaveLength(16);
        expect(meterFromLine(markerLine).some(cell => cell.underline)).toBe(true);
        expect(meterFromLine(markerLine).some(cell => cell.bold)).toBe(true);
        const full = snapshot({context: {...snapshot().context, percent: 100}});
        const fullMeter = meterFromLine(renderer().render(160, full), "100% · 500K");
        expect(fullMeter).toHaveLength(16);
        expect(fullMeter.every(cell => cell.background === "#67cbd0" && cell.text === " ")).toBe(true);
        expect(plain(renderer().render(160, snapshot({context: {...snapshot().context, autoCompact: false}})))).toContain("auto off");
    });
});

describe("fixed Chevron light flow", () => {
    test("only unused foregrounds change; glyphs, backgrounds, labels and occupied cells stay fixed", () => {
        const line = renderer();
        const frames = Array.from({length: 17}, (_, phase) => line.render(160, snapshot({phase})));
        const first = terminalCells(frames[0]);
        const end = plain(frames[0]).indexOf("50% · 500K") - 1;
        const start = end - 16;
        for (const rendered of frames) {
            expect(plain(rendered)).toBe(plain(frames[0]));
            expect(visibleWidth(rendered)).toBe(160);
            const cells = terminalCells(rendered);
            expect(cells.slice(0, start + 8)).toEqual(first.slice(0, start + 8));
            expect(cells.slice(end)).toEqual(first.slice(end));
            expect(cells.map(cell => cell.background)).toEqual(first.map(cell => cell.background));
            expect(cells.slice(start + 8, end).every(cell => cell.text === "\ue0b1")).toBe(true);
        }
        expect(frames[8]).not.toBe(frames[0]);
        expect(frames[16]).toBe(frames[0]);
    });
    for (const mode of ["truecolor", "256color"]) {
        test(`${mode} light packet travels right with a soft periodic seam`, () => {
            const line = renderer(paveway(), host(mode));
            const meter = phase => meterFromLine(line.render(160,
                snapshot({phase, context: {...snapshot().context, percent: 0}})), "0% · 500K");
            const first = meter(0);
            const crest = cells => cells.findIndex(cell => cell.foreground === first[0].foreground);
            expect(first.every(cell => cell.text === "\ue0b1")).toBe(true);
            expect(new Set(first.map(cell => cell.foreground)).size).toBeGreaterThanOrEqual(5);
            for (let phase = 0; phase < 16; phase++) {
                const before = meter(phase), after = meter(phase + 1);
                expect(crest(after)).toBe((crest(before) + 1) % 16);
                expect(after.map(cell => cell.foreground)).toEqual(
                    before.map((_, index) => before[(index + 15) % 16].foreground));
                expect(after.map(cell => cell.background)).toEqual(first.map(cell => cell.background));
                if (mode === "truecolor") {
                    for (let x = 0; x < 16; x++) {
                        expect(Math.abs(luminance(after[x].foreground) - luminance(before[x].foreground))).toBeLessThan(20);
                    }
                }
            }
            expect(meter(16)).toEqual(first);
            expect(meter(-1)).toEqual(meter(15));
        });
    }
    test("the packet is anchored to unused capacity and short tails never blink", () => {
        for (const percent of [1, 9, 50, 75, 99]) {
            for (const phase of [0, 4, 8, 17, 31]) {
                const unused = contextMeterCells(percent, 16, snapshot({phase})).filter(cell => !cell.occupied);
                const origin = contextMeterCells(0, unused.length, snapshot({phase}));
                expect(unused).toEqual(origin);
            }
        }
        for (const percent of [90, 99, 100]) {
            expect(contextMeterCells(percent, 16, snapshot({phase: 9}))).toEqual(
                contextMeterCells(percent, 16, snapshot({phase: 1})));
        }
    });
    test("compact four-cell flow retains a visible crest throughout the cycle", () => {
        const cells = phase => contextMeterCells(0, 4, snapshot({phase}));
        const crest = phase => {
            const values = cells(phase).map(cell => luminance(cell.foreground));
            return values.indexOf(Math.max(...values));
        };
        for (let phase = 0; phase < 16; phase += 4) {
            expect(crest(phase + 4)).toBe((crest(phase) + 1) % 4);
        }
        for (let phase = 0; phase < 16; phase++) {
            expect(luminance(cells(phase)[crest(phase)].foreground)).toBeGreaterThan(170);
        }
        expect(cells(16)).toEqual(cells(0));
    });
    for (const lifecycle of [{working: false}, {motion: false}, {working: false, motion: false}]) {
        test(`idle/reduced-motion keeps complete static chevrons: ${JSON.stringify(lifecycle)}`, () => {
            const line = renderer();
            const first = line.render(120, snapshot({...lifecycle, phase: 0}));
            expect(line.render(120, snapshot({...lifecycle, phase: 18}))).toBe(first);
            const meter = meterFromLine(first);
            const unused = meter.filter(cell => cell.text === "\ue0b1");
            expect(unused.length).toBeGreaterThan(0);
            expect(unused.every(cell => cell.foreground !== cell.background)).toBe(true);
            expect(meter.filter(cell => cell.text === " ").every(cell => cell.background === "#67cbd0")).toBe(true);
        });
    }
    test("live context updates and resize invalidate layout without waiting for a tick", () => {
        const line = renderer();
        for (const [percent, occupied] of [[0, 0], [1, 1], [50, 8], [99, 15], [100, 16], [0, 0]]) {
            const context = {...snapshot().context, percent};
            const meter = meterFromLine(line.render(160, snapshot({phase: 4, context})), `${percent}% · 500K`);
            expect(meter.filter(cell => cell.text === " ")).toHaveLength(occupied);
            expect(meter.filter(cell => cell.text === "\ue0b1")).toHaveLength(16 - occupied);
        }
        for (const width of [40, 80, 160, 100, 24, 160]) {
            const rendered = line.render(width, snapshot({phase: 4}));
            expect(visibleWidth(rendered)).toBe(width);
            expect(plain(rendered)).toContain("50%");
        }
    });
    test("symbol/color/ratio matrix keeps single-cell positions and resets all styling", () => {
        for (const [preset, capStyle, glyph] of [["nerd", "auto", "\ue0b1"],
            ["unicode", "powerline", "\ue0b1"], ["unicode", "auto", ">"], ["ascii", "auto", ">"]]) {
            for (const mode of ["truecolor", "256color", "none"]) {
                const line = createStatusLine(() => paveway({inbox: 0, wakeup: null}), host(mode, preset), {capStyle});
                for (const percent of [0, 1, 50, 99, 100]) {
                    for (const width of [8, 20, 40, 64, 80, 120, 160, 240]) {
                        const state = snapshot({context: {...snapshot().context, percent}});
                        const first = line.render(width, state);
                        const later = line.render(width, {...state, phase: 7});
                        expect(visibleWidth(first)).toBe(width);
                        expect(plain(first)).toContain(`${percent}%`);
                        expect(plain(later)).toBe(plain(first));
                        expect(plain(first)).not.toMatch(/[\r\n\ufffd]/u);
                        if (mode === "none") expect(first).not.toContain("\x1b");
                        if (mode === "256color") expect(first).not.toContain(";2;");
                        if (preset === "ascii") expect(plain(first)).toMatch(/^[\x20-\x7e]*$/u);
                        const arrows = terminalCells(first).filter(cell => cell.text === glyph);
                        expect(arrows.every(cell => visibleWidth(cell.text) === 1)).toBe(true);
                        if (width >= 80 && percent < 100) expect(arrows.length).toBeGreaterThan(0);
                        if (percent === 100) expect(arrows).toEqual([]);
                        const tail = terminalCells(first + "Z").at(-1);
                        expect(tail).toEqual({text: "Z", background: "default", foreground: "default", bold: false, underline: false});
                    }
                }
            }
        }
    });
    test("a wide-reported Chevron falls back without changing its one-cell budget", () => {
        const ui = host();
        ui.visibleWidth = value => visibleWidth(value) + (plain(value).match(/\ue0b1/gu) ?? []).length;
        const rendered = renderer(paveway(), ui).render(160, snapshot());
        expect(visibleWidth(rendered)).toBe(160);
        expect(meterFromLine(rendered).slice(8).every(cell => cell.text === ">")).toBe(true);
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
            expect(rendered).not.toMatch(/[\r\n\ufffd◖◗\ue0b0\ue0b2]/u);
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
        test(`${mode} paints both Powerline caps onto terminal default background for every component`, () => {
            const ui = host(mode, "nerd");
            // Connected host separators must not turn independent bubbles into a band.
            ui.theme.sep = {powerlineCapLeft: "\ue0b2", powerlineLeft: "\ue0b0"};
            const expected = mode === "truecolor"
                ? {PAVEWAY: "#8475cc", "Test model": "#35577f", "analyst/task": "#29384e", "50%": "#203f49", "$0.12": "#222c40", "Wakeup !": "#633c4b"}
                : {PAVEWAY: "256:98", "Test model": "256:60", "analyst/task": "256:17", "50%": "256:23", "$0.12": "256:235", "Wakeup !": "256:95"};
            const rendered = renderer(paveway({wakeup: {unavailable: true}}), ui).render(160, snapshot());
            expect(visibleWidth(rendered)).toBe(160);
            expect(rendered).not.toMatch(/[\ue0b0\ue0b2◖◗]/u);
            const bubbles = expectIndependentBubbles(rendered);
            for (const [label, surface] of Object.entries(expected)) {
                const bubble = bubbles.find(item => item.inner.map(cell => cell.text).join("").includes(label));
                expect(bubble, `missing ${label} bubble`).toBeDefined();
                expect(bubble.inner[0].background).toBe(surface);
            }
            const error = bubbles.find(item => item.inner.map(cell => cell.text).join("").includes("Wakeup !"));
            expect(error.inner.find(cell => cell.text === "!").foreground).toBe(mode === "truecolor" ? "#f5dce0" : "256:224");
            const labelStart = plain(rendered).indexOf("50% · 500K");
            expect(terminalCells(rendered)[labelStart].foreground).toBe(mode === "truecolor" ? "#67cbd0" : "256:80");
            if (mode === "256color") expect(rendered).not.toContain(";2;");
        });
    }
    test("256-color quantization keeps the four semantic surfaces distinct", () => {
        const rendered = renderer(paveway({wakeup: {unavailable: true}}), host("256color")).render(160, snapshot());
        const surfaces = expectIndependentBubbles(rendered).map(bubble => bubble.inner[0].background);
        const semantic = ["256:98", "256:60", "256:17", "256:23"];
        for (const surface of semantic) expect(surfaces).toContain(surface);
        expect(new Set(semantic).size).toBe(4);
    });
    test("Unicode fallback has no private-use glyphs, even when host separators do", () => {
        const ui = host("truecolor", "unicode");
        ui.theme.sep = {powerlineCapLeft: "\ue0b6", powerlineLeft: "\ue0b0"};
        const rendered = renderer(paveway(), ui).render(160, snapshot());
        expect(plain(rendered)).not.toMatch(/[\uE000-\uF8FF]/u);
        expect(meterFromLine(rendered).slice(8).every(cell => cell.text === ">")).toBe(true);
        expectIndependentBubbles(rendered, {opening: "(", closing: ")"});
    });
    test("wide measured Powerline glyphs fall back to complete ASCII pairs", () => {
        const ui = host();
        ui.visibleWidth = value => visibleWidth(value) + (plain(value).match(/[\ue0b6\ue0b4]/gu) ?? []).length;
        const rendered = renderer(paveway(), ui).render(100, snapshot());
        expect(rendered).not.toMatch(/[\ue0b6\ue0b4]/u);
        expect(visibleWidth(rendered)).toBe(100);
        expectIndependentBubbles(rendered, {opening: "(", closing: ")"});
    });
    for (const [symbols, capStyle] of [["nerd", "auto"], ["unicode", "powerline"]]) {
        test(`${symbols}/${capStyle} uses the same single-cell Powerline Chevron`, () => {
            const rendered = createStatusLine(() => paveway(), host("truecolor", symbols), {capStyle}).render(160, snapshot());
            expect(meterFromLine(rendered).slice(8).map(cell => cell.text).join("")).toBe("\ue0b1".repeat(8));
            expectIndependentBubbles(rendered);
        });
    }
    test("live symbol changes invalidate cached glyphs and preserve cell budgets", () => {
        let symbols = "nerd";
        const ui = host();
        ui.theme.getSymbolPreset = () => symbols;
        const line = createStatusLine(() => paveway(), ui);
        const nerd = line.render(160, snapshot());
        symbols = "unicode";
        const unicode = line.render(160, snapshot());
        expect(plain(unicode)).not.toMatch(/[\uE000-\uF8FF]/u);
        expect(meterFromLine(unicode).slice(8).every(cell => cell.text === ">")).toBe(true);
        expect(visibleWidth(unicode)).toBe(visibleWidth(nerd));
        expectIndependentBubbles(unicode, {opening: "(", closing: ")"});
    });
    test("explicit plain removes Powerline PUA even with the Unicode preset", () => {
        const rendered = createStatusLine(() => paveway(), host("truecolor", "unicode"),
            {capStyle: "plain"}).render(100, snapshot());
        expect(rendered).not.toMatch(/[\uE000-\uF8FF]/u);
        expect(visibleWidth(rendered)).toBe(100);
        expectIndependentBubbles(rendered, {opening: "(", closing: ")"});
    });
    test("ASCII preset stays pure ASCII unless Powerline is explicit", () => {
        const rendered = renderer(paveway(), host("truecolor", "ascii")).render(100, snapshot());
        expect(plain(rendered)).toMatch(/^[\x20-\x7e]*$/u);
        expectIndependentBubbles(rendered, {opening: "(", closing: ")"});
        const explicit = createStatusLine(() => paveway(), host("truecolor", "ascii"),
            {capStyle: "powerline"}).render(100, snapshot());
        expect(explicit).toContain("\ue0b6");
        expect(explicit).toContain("\ue0b4");
        expectIndependentBubbles(explicit);
    });
    test("environment override is explicit, not guessed", () => {
        process.env.PAVEWAY_STATUSLINE_CAPS = "plain";
        try {
            const rendered = renderer(paveway(), host("truecolor", "unicode")).render(100, snapshot());
            expect(rendered).not.toMatch(/[\uE000-\uF8FF]/u);
            expectIndependentBubbles(rendered, {opening: "(", closing: ")"});
        } finally { delete process.env.PAVEWAY_STATUSLINE_CAPS; }
    });
    test("an invalid environment value cannot mask an explicit setting", () => {
        process.env.PAVEWAY_STATUSLINE_CAPS = "bogus";
        try {
            const rendered = createStatusLine(() => paveway(), host("truecolor", "unicode"),
                {capStyle: "plain"}).render(100, snapshot());
            expect(rendered).not.toMatch(/[\uE000-\uF8FF]/u);
            expectIndependentBubbles(rendered, {opening: "(", closing: ")"});
        } finally { delete process.env.PAVEWAY_STATUSLINE_CAPS; }
    });
    test("ASCII preset uses symmetric parentheses and plain instruments", () => {
        const rendered = renderer(paveway(), host("truecolor", "ascii")).render(100, snapshot());
        const text = plain(rendered);
        expect(visibleWidth(rendered)).toBe(100);
        expect(text).not.toMatch(/[^\x20-\x7e]/u);
        expect(text).toContain("Wake");
        expect(text).toContain("In 3");
        expect(text).toContain("50%");
        const bubbles = bubblesFromLine(rendered, "(", ")");
        expect(bubbles.length).toBeGreaterThan(0);
    });
    test("TERM=dumb controls ANSI only, never the Powerline cap choice", () => {
        process.env.TERM = "dumb";
        const rendered = renderer(paveway(), host("truecolor", "nerd")).render(100, snapshot());
        expect(rendered).not.toContain("\x1b");
        expect(rendered).toContain("\ue0b6");
        expect(rendered).toContain("\ue0b4");
        expect(visibleWidth(rendered)).toBe(100);
        expect(plain(rendered)).toContain("Wake");
        expect(plain(rendered)).toContain("50%");
        bubblesFromLine(rendered);
    });
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
    test("absent and unknown wakeups remain distinct after layout caching", () => {
        let state = paveway({wakeup: null});
        const line = createStatusLine(() => state, host());
        expect(plain(line.render(160, snapshot()))).not.toContain("Wakeup");
        for (const wakeup of [undefined, {}, null, undefined]) {
            state = {...state, wakeup};
            for (let phase = 0; phase < 2; phase++) {
                const text = plain(line.render(160, snapshot({phase})));
                expect(text.includes("Wakeup ?")).toBe(wakeup !== null);
            }
        }
        expect(plain(line.render(1, snapshot()))).toBe("!");
        expect(plain(line.render(1, snapshot({phase: 7})))).toBe("!");
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
            else expect(text.replace(/\ue0b6[^\ue0b6\ue0b4]*\ue0b4/gu, "").trim()).toBe("");
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
