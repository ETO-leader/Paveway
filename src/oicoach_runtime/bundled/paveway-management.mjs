// User-only dialogs. Private prompt/Skill/credential values never become chat.
import {randomUUID} from "node:crypto";
import {writeFileSync, unlinkSync} from "node:fs";
import {dirname, join} from "node:path";

export function registerManagement(pi, config, requestSwitch) {
    const drafts = new Map();
    const cli = async args => {
        const result = await pi.exec(config.cli_prefix[0], [...config.cli_prefix.slice(1), ...args,
            "--config", config.config_path, "--json"], {timeout: 30000});
        if (result.code) {
            let message = "Operation failed or outcome is unknown; inspect state before retrying";
            try { message = JSON.parse(result.stderr).error.message; } catch {}
            throw new Error(message);
        }
        try { return JSON.parse(result.stdout); }
        catch { throw new Error("Cannot decode CLI result; inspect state before retrying"); }
    };
    const privateInput = async (content, action) => {
        const path = join(dirname(process.env.PAVEWAY_NATIVE_DATA), "ui-" + randomUUID() + ".txt");
        writeFileSync(path, content, {mode: 0o600, flag: "wx"});
        try { return await action(path); }
        finally { try { unlinkSync(path); } catch {} }
    };
    const show = (ctx, value) => ctx.ui.notify(typeof value === "string" ? value : JSON.stringify(value, null, 2), "info");
    const settings = async () => (await cli(["workflow", "customize", "show", config.workflow_id])).settings;
    const changed = ctx => show(ctx, "Saved. Prompt/Skills apply on the next native launch; Persona requires a new conversation. Current turn is unchanged.");
    const secretInput = async (ctx, title) => {
        // Some installed OMP versions silently ignore ui.input({secret:true}).
        // Use the host's actual masked Input, and verify it before accepting data.
        const {Input} = await import(Bun.resolveSync("@oh-my-pi/pi-tui", process.argv[1]));
        const input = new Input();
        if (!("mask" in input)) throw new Error("This OMP build has no verified masked input; use the protected legacy login UI");
        input.mask = true;
        input.setValue("PAVEWAY_MASK_PROBE_6372");
        if (input.render(80).join("").includes("PAVEWAY_MASK_PROBE_6372"))
            throw new Error("Masked input verification failed; no credential was requested");
        input.setValue("");
        return ctx.ui.custom((tui, theme, _keys, done) => {
            input.onSubmit = value => { input.setValue(""); done(value); };
            input.onEscape = () => { input.setValue(""); done(undefined); };
            return {
                get focused() { return input.focused; },
                set focused(value) { input.focused = value; },
                render(width) { return [theme.fg("accent", title), ...input.render(width), "Enter submit · Esc cancel"]; },
                invalidate() { input.invalidate(); },
                handleInput(data) { input.handleInput(data); tui.requestRender(); },
            };
        });
    };
    const edit = async (ctx, key, title, original) => {
        const value = await ctx.ui.editor(title, drafts.get(key) ?? original);
        if (value !== undefined) drafts.set(key, value);
        return value;
    };
    const actions = {};
    actions.prompt = async ctx => {
        const raw = await settings();
        const mode = await ctx.ui.select("Prompt", ["show", "append", "replace"]);
        if (mode === "show") return show(ctx, raw.system_prompt.text || "No custom prompt");
        if (!mode) return;
        const value = await edit(ctx, "prompt", "Custom user system prompt", raw.system_prompt.text);
        if (value === undefined) return;
        await privateInput(value, file => cli(["workflow", "customize", "prompt", config.workflow_id,
            "--mode", mode, "--expected-revision", String(raw.revision), "--content-file", file]));
        drafts.delete("prompt"); changed(ctx);
    };
    actions.persona = async ctx => {
        const raw = await settings();
        const value = await edit(ctx, "persona", "GPT Persona — new conversation activation", raw.persona || "");
        if (value === undefined) return;
        await privateInput(value, file => cli(["coach", "persona", config.coach_id, "--expected-revision", String(raw.revision), "--content-file", file]));
        drafts.delete("persona"); changed(ctx);
    };
    actions.skills = async ctx => {
        const raw = await settings();
        const labels = raw.skills.map(skill => `${skill.name} · ${skill.category || "personal-skill"} · ${skill.enabled ? "on" : "off"}`);
        const picked = await ctx.ui.select("User Skills", ["Create Skill", ...labels]);
        if (!picked) return;
        const existing = raw.skills[labels.indexOf(picked)];
        let operation = existing ? await ctx.ui.select(existing.name, ["edit", "show", "toggle", "delete"]) : "edit";
        if (!operation) return;
        if (operation === "show") return show(ctx, existing.content);
        const name = existing?.name || await ctx.ui.input("Skill name (lowercase words separated by hyphens)");
        if (!name) return;
        const base = ["workflow", "customize"];
        if (operation === "delete" || operation === "toggle") {
            if (operation === "delete" && !await ctx.ui.confirm("Delete Skill?", name)) return;
            const action = operation === "delete" ? "skill-delete" : existing.enabled ? "skill-disable" : "skill-enable";
            await cli([...base, action, config.workflow_id, "--name", name, "--expected-revision", String(raw.revision)]);
        } else {
            const categories = ["oi-introduce", "user-information", "personal-skill"];
            const currentCategory = existing?.category || "personal-skill";
            const category = await ctx.ui.select("Skill category", existing ?
                [currentCategory, ...categories.filter(value => value !== currentCategory)] : categories);
            if (!category) return;
            const description = await ctx.ui.editor("When should GPT read this Skill?", existing?.description || "");
            if (description === undefined) return;
            const value = await edit(ctx, "skill:" + name, "Skill body (loaded on demand)", existing?.content || "");
            if (value === undefined) return;
            await privateInput(value, body => privateInput(description, desc => cli([...base, "skill-put", config.workflow_id,
                "--name", name, "--category", category, "--description-file", desc, "--content-file", body,
                "--expected-revision", String(raw.revision)])));
            drafts.delete("skill:" + name);
        }
        changed(ctx);
    };
    actions.profile = async ctx => {
        const profiles = (await cli(["coach", "list"])).profiles;
        const choice = await ctx.ui.select("GPT profiles", ["Switch profile", "Create profile", "New conversation", "Restart native session"]);
        if (choice === "Switch profile") {
            const id = await ctx.ui.select("Select GPT profile", profiles.map(row => row.coach_id));
            if (id) requestSwitch(ctx, {kind: "profile", coach_id: id});
        } else if (choice === "Create profile") {
            const id = await ctx.ui.input("New GPT profile ID");
            if (!id) return;
            const source = await ctx.ui.select("Source workflow template", [...new Set(profiles.map(row => row.workflow_source))]);
            if (!source) return;
            const persona = await ctx.ui.editor("Initial GPT Persona", "");
            if (persona === undefined) return;
            show(ctx, await privateInput(persona, file => cli(["coach", "create", id, "--workflow", source, "--persona-file", file])));
        } else if (choice === "New conversation") requestSwitch(ctx, {kind: "new"});
        else if (choice === "Restart native session") requestSwitch(ctx, {kind: "resume"});
    };
    actions.events = async ctx => {
        const events = await cli(["events", "upcoming", "--workflow", config.workflow_id, "--task", config.task_id]);
        const rows = events.result || events;
        if (!rows.length) return show(ctx, "No pending events");
        const labels = rows.map(row => `${row.event_id} · ${row.status} · ${row.reason}`);
        const picked = await ctx.ui.select("Runtime events", labels);
        const row = rows[labels.indexOf(picked)];
        if (!row) return;
        const full = (await cli(["events", "show", row.event_id])).result;
        const choice = await ctx.ui.select("Event " + row.event_id, ["details", "cancel", "reschedule"]);
        if (choice === "cancel" && await ctx.ui.confirm("Cancel event?", row.reason))
            show(ctx, await cli(["events", "cancel", row.event_id, "--expected-version", String(full.version)]));
        else if (choice === "reschedule") {
            const at = await ctx.ui.input("RFC3339 time with timezone");
            if (at) show(ctx, await cli(["events", "reschedule", row.event_id, "--expected-version", String(full.version), "--at", at]));
        } else if (choice === "details") show(ctx, full);
    };
    actions.recover = async ctx => {
        const rows = (await cli(["recover", "list"])).result;
        if (!rows.length) return show(ctx, "No backend recovery items. Native ownership: CLI frontend list/show/recover.");
        const labels = rows.map(row => `${row.event_id} · ${row.status} · ${row.last_error_code}`);
        const picked = await ctx.ui.select("Backend recovery (no automatic replay)", labels);
        const row = rows[labels.indexOf(picked)];
        if (!row) return;
        show(ctx, await cli(["events", "show", row.event_id]));
        if (await ctx.ui.confirm("Retry may repeat external side effects", "Confirm only after checking the original event and its effects."))
            show(ctx, await cli(["recover", "retry", row.event_id, "--confirm-duplicate-risk"]));
    };
    actions.memory = async ctx => {
        const scope = await ctx.ui.select("Paveway memory scope", ["profile", "current task"]);
        if (!scope) return;
        const flags = ["--workflow", config.workflow_id, ...(scope === "current task" ? ["--task", config.task_id] : [])];
        const rows = (await cli(["memory", "list", ...flags])).result;
        const labels = rows.map(row => `${row.key} · v${row.version} · ${row.title}`);
        const picked = await ctx.ui.select("Paveway Memory", ["Create note", ...labels]);
        if (!picked) return;
        const old = rows[labels.indexOf(picked)];
        const note = old && (await cli(["memory", "show", ...flags, "--key", old.key])).result;
        const action = note ? await ctx.ui.select(note.title, ["show", "edit", "delete"]) : "edit";
        if (action === "show") return show(ctx, note);
        if (action === "delete") {
            if (await ctx.ui.confirm("Delete note?", note.title)) show(ctx, await cli(["memory", "delete", ...flags,
                "--key", note.key, "--expected-version", String(note.version)]));
        } else if (action === "edit") {
            const key = note?.key || await ctx.ui.input("Note key");
            if (!key) return;
            const typed = await ctx.ui.input("Note title", note?.title || "");
            const title = typed === "" ? note?.title : typed;
            if (!title) return;
            const body = await edit(ctx, "memory:" + key, "Memory content", note?.content || "");
            if (body === undefined) return;
            await privateInput(body, file => cli(["memory", "put", ...flags, "--key", key, "--title=" + title,
                "--content-file", file, "--expected-version", String(note?.version || 0)]));
            drafts.delete("memory:" + key); show(ctx, "Memory saved");
        }
    };
    actions.history = async ctx => {
        const query = await ctx.ui.input("Native history search (empty shows recent entries)");
        if (query === undefined) return;
        let offset = 0;
        while (true) {
            const page = (await cli(["frontend", "messages", config.conversation_id, "--query", query, "--offset", String(offset)])).result;
            const labels = page.messages.map((row, index) => `${offset + index + 1} · ${row.role} · ${row.session_id.slice(0, 8)} · ${row.text.slice(0, 60)}`);
            const selected = await ctx.ui.select("Native history · page " + (offset / 20 + 1), [...labels,
                ...(page.has_more ? ["Next page"] : []), ...(offset ? ["Previous page"] : [])]);
            if (!selected) return;
            if (selected === "Next page") offset += 20;
            else if (selected === "Previous page") offset -= 20;
            else show(ctx, page.messages[labels.indexOf(selected)]);
        }
    };
    actions.sync = async ctx => {
        const action = await ctx.ui.select("Private user-data Git", ["status", "set remote", "now", "pause", "resume", "conflicts", "resolve keep local", "bind remote profile", "archived sessions"]);
        if (!action) return;
        if (action === "set remote") {
            const remote = await ctx.ui.input("Dedicated personal-data remote (empty keeps local only)");
            if (remote !== undefined) show(ctx, await cli(["sync", "remote", "--remote", remote]));
        } else if (action === "resolve keep local") {
            if (await ctx.ui.confirm("Resolve configuration conflict?", "Keep the current profile's local version."))
                show(ctx, await cli(["sync", "resolve", "--workflow", config.workflow_id, "--keep-local"]));
        } else if (action === "bind remote profile") {
            const id = await ctx.ui.input("Remote workflow/profile runtime ID");
            const workspace = id && await ctx.ui.input("Absolute local workspace");
            if (workspace) show(ctx, await cli(["sync", "bind", "--workflow", id, "--workspace", workspace]));
        } else if (action === "archived sessions") {
            let offset = 0;
            while (true) {
                const page = await cli(["sync", "sessions", "--workflow", config.workflow_id, "--offset", String(offset)]);
                const labels = page.records.map(row => `${row.device_id.slice(0, 8)} · ${row.task_id} · ${row.session.slice(0, 8)} · ${row.message_count}`);
                const selected = await ctx.ui.select("Synced text archives", [...labels,
                    ...(page.has_more ? ["Next page"] : []), ...(offset ? ["Previous page"] : [])]);
                if (!selected) return;
                if (selected === "Next page") offset += 20;
                else if (selected === "Previous page") offset -= 20;
                else {
                    const row = page.records[labels.indexOf(selected)];
                    const query = await ctx.ui.input("Archived text query");
                    if (query !== undefined) show(ctx, await cli(["sync", "messages", "--device", row.device_id, "--session", row.session, "--query", query]));
                }
            }
        } else show(ctx, await cli(["sync", action]));
    };
    actions.vjudge = async ctx => {
        const action = await ctx.ui.select("VJudge login", ["status", "test", "login", "cookie", "clear"]);
        if (!action) return;
        if (action === "login") {
            const username = await ctx.ui.input("VJudge username");
            if (!username) return;
            const password = await secretInput(ctx, "VJudge password");
            if (password === undefined) return;
            show(ctx, await privateInput(JSON.stringify({username, password}), file => cli(["vjudge", "auth", "login", "--credentials-file", file])));
        } else if (action === "cookie") {
            const cookie = await secretInput(ctx, "VJudge Cookie");
            if (cookie !== undefined) show(ctx, await privateInput(cookie, file => cli(["vjudge", "auth", "cookie", "--cookie-file", file])));
        } else if (action !== "clear" || await ctx.ui.confirm("Clear VJudge Cookie?", "Password is never saved."))
            show(ctx, await cli(["vjudge", "auth", action]));
    };
    actions.tijian = async ctx => {
        const executable = await ctx.ui.input("Absolute native tijian-mcp executable path");
        if (!executable) return;
        const mode = await ctx.ui.select("Tijian service permission", ["read only", "read/write"]);
        if (!mode) return;
        show(ctx, await cli(["workflow", "connect-tijian", config.workflow_id, "--executable", executable,
            mode === "read only" ? "--read-only" : "--read-write"]));
        show(ctx, "MCP saved; restart the native session to load it");
    };
    actions.service = async ctx => {
        const action = await ctx.ui.select("Runtime systemd service", ["status", "logs", "install", "start", "stop", "restart", "enable", "disable", "uninstall"]);
        if (!action) return;
        if (!["status", "logs"].includes(action) && !await ctx.ui.confirm("Service " + action + "?", "Runtime lifecycle changes may require inspecting native ownership before reconnecting.")) return;
        show(ctx, await cli(["service", action]));
    };
    actions.agent = async ctx => {
        const choice = await ctx.ui.select("Background worker", ["current conversation backend", "logical task backend", "another task"]);
        const task = choice === "current conversation backend" ? config.background_task_id :
            choice === "logical task backend" ? config.task_id : choice ? await ctx.ui.input("Background task ID") : undefined;
        if (task && await ctx.ui.confirm("Restart background worker?", "An interrupted result stays uncertain; no prompt will be replayed."))
            show(ctx, await cli(["agent", "restart", "--workflow", config.workflow_id, "--task", task]));
    };
    actions.legacy = async ctx => requestSwitch(ctx, {kind: "legacy"});
    actions.model = async ctx => {
        const raw = await settings();
        const rows = (await cli(["workflow", "models", config.workflow_id])).models;
        const selected = await ctx.ui.select("Portable model preference (next launch)", ["inherit", ...rows.map(row => row.selector)]);
        if (selected !== undefined) show(ctx, await cli(["workflow", "customize", "inference", config.workflow_id,
            "--model", selected === "inherit" ? "" : selected, "--expected-revision", String(raw.revision)]));
    };
    actions.thinking = async ctx => {
        const raw = await settings();
        const selected = await ctx.ui.select("Portable thinking preference (next launch)", ["inherit", "off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"]);
        if (selected !== undefined) show(ctx, await cli(["workflow", "customize", "inference", config.workflow_id,
            "--thinking", selected === "inherit" ? "" : selected, "--expected-revision", String(raw.revision)]));
    };
    const run = async (args, ctx) => {
        try {
            const action = args.trim() || await ctx.ui.select("Paveway", Object.keys(actions));
            if (actions[action]) await actions[action](ctx);
            else if (action) show(ctx, "Use /paveway to select an action; /paveway status shows Runtime health");
        } catch (error) { ctx.ui.notify(error.message + ". Unsubmitted editor drafts remain available in this UI.", "error"); }
    };
    return {run, cli};
}
