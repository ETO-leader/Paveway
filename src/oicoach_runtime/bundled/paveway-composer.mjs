import {presentationColor} from "./paveway-presentation.mjs";

const installations = new WeakMap();
let nextStyle = 0;

async function nativeCustomEditor(ui) {
    if (ui.CustomEditor) return ui.CustomEditor;
    const specifier = "@oh-my-pi/pi-tui/prompt/custom-editor";
    // Resolve from the host, not the extension's installation directory. This is
    // a documented package export, not a source-path/private-structure import.
    try { return (await import(Bun.resolveSync(specifier, process.argv[1]))).CustomEditor; }
    catch { return (await import(specifier)).CustomEditor; }
}

/**
 * Install Paveway's visual-only native composer in an interactive main context.
 *
 * `ui` is the public pi-tui module returned by loadNativeTui(). It may also
 * include the public CustomEditor export (useful for host embedding). Requires
 * the pinned host's opt-in CustomEditor.composerStyle seam in composer.patch.
 * Repeated calls for the same ctx.ui retain the editor, draft and registration.
 * The returned disposer restores the host factory only while this installation
 * still owns it. Callers should dispose on extension unload, not session switch.
 *
 * No input/render methods are overridden: native key handling, IME marker,
 * history, paste, wrapping, completion and viewport height budgeting stay native.
 */
export async function installComposer(ctx, ui) {
    const installed = installations.get(ctx.ui);
    if (installed) return installed;
    const pending = createInstallation(ctx, ui);
    installations.set(ctx.ui, pending);
    try { return await pending; }
    catch (error) { installations.delete(ctx.ui); throw error; }
}

async function createInstallation(ctx, ui) {
    const CustomEditor = await nativeCustomEditor(ui);
    const id = `paveway-native-${++nextStyle}`;
    let activeEditor;
    let activeTui;
    const paint = (token, text) => presentationColor(ui, token, text);
    const focused = () => activeEditor?.focused === true;
    const ascii = () => process.env.TERM === "dumb" || ui.theme?.getSymbolPreset?.() === "ascii";
    const style = {
        id,
        filledSurface: false,
        // A rail has no right frame for IME preedit to push. Native borderless
        // layout therefore owns both prompt-gutter width and end-of-line cursor.
        sideBorders: false,
        verticalChrome: 0,
        statusAttachment: "none",
        bottomBar: "full",
        bottomBarGap: false,
        defaultPromptGutter: "> ",
        defaultPaddingX: () => 1,
        // One rail column plus one padding column on the left; reserve exactly
        // the same width on the right. Never draw a glyph outside this budget.
        sideChromeWidth: paddingX => paddingX + 1,
        renderTop: () => undefined,
        renderBottom: () => undefined,
        renderRow(chrome) {
            const edge = paint(focused() ? "brand" : "border", ascii() ? "|" : "│");
            const gutter = paint(focused() ? "brand" : "muted", chrome.gutter);
            let fill = chrome.pad;
            const rows = activeEditor?.viewportRowsProvider?.() ?? activeTui?.terminal.rows ?? 24;
            if (focused() && chrome.isLastRow && chrome.width >= 64 && rows >= 12) {
                // Editor consumes this process-wide public registry too. Read it
                // at paint time, so rebinding/unbinding never leaves stale hints.
                const keys = ui.getKeybindings();
                const hints = [];
                const submit = keys.getKeys("tui.input.submit")[0];
                const newline = keys.getKeys("tui.input.newLine")[0];
                if (submit) hints.push(`${submit} send`);
                if (newline) hints.push(`${newline} newline`);
                const hint = hints.join(" · ");
                const cells = ui.visibleWidth(hint);
                // Only reuse the native row's spare cells. Neither a cursor nor
                // user text is shortened, and hints consume no additional rows.
                if (hint && fill.length >= cells + 3)
                    fill = " ".repeat(fill.length - cells) + paint("muted", hint);
            }
            const inset = " ".repeat(chrome.paddingX);
            const right = " ".repeat(Math.max(0, chrome.paddingX + 1 - chrome.cursorOverflow));
            return [edge + inset + gutter + chrome.text + fill + right];
        },
    };
    const unregister = ui.registerComposerStyle(style);
    class PavewayEditor extends CustomEditor {
        constructor(tui, theme, keybindings) {
            super(tui, {...theme, textColor: text => paint("ink", text)}, keybindings);
            this.composerStyle = id;
            this.setBorderStyle(id);
            this.setPaddingX(1);
            this.setPromptGutter("> ");
            activeEditor = this;
            activeTui = tui;
        }
    }
    const factory = (tui, theme, keys) => new PavewayEditor(tui, theme, keys);
    try { ctx.ui.setEditorComponent(factory); }
    catch (error) { unregister(); throw error; }
    let disposed = false;
    return () => {
        if (disposed) return;
        disposed = true;
        if (ctx.ui.getEditorComponent() === factory) ctx.ui.setEditorComponent(undefined);
        unregister();
        installations.delete(ctx.ui);
    };
}
