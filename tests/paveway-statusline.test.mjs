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
function meterFromLine(line, label = " 50%/500K ") {
    const start = plain(line).indexOf(label);
    expect(start).toBeGreaterThanOrEqual(16);
    return terminalCells(line).slice(start - 16, start);
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
            expect(plain(line)).toContain(` ${percent}%/500K `);
            expect(terminalCells(line).filter(cell => cell.background === "#67cbd0")).toHaveLength(occupied);
        });
    }
    test("unknown, NaN and infinities are unknown, not a fabricated zero", () => {
        for (const percent of [null, undefined, NaN, Infinity, -Infinity]) {
            const context = {...snapshot().context, percent};
            const line = renderer().render(160, snapshot({context}));
            expect(plain(line)).toContain(" ?%/500K ");
            expect(plain(line)).not.toContain(" 0%/500K ");
            const first = contextMeterCells(percent, 16, snapshot({phase: 0}));
            expect(first.every(cell => !cell.occupied && cell.background === "#161d30")).toBe(true);
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
        expect(plain(compact)).toContain("┊");
        expect(plain(compact)).toContain("│");
        const full = snapshot({context: {...snapshot().context, percent: 100}});
        expect(meterFromLine(renderer().render(160, full), " 100%/500K ").every(cell => cell.background === "#67cbd0" && cell.text === " ")).toBe(true);
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
        const end = plain(first).indexOf(" 50%/500K ");
        const start = end - 16;
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
            const cells = contextMeterCells(50, 16, snapshot(lifecycle));
            expect(cells.slice(8).every(cell => cell.background === "#161d30")).toBe(true);
        });
    }
    test("running to idle to reduced-motion changes no occupied cells", () => {
        const line = renderer();
        const occupied = options => meterFromLine(line.render(160, snapshot(options))).slice(0, 8);
        expect(occupied({working: false})).toEqual(occupied({working: true, phase: 10}));
        expect(occupied({motion: false, phase: 29})).toEqual(occupied({working: true, phase: 10}));
    });
});

