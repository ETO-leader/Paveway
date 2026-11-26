export const PAVEWAY_COMPOSER_SHAPE = "paveway.chrome";

export function registerComposer(pi) {
    if (!pi.registerComposerShape) return;
    const sideWidth = paddingX => paddingX + 1;
    const railRow = ({box, gutter, pad, paddingX, text}, accent, overflow, imeSafeCursorTail) => {
        const input = accent(box.vertical) + " ".repeat(paddingX) + gutter + text;
        const tail = imeSafeCursorTail ? "" : pad + " ".repeat(Math.max(0, sideWidth(paddingX) - overflow));
        return [input + tail];
    };
    // The old host reads definition.style; the node host reads the flat
    // shape. Both register the same ID and ignore the other layout fields.
    pi.registerComposerShape({
        id: PAVEWAY_COMPOSER_SHAPE,
        label: "Paveway Chrome",
        description: "Transparent input with an accent rail and the native status bar",
        filled: false,
        sideBorders: false,
        bottomBar: "full",
        bottomBarGap: false,
        gutter: "> ",
        paddingX: 1,
        sideWidth,
        row: cx => railRow(cx, cx.accent, cx.overflow, false),
        style: {
            id: PAVEWAY_COMPOSER_SHAPE,
            filledSurface: false,
            sideBorders: false,
            verticalChrome: 0,
            statusAttachment: "none",
            bottomBar: "full",
            bottomBarGap: false,
            defaultPromptGutter: "> ",
            defaultPaddingX: themePaddingX => themePaddingX ?? 1,
            sideChromeWidth: sideWidth,
            renderTop: () => undefined,
            renderBottom: () => undefined,
            renderRow: cx => railRow(cx, cx.accentColor, cx.cursorOverflow, cx.imeSafeCursorTail),
        },
    });
}
