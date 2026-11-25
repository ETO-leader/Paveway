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
const MUTED = [128, 143, 165];
const GOLD = [220, 182, 119];
const RESET = "\x1b[0m";
const mix = (a, b, amount) => a.map((value, index) => Math.round(value + (b[index] - value) * amount));
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
function colorMode(theme) {
    if (process.env.NO_COLOR !== undefined || process.env.FORCE_COLOR === "0" || process.env.TERM === "dumb") return "none";
    const mode = theme.getColorMode();
    return mode === "none" || mode === false ? "none" : mode === "truecolor" ? "truecolor" : "ansi256";
}
function singleLine(value) {
    return String(value ?? "").replace(/[\r\n\t]/g, " ").replace(/[\x00-\x1f\x7f-\x9f]/g, "").trim();
}
const versionLabel = value => value.startsWith("v") ? value : `v${value}`;

const visibleWidth = text => Bun.stringWidth(Bun.stripANSI(text));
const graphemes = new Intl.Segmenter(undefined, {granularity: "grapheme"});

function clip(text, width) {
    let cells = 0;
    for (const {segment, index} of graphemes.segment(text)) {
        cells += Bun.stringWidth(segment);
        if (cells > width) return text.slice(0, index);
    }
    return text;
}

/** Render the original wordmark and pathline at rest, independently of startup timing. */
export function createBanner({version, workflow}, theme) {
    const pavewayVersion = versionLabel(singleLine(version));
    const workflowName = singleLine(workflow);
    let art;
    let resting;
    const shorten = (text, width, ascii) => visibleWidth(text) <= width ? text
        : width < 2 ? clip(text, width) : clip(text, width - 1) + (ascii ? "~" : "…");

    function artwork(mode) {
        if (art?.mode === mode) return art;
        const columns = GRADIENT.map(rgb => color(rgb, mode));
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
        art = {mode, logo, letters};
        return art;
    }

    function pathline(length, mode, ascii) {
        const dot = Math.round((length - 1) * 0.92);
        const rail = color(RAIL, mode);
        const rule = ascii ? "-" : "─";
        return rail + rule.repeat(dot) + color(STOPS[3], mode) + (ascii ? "." : "●") +
            rail + rule.repeat(length - dot - 1) + (mode === "none" ? "" : RESET);
    }

    return function render(width, ompVersion) {
        width = Number.isFinite(width) ? Math.max(0, Math.floor(width)) : 0;
        if (width === 0) return [];
        const mode = colorMode(theme);
        const ascii = process.env.TERM === "dumb" || theme.getSymbolPreset() === "ascii";
        const key = `${mode}:${ascii}`;
        const hostVersion = singleLine(ompVersion).replace(/^v/, "");
        if (resting?.key === key && resting.width === width && resting.hostVersion === hostVersion) return resting.lines;
        const {logo, letters} = artwork(mode);
        const available = Math.min(width > 2 ? width - 2 : width, 65);
        const wide = !ascii && available >= 65;
        const center = text => " ".repeat(Math.max(0, Math.floor((width - visibleWidth(text)) / 2))) + text;
        const paint = (text, rgb, italic = false) => mode === "none" ? text
            : color(rgb, mode) + (italic ? "\x1b[3m" : "") + text + RESET;
        const line = (text, rgb, italic = false) => center(paint(shorten(text, available, ascii), rgb, italic));
        const smallLogo = letters.slice(0, Math.min(7, available)).join("") + (mode === "none" ? "" : RESET);
        const lines = wide ? logo.map(center) : [center(smallLogo)];
        lines.push("");
        lines.push(center(pathline(available, mode, ascii)));
        lines.push("");
        lines.push(line("OI TRAINING RUNTIME", MUTED));
        const join = ascii ? " | " : " · ";
        const prefix = `Paveway ${pavewayVersion}`;
        const suffix = `OMP ${hostVersion}`;
        const remaining = available - visibleWidth(prefix + join + join + suffix);
        if (workflowName && remaining >= 1) {
            lines.push(line(prefix + join + shorten(workflowName, remaining, ascii) + join + suffix, MUTED));
        } else if (visibleWidth(prefix + join + suffix) <= available) {
            lines.push(line(prefix + join + suffix, MUTED));
            if (workflowName) lines.push(line(shorten(`Workflow ${workflowName}`, available, ascii), MUTED));
        } else {
            lines.push(line(shorten(prefix, available, ascii), MUTED));
            lines.push(line(shorten(suffix, available, ascii), MUTED));
            if (workflowName) lines.push(line(shorten(`Workflow ${workflowName}`, available, ascii), MUTED));
        }
        lines.push("");
        lines.push(line("Tip: Find the insight. Build the solution.", GOLD, true));
        resting = {key, width, hostVersion, lines};
        return lines;
    };
}