describe("single-row responsive consumer", () => {
    for (const width of [80, 100, 120, 160]) {
        test(`${width} cells connects real identity and right-hand instruments`, () => {
            const rendered = renderer().render(width, snapshot());
            const text = plain(rendered);
            expect(visibleWidth(rendered)).toBe(width);
            expect(text).toContain("PAVEWAY");
            expect(text).toContain("gpt-test");
            expect(text).toContain("wake 1m20s");
            expect(text).toContain(`${width >= 120 ? "Inbox" : "in"} 3`);
            expect(text).toContain("$0.12");
            expect(text).toContain("50%/500K");
            if (width >= 120) {
                expect(text).toContain("analyst");
                expect(text).toContain("analyst/task");
                expect(text).toContain("high");
            }
            const cells = terminalCells(rendered);
            expect(cells.find(cell => cell.text === "P").bold).toBe(true);
            expect(/[\r\n\uE000-\uF8FF]/u.test(rendered)).toBe(false);
        });
    }
    test("default Unicode and opt-in Nerd caps respect host font information", () => {
        const unicode = renderer().render(100, snapshot());
        expect(unicode).not.toMatch(/[\uE000-\uF8FF]/u);
        expect(plain(unicode).startsWith("◖PAVEWAY◗")).toBe(true);
        const nerd = renderer(paveway(), host("truecolor", "nerd")).render(100, snapshot());
        expect(nerd).toContain("\ue0b6"); expect(nerd).toContain("\ue0b0");
        expect(visibleWidth(nerd)).toBe(100);
    });
    test("Nerd connection glyphs come from the real host separator surface", () => {
        const ui = host("truecolor", "nerd");
        ui.theme.sep.powerlineCapLeft = "\ue0b2";
        ui.theme.sep.powerlineLeft = "\ue0b4";
        const rendered = renderer(paveway(), ui).render(100, snapshot());
        expect(plain(rendered).startsWith("\ue0b2PAVEWAY\ue0b4")).toBe(true);
        expect(visibleWidth(rendered)).toBe(100);
        const unicode = host();
        unicode.theme.sep.powerlineCapLeft = "\ue0b6";
        expect(renderer(paveway(), unicode).render(100, snapshot())).not.toMatch(/[\uE000-\uF8FF]/u);
    });
    test("compact long identities hide secondary fields instead of leaving tiny fragments", () => {
        const line = renderer(paveway({coach: "secondary-coach-profile", workflow: "secondary-workflow/training"}));
        const compact = plain(line.render(80, snapshot({model: {id: "reasoning-model-with-a-long-identifier"}})));
        expect(compact).toContain("reasoning-model");
        expect(compact).not.toContain("secondary");
        expect(compact).toContain("50%/500K");
        expect(compact).toContain("in 3");
    });
    test("long CJK and combining identities stay single-line at every width", () => {
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
    test("extreme narrow keeps error and unread priority before decorative identity", () => {
        const line = renderer(paveway({coach: "long-coach".repeat(50), workflow: "long-workflow".repeat(50), inbox: 7, wakeup: {unavailable: true}}));
        expect(plain(line.render(1, snapshot()))).toBe("!");
        for (const width of [12, 16, 20, 32, 56]) {
            const rendered = line.render(width, snapshot());
            expect(visibleWidth(rendered)).toBe(width);
            expect(plain(rendered)).toContain("wake!");
            expect(plain(rendered)).toContain("in 7");
            if (width >= 16) expect(plain(rendered)).toContain("50%");
        }
        expect(plain(line.render(32, snapshot()))).toContain("gpt-test");
    });
    test("pathologically large real numeric values cannot wrap", () => {
        const line = renderer(paveway({inbox: Number.MAX_VALUE, wakeup: {unavailable: false, timing: "x".repeat(100), running: false}}));
        for (const width of [1, 20, 72, 80, 100, 120, 160]) {
            const result = line.render(width, snapshot({cost: Number.MAX_VALUE, context: {...snapshot().context, window: Number.MAX_VALUE}}));
            expect(visibleWidth(result)).toBe(width);
            if (width >= 72) expect(plain(result)).toContain("50%/");
        }
    });
    test("unknown instruments remain unknown, never zero or a synthetic identity", () => {
        const line = renderer({});
        const text = plain(line.render(160, snapshot({model: undefined, thinking: undefined, cost: undefined,
            context: {percent: null, window: undefined, thresholdPercent: null, speculationPercent: null, autoCompact: true}})));
        expect(text).toContain("coach ?"); expect(text).toContain("workflow ?");
        expect(text).toContain("model ?"); expect(text).toContain("wake ?");
        expect(text).toContain("Inbox ?"); expect(text).toContain("?%/?");
        expect(text).not.toContain("$0"); expect(text).not.toContain("GPT");
    });
    test("wakeup states and inbox values come from the latest state, not a local counter", () => {
        let state = paveway();
        const line = createStatusLine(() => state, host());
        const first = line.render(160, snapshot());
        expect(line.render(160, snapshot())).toBe(first);
        state = paveway({coach: "geometry", workflow: "geometry/new-task", inbox: 8, wakeup: {unavailable: false, running: true}});
        const running = plain(line.render(160, snapshot()));
        expect(running).toContain("geometry/new-task"); expect(running).toContain("Inbox 8"); expect(running).toContain("wake running");
        state = paveway({inbox: 0, wakeup: {unavailable: false, running: false, timing: "none"}});
        expect(plain(line.render(160, snapshot()))).toContain("wake none");
        expect(plain(line.render(160, snapshot()))).toContain("Inbox 0");
        state = paveway({wakeup: {unavailable: true}});
        const failed = line.render(160, snapshot());
        expect(plain(failed)).toContain("wake !");
        expect(terminalCells(failed).some(cell => cell.foreground === "#d67d87")).toBe(true);
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
        process.env.NO_COLOR = "";
        const line = renderer();
        const rendered = line.render(100, snapshot());
        expect(rendered).not.toContain("\x1b"); expect(visibleWidth(rendered)).toBe(100);
        expect(rendered).toContain("━━━━━━━━");
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
