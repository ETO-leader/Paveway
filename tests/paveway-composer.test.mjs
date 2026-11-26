import {expect, test} from "bun:test";
import {PAVEWAY_COMPOSER_SHAPE, registerComposer} from "../src/oicoach_runtime/bundled/paveway-composer.mjs";

function registeredStyle() {
    let shape;
    registerComposer({registerComposerShape(definition) { shape = definition; }});
    expect(shape.label).toBe("Paveway Chrome");
    expect(shape.style.id).toBe("paveway.chrome");
    expect(shape.style.id).toBe(PAVEWAY_COMPOSER_SHAPE);
    return shape.style;
}

test("Paveway Chrome is a transparent rail with the full native status bar", () => {
    const style = registeredStyle();
    expect(style.filledSurface).toBe(false);
    expect(style.sideBorders).toBe(false);
    expect(style.verticalChrome).toBe(0);
    expect(style.bottomBar).toBe("full");
    expect(style.defaultPromptGutter).toBe("> ");
    expect(style.defaultPaddingX()).toBe(1);
    expect(style.defaultPaddingX(2)).toBe(2);
    expect(style.renderTop()).toBeUndefined();
    expect(style.renderBottom()).toBeUndefined();
});

test("Paveway Chrome spends only its declared cells and preserves native input decorations", () => {
    const style = registeredStyle();
    const marker = "\x1b_pi:c\x07";
    const context = {
        box: {vertical: "│"}, accentColor: text => `\x1b[36m${text}\x1b[39m`,
        gutter: "❯ ", text: `中🙂e\u0301${marker}`, pad: "    ",
        cursorOverflow: 1, paddingX: 2,
    };
    const expected = "│  ❯ 中🙂e\u0301      ";
    const [row] = style.renderRow({...context, width: Bun.stringWidth(expected)});
    expect(row.startsWith("\x1b[36m│\x1b[39m")).toBe(true);
    expect(row).toContain(marker);
    expect(Bun.stripANSI(row.replace(marker, ""))).toBe(expected);
    expect(style.sideChromeWidth(context.paddingX)).toBe(3);
    expect(style.renderRow({...context, imeSafeCursorTail: true})[0].endsWith(marker)).toBe(true);
});

test("the rail uses the host symbol preset", () => {
    const [row] = registeredStyle().renderRow({
        width: 9, box: {vertical: "|"}, accentColor: text => text,
        gutter: "> ", text: "draft", pad: "", cursorOverflow: 0, paddingX: 0,
    });
    expect(row).toBe("|> draft ");
});

test("OMP 19.2 registers the flat shape and paints exactly the declared rail geometry", () => {
    let shape;
    registerComposer({registerComposerShape(definition) {
        expect(definition.id).toBe(PAVEWAY_COMPOSER_SHAPE);
        expect(typeof definition.row).toBe("function");
        expect(typeof definition.sideWidth).toBe("function");
        shape = definition;
    }});
    expect(shape.label).toBe("Paveway Chrome");
    expect(shape.filled).toBe(false);
    expect(shape.sideBorders).toBe(false);
    expect(shape.bottomBar).toBe("full");
    expect(shape.statusAttachment).toBeUndefined();
    expect(shape.top).toBeUndefined();
    expect(shape.bottom).toBeUndefined();
    expect(shape.gutter).toBe("> ");
    expect(shape.paddingX).toBe(1);
    expect(shape.sideWidth(2)).toBe(3);
    const expected = "|  > 中🙂e\u0301      ";
    const [row] = shape.row({
        width: Bun.stringWidth(expected), paddingX: 2, overflow: 1,
        box: {vertical: "|"}, accent: value => `\x1b[36m${value}\x1b[39m`,
        gutter: "> ", text: "中🙂e\u0301", pad: "    ",
    });
    expect(row.startsWith("\x1b[36m|\x1b[39m")).toBe(true);
    expect(Bun.stripANSI(row)).toBe(expected);
});
