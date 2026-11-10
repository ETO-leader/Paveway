// Scoped chat/composer colors. Status-line surfaces and the user's OMP theme stay untouched.
const TOKENS = Object.freeze({
    brand: ["#8475cc", 98],
    border: ["#49415e", 60],
    muted: ["#8d899b", 245],
    ink: ["#dae7f3", 189],
});
const COLORS = Object.fromEntries(Object.entries(TOKENS).map(([name, [hex, index]]) => [
    name, {
        truecolor: `\x1b[38;2;${[1, 3, 5].map(offset => Number.parseInt(hex.slice(offset, offset + 2), 16)).join(";")}m`,
        indexed: `\x1b[38;5;${index}m`,
    },
]));
export function presentationColor(ui, token, text) {
    const color = COLORS[token];
    const mode = ui.theme.getColorMode();
    if (mode === "none" || mode === false || process.env.NO_COLOR !== undefined ||
        process.env.FORCE_COLOR === "0" || process.env.TERM === "dumb") return text;
    const escape = mode === "truecolor" ? color.truecolor : color.indexed;
    return escape + text.replace(/\x1b\[(?:0|39)m/g, "$&" + escape) + "\x1b[39m";
}
