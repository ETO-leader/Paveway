// User-command-only settings. Business I/O runs in the controller, never in render().
export async function loadNativeTui() {
    try { return await import(Bun.resolveSync("@oh-my-pi/pi-tui", process.argv[1])); }
    catch { return import("@oh-my-pi/pi-tui"); } // Compiled OMP resolves this to its bundled host module.
}

const permissions = [
    {value: "none", label: "None", description: "Read only: query, search, statements and history."},
    {value: "managed", label: "Managed", description: "Recommended: read + problem/content/embedding ensure; no direct edits or deletion."},
    {value: "full", label: "Full", description: "All MCP tools, including direct changes and deletion."},
];
const permissionLabel = value => permissions.find(item => item.value === value)?.label || "Unknown";
const restartNotice = "Saved. Restart the Paveway native OMP session to apply Tijian MCP changes.";

export async function openSettings(ctx, {cli, workflowId, preferences, model, thinking}, initialPage = "settings") {
    // RPC can have hasUI=true for protocol dialogs, but cannot mount custom components.
    if (!ctx.hasUI || !process.stdin.isTTY || !process.stdout.isTTY || ctx.agent?.kind !== "main") {
        ctx.ui.notify("Paveway settings require the interactive main OMP terminal.", "warning");
        return;
    }
    const ui = await loadNativeTui();
    // Older source installs export the adapters from the agent, newer OMP from pi-tui.
    const themes = ui.getSettingsListTheme ? ui : await import(Bun.resolveSync("@oh-my-pi/pi-coding-agent", process.argv[1]));
    const notifyError = error => ctx.ui.notify(error.message || "Cannot update settings. Review the configuration and retry.", "error");
    const busy = async (message, action) => {
        const result = await ctx.ui.custom((_tui, theme, _keys, done) => {
            // Keep focus away from chat while CLI I/O is pending. An in-flight save is not cancellable.
            Promise.resolve().then(action).then(value => done({value}), error => done({error}));
            return new ui.Text(theme.fg("muted", message));
        });
        if (result.error) throw result.error;
        return result.value;
    };
    const panel = (tui, theme, keybindings, done, title, list) => {
        let disposed = false;
        return {
            render(width) {
                return [theme.fg("accent", title), "", ...list.render(width)]
                    .map(line => ui.truncateToWidth(ui.replaceTabs(line), width));
            },
            handleInput(data) {
                if (disposed) return;
                if (keybindings.matches(data, "app.interrupt")) done(undefined);
                else list.handleInput(data);
                tui.requestRender();
            },
            invalidate() { list.invalidate?.(); },
            dispose() {
                if (disposed) return;
                disposed = true;
                list.onSelect = undefined;
                list.onCancel = undefined;
                list.dispose?.();
            },
        };
    };
    const settingsPage = (title, rows, selected) => ctx.ui.custom((tui, theme, keys, done) => {
        // A single-value row activates the controller without speculatively changing its saved value.
        const items = rows.map(row => row.editable === false ? row : {...row, values: [row.currentValue]});
        const list = new ui.SettingsList(items, items.length, themes.getSettingsListTheme(),
            id => done(id), () => done(undefined));
        if (selected) list.selectItem(selected);
        return panel(tui, theme, keys, done, title, list);
    });
    const permissionPicker = current => ctx.ui.custom((tui, theme, keys, done) => {
        const list = new ui.SelectList(permissions, permissions.length, themes.getSelectListTheme(),
            {wrapDescription: true});
        list.setSelectedIndex(Math.max(0, permissions.findIndex(item => item.value === current)));
        list.onSelect = item => done(item.value);
        list.onCancel = () => done(undefined);
        return panel(tui, theme, keys, done, "Tijian service permission", list);
    });
    const tijianPage = async () => {
        let selected = "executable";
        let draftPermission = "managed";
        while (true) {
            let raw, readError;
            try { raw = await busy("Reading Tijian MCP configuration…", () => cli(["workflow", "tijian", "show", workflowId])); }
            catch (error) { readError = error; }
            const currentPermission = raw?.permission || draftPermission;
            const row = await settingsPage("Paveway / Settings / Tijian MCP", [
                {id: "executable", label: "MCP executable", currentValue: raw?.executable || (readError ? "Unavailable" : "Not configured"),
                    description: "Enter a replacement absolute tijian-mcp path; empty keeps the saved path. Permission is preserved."},
                {id: "permission", label: "Permission", currentValue: readError ? "Unknown" : permissionLabel(currentPermission),
                    description: readError ? readError.message : permissions.find(item => item.value === currentPermission)?.description},
                {id: "configuration", label: "Configuration", editable: false,
                    currentValue: readError ? "Error — not modified" : raw.configured ? "Saved (restart required)" : "Not configured",
                    description: readError ? readError.message : raw.configured ?
                        "Saved configuration only. Running MCP permissions are unknown; restart the Paveway native OMP session to apply changes." :
                        "Choose an MCP executable to save. Managed is recommended for first setup; an unsaved permission choice is discarded on Back."},
            ], selected);
            if (!row) return;
            selected = row;
            try {
                if (readError) throw readError;
                let flags;
                if (row === "executable") {
                    // OMP input's second argument is a placeholder, not an editable initial value.
                    const executable = await ctx.ui.input("Absolute native tijian-mcp executable path",
                        raw.configured ? "Leave empty to keep the saved executable" : "/absolute/path/to/tijian-mcp");
                    if (executable === undefined || (executable === "" && raw.configured)) continue;
                    flags = ["--executable", executable];
                    if (!raw.configured) flags.push("--permission", draftPermission);
                } else if (row === "permission") {
                    const permission = await permissionPicker(currentPermission);
                    if (permission === undefined || permission === currentPermission) continue;
                    if (!raw.configured) {
                        draftPermission = permission;
                        continue;
                    }
                    flags = ["--permission", permission];
                } else continue;
                const targetPermission = row === "permission" ? flags[1] : currentPermission;
                if (targetPermission === "full" && raw.permission !== "full") {
                    if (!await ctx.ui.confirm("Enable Full Tijian access?",
                        "Full allows all MCP tools, including direct edits and deletion. Restart is required to apply this permission.")) continue;
                    flags.push("--confirm-full");
                }
                const result = await busy("Saving Tijian MCP configuration…",
                    () => cli(["workflow", "tijian", "set", workflowId, "--expected-revision", raw.revision, ...flags]));
                ctx.ui.notify(result.changed ? restartNotice : "No changes. Saved configuration is unchanged.", "info");
            } catch (error) { notifyError(error); }
        }
    };
    let page = initialPage;
    let selected = "tijian";
    while (true) {
        if (page === "tijian") {
            await tijianPage();
            page = "settings";
        }
        let raw;
        try { raw = await busy("Reading Paveway preferences…", preferences); }
        catch (error) { notifyError(error); }
        const inference = raw?.inference || {};
        const row = await settingsPage("Paveway / Settings", [
            {id: "tijian", label: "Tijian MCP", currentValue: "Configure", description: "Executable and None / Managed / Full permissions; restart required after saving."},
            {id: "model", label: "Model preference", currentValue: inference.model || "inherit", description: "Portable model preference. Applies on the next native launch."},
            {id: "thinking", label: "Thinking preference", currentValue: inference.thinking || "inherit", description: "Portable thinking preference. Applies on the next native launch."},
        ], selected);
        if (!row) return;
        selected = row;
        if (row === "tijian") page = "tijian";
        else {
            try { await (row === "model" ? model : thinking)(ctx); }
            catch (error) { notifyError(error); }
        }
    }
}
