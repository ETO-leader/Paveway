import {afterEach, beforeAll, beforeEach, describe, expect, test} from "bun:test";
import {installComposer} from "../src/oicoach_runtime/bundled/paveway-composer.mjs";

// Run against an installed, patched host, never a replacement editor:
// PAVEWAY_OMP_SOURCE=/path/to/prepared/source bun test tests/paveway-composer.test.mjs
const source = process.env.PAVEWAY_OMP_SOURCE;
const fromHost = specifier => import(Bun.resolveSync(specifier, `${source}/packages/tui/package.json`));
const native = source ? await fromHost("@oh-my-pi/pi-tui") : undefined;
const {CustomEditor} = source ? await fromHost("@oh-my-pi/pi-tui/prompt/custom-editor") : {};
const previousKeys = native?.getKeybindings();
const colorEnvironment = Object.fromEntries(["NO_COLOR", "FORCE_COLOR", "TERM"].map(key => [key, process.env[key]]));
const disposers = [];
const text = row => Bun.stripANSI(row.replaceAll(native.CURSOR_MARKER, ""));
const width = row => native.visibleWidth(row.replaceAll(native.CURSOR_MARKER, ""));
const cursor = frame => {
    const row = frame.findIndex(line => line.includes(native.CURSOR_MARKER));
    return row < 0 ? undefined : {row, col: native.visibleWidth(frame[row].slice(0, frame[row].indexOf(native.CURSOR_MARKER)))};
};

