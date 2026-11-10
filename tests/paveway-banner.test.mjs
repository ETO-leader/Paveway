import {afterEach, beforeEach, describe, expect, test} from "bun:test";
import {createBanner} from "../src/oicoach_runtime/bundled/paveway-banner.mjs";

const plain = value => Bun.stripANSI(value);
const visibleWidth = value => Bun.stringWidth(plain(value));
const segmenter = new Intl.Segmenter(undefined, {granularity: "grapheme"});
// The production host provides these helpers. This adapter measures with Bun,
// and clips whole graphemes instead of duplicating the banner's layout logic.
function truncateToWidth(value, width) {
    let result = "", occupied = 0;
    for (const {segment} of segmenter.segment(plain(value))) {
        const next = visibleWidth(segment);
        if (occupied + next > width) break;
        result += segment;
        occupied += next;
    }
    return result;
}
function host(mode = "truecolor", preset = "unicode") {
    return {visibleWidth, truncateToWidth, theme: {getColorMode: () => mode, getSymbolPreset: () => preset}};
}
const config = {version: "1.4.2", workflow: "竞赛🙂e\u0301 / 动态规划与图论 / long workflow"};
const banner = (ui = host(), options = config) => createBanner(options, ui);
const text = lines => lines.map(plain).join("\n");
let environment;
beforeEach(() => {
    environment = Object.fromEntries(["NO_COLOR", "FORCE_COLOR", "TERM"].map(key => [key, process.env[key]]));
    delete process.env.NO_COLOR;
    delete process.env.FORCE_COLOR;
    process.env.TERM = "xterm-256color";
});
afterEach(() => {
    for (const [key, value] of Object.entries(environment)) {
        if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
});

describe("Paveway Aurora × Pathline banner", () => {
    test("resizes and animation frames stay within the Unicode display budget", () => {
        const render = banner();
        for (const width of [120, 80, 67, 66, 50, 40, 24, 18, 7, 3, 2, 1, 0, 80]) {
            for (const progress of [0, 0.2, 0.4, 0.6, 0.8, 1]) {
                const lines = render(width, "13.9.0", progress);
                expect(lines.every(row => visibleWidth(row) <= width)).toBe(true);
                expect(lines.every(row => !/[\r\n\uFFFD]/.test(row))).toBe(true);
            }
        }
        expect(render(0, "13.9.0")).toEqual([]);
        expect(text(render(1, "13.9.0"))).toContain("P");
        expect(plain(render(24, "13.9.0")[0]).trim()).toBe("PAVEWAY");
    });

    test("wordmark, rail and supporting copy share the terminal center", () => {
        for (const width of [80, 120, 160]) {
            const lines = banner()(width, "13.9.0").map(plain);
            const rail = lines.find(row => row.includes("─"));
            const logo = lines.filter(row => /[█▀▄]/.test(row));
            expect(visibleWidth(rail.trim())).toBeGreaterThan(Math.max(...logo.map(row => visibleWidth(row.trim()))));
            for (const row of [...logo.slice(0, 1), ...lines.filter(row => /OI COACHING|Paveway|Tip:|─/.test(row))]) {
                const start = row.length - row.trimStart().length;
                const occupied = visibleWidth(row.trimEnd());
                expect(Math.abs(start - (width - occupied))).toBeLessThanOrEqual(1);
            }
            expect(rail.match(/●/g)).toHaveLength(1);
            expect(rail.indexOf("●") - rail.indexOf("─")).toBeGreaterThan(visibleWidth(rail.trim()) * 0.85);
        }
    });

    test("fade, sweep and moving pathline keep every row in place and settle deterministically", () => {
        const render = banner();
        const final = render(80, "13.9.0");
        const logo = final.filter(row => /[█▀▄]/.test(plain(row)));
        let previousDot = -1;
        for (const progress of [0, 0.2, 0.4, 0.6, 0.8, 1]) {
            const frame = render(80, "13.9.0", progress);
            expect(frame.map(visibleWidth)).toEqual(final.map(visibleWidth));
            expect(frame.filter(row => /[█▀▄]/.test(plain(row))).map(plain)).toEqual(logo.map(plain));
            const dot = plain(frame.find(row => plain(row).includes("●"))).indexOf("●");
            expect(dot).toBeGreaterThanOrEqual(previousDot);
            previousDot = dot;
        }
        expect(render(80, "13.9.0", 0)).not.toEqual(final);
        expect(render(80, "13.9.0", 0.6)).not.toEqual(final);
        expect(render(80, "13.9.0", 2)).toEqual(final);
    });

    test("Unicode workflow truncation preserves both versions and their requested ordering", () => {
        const render = banner();
        for (const width of [67, 72, 80, 120]) {
            const output = text(render(width, "13.9.0"));
            expect(output).toMatch(/Paveway v1\.4\.2 · 竞赛🙂é.*… · OMP 13\.9\.0/);
        }
        const narrow = text(render(18, "13.9.0"));
        expect(narrow).toContain("Paveway v1.4.2");
        expect(narrow).toContain("OMP 13.9.0");
        const changed = text(render(80, "14.0.1"));
        expect(changed).toContain("OMP 14.0.1");
        expect(changed).not.toContain("13.9.0");
    });

    test("TrueColor palette reaches all requested stops without painting a background", () => {
        const output = banner()(80, "13.9.0").join("\n");
        for (const rgb of ["228;91;196", "164;99;244", "104;126;255", "85;197;229"]) {
            expect(output).toContain(`\x1b[38;2;${rgb}m`);
        }
        expect(output).toContain("\x1b[3mTip: Find the insight. Build the solution.");
        expect(output).not.toMatch(/\x1b\[(?:48;|4[0-7]m)/);
    });

    test("live color and symbol capabilities replace cached ANSI and block artwork", () => {
        let mode = "truecolor", preset = "unicode";
        const ui = {...host(), theme: {getColorMode: () => mode, getSymbolPreset: () => preset}};
        const render = banner(ui);
        expect(render(80, "13.9.0").join("")).toContain("\x1b[38;2;");
        mode = "ansi256";
        const indexed = render(80, "13.9.0").join("");
        expect(indexed).toContain("\x1b[38;5;");
        expect(indexed).not.toContain("\x1b[38;2;");
        preset = "ascii";
        const ascii = text(render(80, "13.9.0"));
        expect(ascii).toContain("PAVEWAY");
        expect(ascii).not.toMatch(/[█▀▄─●…]/);
        expect(ascii).toContain("---");
        for (const [key, value] of [["NO_COLOR", ""], ["FORCE_COLOR", "0"], ["TERM", "dumb"]]) {
            process.env[key] = value;
            expect(render(80, "13.9.0").join("")).not.toContain("\x1b");
            if (key === "TERM") process.env.TERM = "xterm-256color";
            else delete process.env[key];
        }
        mode = "none";
        expect(render(80, "13.9.0").join("")).not.toContain("\x1b");
    });
});
