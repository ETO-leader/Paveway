export const PAVEWAY_COMPOSER_SHAPE = "paveway.chrome";

export function registerComposer(pi) {
    pi.registerComposerShape?.({
        label: "Paveway Chrome",
        description: "Transparent input with an accent rail and the native status bar",
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
            sideChromeWidth: paddingX => paddingX + 1,
            renderTop: () => undefined,
            renderBottom: () => undefined,
            renderRow({accentColor, box, cursorOverflow, gutter, imeSafeCursorTail, pad, paddingX, text}) {
                const input = accentColor(box.vertical) + " ".repeat(paddingX) + gutter + text;
                const tail = imeSafeCursorTail ? "" : pad + " ".repeat(Math.max(0, paddingX + 1 - cursorOverflow));
                return [input + tail];
            },
        },
    });
}