beforeAll(async () => { if (source) await native.initTheme(); });
beforeEach(() => {
    if (!source) return;
    delete process.env.NO_COLOR;
    delete process.env.FORCE_COLOR;
    process.env.TERM = "xterm-256color";
    native.setKeybindings(new native.KeybindingsManager(native.TUI_KEYBINDINGS));
});
afterEach(() => {
    if (!source) return;
    for (const dispose of disposers.splice(0)) dispose();
    native.setKeybindings(previousKeys);
    for (const [key, value] of Object.entries(colorEnvironment)) {
        if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
});

async function editorHost({rows = 30, mode = "truecolor"} = {}) {
    const ui = {...native, CustomEditor, theme: {getColorMode: () => mode, getSymbolPreset: () => "unicode"}};
    const tui = {terminal: {rows, columns: 80}, requestRender() {}};
    let editor;
    let factory;
    const ctx = {ui: {
        getEditorComponent: () => factory,
        setEditorComponent(next) {
            factory = next;
            editor = next ? next(tui, native.getEditorTheme(), native.getKeybindings()) : undefined;
            if (!editor) return;
            // Same public opt-in shape selection the host applies after invoking
            // the factory. Everything under test below is the real CustomEditor.
            editor.setBorderStyle(editor.composerStyle ?? "box");
            editor.setUseTerminalCursor(true);
            editor.viewportRowsProvider = () => tui.terminal.rows;
            editor.setMaxHeight(18);
            editor.focused = true;
        },
    }};
    disposers.push(await installComposer(ctx, ui));
    return {editor, ui, ctx, tui, factory, getEditor: () => editor};
}

describe.skipIf(!source)("Paveway native composer", () => {
    test("keeps native cursor/inset geometry", async () => {
        const {editor} = await editorHost();
        editor.setText("中🙂e\u0301");
        const frame = editor.render(80);
        expect(frame).toHaveLength(1);
        expect(frame.every(row => width(row) === 80)).toBe(true);
        expect(text(frame[0]).startsWith("│ > 中🙂e\u0301")).toBe(true);
        expect(cursor(frame)).toEqual({row: 0, col: 9});
    });

    test("focus changes the rail without shifting text and retains the native IME marker", async () => {
        const {editor} = await editorHost();
        editor.setText("draft");
        const focused = editor.render(80);
        expect(focused[0]).toContain("\x1b[38;2;132;117;204m");
        expect(cursor(focused)).toEqual({row: 0, col: 9});
        editor.focused = false;
        const blurred = editor.render(80);
        expect(blurred[0]).toContain("\x1b[38;2;73;65;94m");
        expect(cursor(blurred)).toBeUndefined();
        expect(text(blurred[0]).startsWith("│ > draft")).toBe(true);
        editor.focused = true;
        expect(cursor(editor.render(80))).toEqual({row: 0, col: 9});
        // Marker placement is testable; actual terminal-local IME candidate
        // windows/preedit (including CJK input methods) require a real terminal.
    });

    test("bracketed paste, multiline input, submit and action keys stay native", async () => {
        const {editor} = await editorHost();
        editor.handleInput("\x1b[200~first\n中🙂\x1b[201~");
        expect(editor.getText()).toBe("first\n中🙂");
        expect(editor.getCursor()).toEqual({line: 1, col: 3});
        editor.handleInput("\x0a");
        editor.handleInput("last");
        expect(editor.getText()).toBe("first\n中🙂\nlast");
        expect(editor.render(80)).toHaveLength(3);
        let submitted;
        editor.onSubmit = value => { submitted = value; };
        editor.handleInput("\r");
        expect(submitted).toBe("first\n中🙂\nlast");
        editor.setActionKeys("app.clear", ["alt+k"]);
        let cleared = 0;
        editor.onClear = () => { cleared++; };
        editor.handleInput("\x1bk");
        expect(cleared).toBe(1);
    });

    test("wrapping, wide graphemes and viewport caps survive resize without changing the draft", async () => {
        const {editor, tui} = await editorHost();
        const draft = "中🙂e\u0301".repeat(30) + "\nlast";
        editor.setText(draft);
        const before = editor.getCursor();
        editor.setMaxHeight(6);
        for (const columns of [80, 24, 12, 100, 20]) {
            tui.terminal.columns = columns;
            const frame = editor.render(columns);
            expect(frame.length).toBeLessThanOrEqual(6);
            expect(frame.every(row => width(row) === columns)).toBe(true);
            expect(cursor(frame)).toBeDefined();
            expect(editor.getText()).toBe(draft);
            expect(editor.getCursor()).toEqual(before);
        }
        editor.setText("x".repeat(20));
        expect(editor.render(14)).toHaveLength(3);
        expect(editor.render(80)).toHaveLength(1);
        tui.terminal.rows = 8;
        editor.setMaxHeight(4);
        editor.setText(Array.from({length: 20}, (_, index) => `line ${index}`).join("\n"));
        expect(editor.render(80)).toHaveLength(4);
        expect(text(editor.render(80).at(-1))).toContain("line 19");
    });

    test("history recall and navigation retain native content and cursor positions", async () => {
        const {editor} = await editorHost();
        editor.addToHistory("previous\nmessage");
        editor.setText("");
        editor.handleInput("\x1b[A");
        expect(editor.getText()).toBe("previous\nmessage");
        editor.handleInput("\x1b[H");
        const position = editor.getCursor();
        expect(position.col).toBe(0);
        expect(editor.render(80).every(row => width(row) === 80)).toBe(true);
    });

    test("hints reflect current bound keys, disappear when unbound or space constrained", async () => {
        const {editor, tui} = await editorHost();
        expect(text(editor.render(80)[0])).toContain("enter send · shift+enter newline");
        native.setKeybindings(new native.KeybindingsManager(native.TUI_KEYBINDINGS, {
            "tui.input.submit": ["ctrl+s"], "tui.input.newLine": ["ctrl+n"],
        }));
        expect(text(editor.render(80)[0])).toContain("ctrl+s send · ctrl+n newline");
        editor.handleInput("a");
        editor.handleInput("\x0e");
        editor.handleInput("b");
        expect(editor.getText()).toBe("a\nb");
        let submitted;
        editor.onSubmit = value => { submitted = value; };
        editor.handleInput("\x13");
        expect(submitted).toBe("a\nb");
        editor.setText("");
        expect(text(editor.render(50)[0])).not.toContain(" send");
        tui.terminal.rows = 8;
        expect(text(editor.render(80)[0])).not.toContain(" send");
        tui.terminal.rows = 30;
        editor.setText("x".repeat(70));
        expect(text(editor.render(80)[0])).not.toContain(" send");
        editor.setText("");
        native.setKeybindings(new native.KeybindingsManager(native.TUI_KEYBINDINGS, {
            "tui.input.submit": [], "tui.input.newLine": [],
        }));
        expect(text(editor.render(80)[0])).not.toContain(" send");
        expect(text(editor.render(80)[0])).not.toContain(" newline");
    });

    test("native async completion list, selection and application remain functional", async () => {
        const {editor} = await editorHost();
        const shown = Promise.withResolvers();
        editor.onAutocompleteUpdate = () => { if (editor.isShowingAutocomplete()) shown.resolve(); };
        editor.setAutocompleteProvider({
            async getSuggestions(lines, line, col) {
                const prefix = lines[line].slice(0, col);
                return prefix.startsWith("@") ? {prefix, items: [
                    {label: "alpha.ts", value: "@alpha.ts"}, {label: "beta.ts", value: "@beta.ts"},
                ]} : null;
            },
            applyCompletion(lines, line, col, item, prefix) {
                const replacement = item.value + " ";
                const next = [...lines];
                next[line] = lines[line].slice(0, col - prefix.length) + replacement + lines[line].slice(col);
                return {lines: next, cursorLine: line, cursorCol: col - prefix.length + replacement.length};
            },
        });
        editor.handleInput("@");
        await shown.promise;
        expect(editor.isShowingAutocomplete()).toBe(true);
        const frame = editor.render(80);
        expect(frame.length).toBeGreaterThan(1);
        expect(cursor(frame)).toEqual({row: 0, col: 5});
        expect(frame.some(row => text(row).includes("alpha.ts"))).toBe(true);
        editor.handleInput("\x1b[B");
        editor.handleInput("\t");
        expect(editor.getText()).toBe("@beta.ts ");
        expect(editor.getCursor()).toEqual({line: 0, col: 9});
        expect(editor.isShowingAutocomplete()).toBe(false);
    });

    test.each(["NO_COLOR", "FORCE_COLOR", "TERM"])("respects %s without adding color or background fills", async variable => {
        process.env[variable] = variable === "TERM" ? "dumb" : variable === "FORCE_COLOR" ? "0" : "1";
        const {editor} = await editorHost();
        editor.setText("plain draft");
        const frame = editor.render(80);
        expect(frame.join("\n")).not.toMatch(/\x1b\[(?:38|48);/);
        expect(text(frame[0])).toContain("> plain draft");
    });

    test("256-color mode is explicit and the composer remains transparent", async () => {
        const {editor} = await editorHost({mode: "256color"});
        editor.setText("draft");
        const frame = editor.render(80).join("\n");
        expect(frame).toContain("\x1b[38;5;98m");
        expect(frame).not.toMatch(/\x1b\[48;/);
        expect(frame).not.toMatch(/\x1b\[38;2;/);
    });

    test("session-ready reinstallation retains factory, text and owned registration", async () => {
        const {editor, ctx, ui, factory, getEditor} = await editorHost();
        editor.setText("keep draft");
        const first = await installComposer(ctx, ui);
        const again = await installComposer(ctx, ui);
        expect(again).toBe(first);
        expect(ctx.ui.getEditorComponent()).toBe(factory);
        expect(getEditor()).toBe(editor);
        expect(editor.getText()).toBe("keep draft");
        first();
        expect(ctx.ui.getEditorComponent()).toBeUndefined();
        expect(ui.getComposerStyle(editor.composerStyle).id).toBe("box");
    });
});
