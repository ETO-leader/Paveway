// A 63-column, twelve-pixel wordmark, packed into six terminal rows. All
// strokes use the terminal's standard block repertoire; no font assets or host imports.
const GLYPHS = {
    P: ["1111110", "1111111", "1100011", "1100011", "1111111", "1111110", "1100000", "1100000", "1100000", "1100000", "1100000", "1100000"],
    A: ["0011100", "0111110", "1100011", "1100011", "1100011", "1100011", "1111111", "1111111", "1100011", "1100011", "1100011", "1100011"],
    V: ["1100011", "1100011", "1100011", "1100011", "1100011", "1100011", "0110110", "0110110", "0110110", "0011100", "0011100", "0011100"],
    E: ["1111111", "1111111", "1100000", "1100000", "1100000", "1111110", "1111110", "1100000", "1100000", "1100000", "1111111", "1111111"],
    W: ["110000011", "110000011", "110000011", "110000011", "110000011", "110010011", "110111011", "110111011", "111101111", "111101111", "011000110", "011000110"],
    Y: ["1100011", "1100011", "1100011", "0110110", "0110110", "0011100", "0011100", "0011100", "0011100", "0011100", "0011100", "0011100"],
};
const PIXELS = Array.from({length: 12}, (_, row) => [..."PAVEWAY"].map(letter => GLYPHS[letter][row]).join("00"));
const LOGO = Array.from({length: 6}, (_, row) => Array.from({length: PIXELS[0].length}, (_, col) => {
    const top = PIXELS[row * 2][col] === "1";
    const bottom = PIXELS[row * 2 + 1][col] === "1";
    return top ? (bottom ? "█" : "▀") : (bottom ? "▄" : " ");
}));
const STOPS = [[228, 91, 196], [164, 99, 244], [104, 126, 255], [85, 197, 229]];
const RAIL = [47, 58, 78];
const DIM = [24, 29, 42];
const MUTED = [128, 143, 165];
const GOLD = [220, 182, 119];
const RESET = "\x1b[0m";
const STEPS = 48;
const mix = (a, b, amount) => a.map((value, index) => Math.round(value + (b[index] - value) * amount));
const clamp = value => Math.max(0, Math.min(1, value));
const smooth = value => { const t = clamp(value); return t * t * (3 - 2 * t); };
const ANCHORS = STOPS.map((_, index) => Math.round(index * (PIXELS[0].length - 1) / (STOPS.length - 1)));
const GRADIENT = Array.from({length: PIXELS[0].length}, (_, col) => {
    const index = col <= ANCHORS[1] ? 0 : col <= ANCHORS[2] ? 1 : 2;
    const position = (col - ANCHORS[index]) / (ANCHORS[index + 1] - ANCHORS[index]);
    return mix(STOPS[index], STOPS[index + 1], position);
});

// Choose the nearest xterm cube/gray entry, not the theme-dependent first 16.
function indexedColor(rgb) {
    const levels = [0, 95, 135, 175, 215, 255];
    const cube = rgb.map(channel => levels.reduce((best, value, index) =>
        Math.abs(channel - value) < Math.abs(channel - levels[best]) ? index : best, 0));
    const gray = Math.max(0, Math.min(23, Math.round((rgb.reduce((a, b) => a + b) / 3 - 8) / 10)));
    const cubeError = rgb.reduce((sum, value, index) => sum + (value - levels[cube[index]]) ** 2, 0);
    const grayError = rgb.reduce((sum, value) => sum + (value - (8 + gray * 10)) ** 2, 0);
    return grayError < cubeError ? 232 + gray : 16 + cube[0] * 36 + cube[1] * 6 + cube[2];
}
function color(rgb, mode) {
    return mode === "none" ? "" : mode === "truecolor"
        ? `\x1b[38;2;${rgb.join(";")}m` : `\x1b[38;5;${indexedColor(rgb)}m`;
}
function colorMode(ui) {
    if (process.env.NO_COLOR !== undefined || process.env.FORCE_COLOR === "0" || process.env.TERM === "dumb") return "none";
    const mode = ui.theme.getColorMode();
    return mode === "none" || mode === false ? "none" : mode === "truecolor" ? "truecolor" : "ansi256";
}
function singleLine(value) {
    return String(value ?? "").replace(/[\r\n\t]/g, " ").replace(/[\x00-\x1f\x7f-\x9f]/g, "").trim();
}
const versionLabel = value => value.startsWith("v") ? value : `v${value}`;

/**
 * The host owns timing. Call the returned renderer with its actual OMP version
 * and progress (0..1); omitting progress gives the exact resting transcript.
 * ui supplies only display-width helpers and theme capability getters.
 */
export function createBanner({version, workflow}, ui) {
    const pavewayVersion = versionLabel(singleLine(version));
    const workflowName = singleLine(workflow);
    // Frames share the bitmap and one color per column across all six rows.
    // Keep only the active capability set and most recent resting layout.
    let capability;
    let artFrames = new Map();
    let resting;
    const clip = (text, width) => width <= 0 ? "" : ui.truncateToWidth(text, width, "");
    const shorten = (text, width, ascii) => ui.visibleWidth(text) <= width ? text
        : width < 2 ? clip(text, width) : clip(text, width - 1) + (ascii ? "~" : "…");

    function artwork(mode, step) {
        const cached = artFrames.get(step);
        if (cached) return cached;
        const progress = step / STEPS;
        const fade = smooth(progress / 0.4);
        const sweep = (progress - 0.3) / 0.55;
        const sweepStrength = progress > 0.3 && progress < 0.85 ? Math.sin(clamp(sweep) * Math.PI) * 0.28 : 0;
        const columns = GRADIENT.map((rgb, col) => {
            const distance = Math.abs(col / (GRADIENT.length - 1) - sweep);
            const glow = Math.max(0, 1 - distance / 0.22) * sweepStrength;
            return color(mix(mix(DIM, rgb, fade), [255, 244, 255], glow), mode);
        });
        const logo = LOGO.map(row => {
            let line = "";
            let active = "";
            for (let col = 0; col < row.length; col++) {
                if (row[col] !== " " && columns[col] !== active) {
                    active = columns[col]; line += active;
                }
                line += row[col];
            }
            return line + (mode === "none" ? "" : RESET);
        });
        const letters = [..."PAVEWAY"].map((letter, index) =>
            columns[Math.round(index * (GRADIENT.length - 1) / 6)] + letter);
        const result = {logo, letters, progress};
        artFrames.set(step, result);
        return result;
    }

    function pathline(length, mode, ascii, progress) {
        if (length <= 0) return "";
        const travel = smooth((progress - 0.35) / 0.65);
        const dot = Math.round((length - 1) * 0.92 * travel);
        let result = "";
        let active = "";
        for (let col = 0; col < length; col++) {
            const illumination = smooth((travel - col / Math.max(1, length - 1)) * 6 + 1);
            const rgb = col === dot ? mix(DIM, STOPS[3], smooth((progress - 0.35) / 0.2)) : mix(DIM, RAIL, illumination);
            const next = color(rgb, mode);
            if (next !== active) { result += next; active = next; }
            result += col === dot ? (ascii ? "." : "●") : (ascii ? "-" : "─");
        }
        return result + (mode === "none" ? "" : RESET);
    }

    return function render(width, ompVersion, progress = 1) {
        width = Number.isFinite(width) ? Math.max(0, Math.floor(width)) : 0;
        if (width === 0) return [];
        const mode = colorMode(ui);
        const ascii = process.env.TERM === "dumb" || ui.theme.getSymbolPreset() === "ascii";
        const key = `${mode}:${ascii}`;
        if (key !== capability) { capability = key; artFrames = new Map(); resting = undefined; }
        const hostVersion = singleLine(ompVersion).replace(/^v/, "");
        const step = Math.round(clamp(Number.isFinite(progress) ? progress : 1) * STEPS);
        if (step === STEPS && resting?.width === width && resting.hostVersion === hostVersion) return resting.lines;
        const {logo, letters, progress: frameProgress} = artwork(mode, step);
        const available = Math.min(width > 2 ? width - 2 : width, 65);
        const wide = !ascii && available >= 65;
        const center = text => " ".repeat(Math.max(0, Math.floor((width - ui.visibleWidth(text)) / 2))) + text;
        const paint = (text, rgb, italic = false) => mode === "none" ? text
            : color(rgb, mode) + (italic ? "\x1b[3m" : "") + text + RESET;
        const line = (text, rgb, italic = false) => center(paint(shorten(text, available, ascii), rgb, italic));
        const smallLogo = letters.slice(0, Math.min(7, available)).join("") + (mode === "none" ? "" : RESET);
        const lines = wide ? logo.map(center) : [center(smallLogo)];
        lines.push("");
        lines.push(center(pathline(available, mode, ascii, frameProgress)));
        lines.push("");
        lines.push(line("OI COACHING RUNTIME", MUTED));
        const join = ascii ? " | " : " · ";
        const prefix = `Paveway ${pavewayVersion}`;
        const suffix = `OMP ${hostVersion}`;
        const remaining = available - ui.visibleWidth(prefix + join + join + suffix);
        if (workflowName && remaining >= 1) {
            lines.push(line(prefix + join + shorten(workflowName, remaining, ascii) + join + suffix, MUTED));
        } else if (ui.visibleWidth(prefix + join + suffix) <= available) {
            lines.push(line(prefix + join + suffix, MUTED));
            if (workflowName) lines.push(line(shorten(`Workflow ${workflowName}`, available, ascii), MUTED));
        } else {
            lines.push(line(shorten(prefix, available, ascii), MUTED));
            lines.push(line(shorten(suffix, available, ascii), MUTED));
            if (workflowName) lines.push(line(shorten(`Workflow ${workflowName}`, available, ascii), MUTED));
        }
        lines.push("");
        lines.push(line("Tip: Find the insight. Build the solution.", GOLD, true));
        if (step === STEPS) resting = {width, hostVersion, lines};
        return lines;
    };
}
