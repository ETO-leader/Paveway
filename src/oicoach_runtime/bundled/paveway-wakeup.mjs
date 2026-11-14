import {randomUUID} from "node:crypto";

export function registerWakeups(pi, config, bound, request, captureInference, publishStatus) {
    const call = async (operation, parameters, ctx) => {
        if (operation === "schedule" && parameters.intent === "agent_wake" && ctx) await captureInference(ctx);
        return request(config.socket_path, "wakeup." + operation, {...bound(), parameters});
    };
    const inboxCall = (operation, parameters = {}) => request(config.socket_path, "inbox.bound-" + operation, {...bound(), parameters});
    const bindingCall = (operation, parameters = {}) => request(config.socket_path, "wakebinding." + operation, {...bound(), parameters});
    const z = pi.zod;
    const when = z.union([
        z.object({kind: z.literal("at"), at: z.string()}),
        z.object({kind: z.literal("after_now"), seconds: z.number().min(0).max(31536000)}),
    ]);
    const bindingBase = {
        mode: z.enum(["independent", "linked", "follow"]).optional(),
        time_source: z.enum(["scheduled", "due"]).optional(),
        offset_seconds: z.number().int().min(-2678400).max(2678400).optional(),
        eligibility: z.enum(["open", "open_and_incomplete"]).optional(),
        reminder_at: z.string().optional(),
        resume_policy: z.enum(["manual", "when_eligible"]).optional(),
    };
    const definitions = {
        schedule: z.object({when, reason: z.string().min(1).max(4096), dedupe_key: z.string().min(1).max(256),
            intent: z.enum(["reminder", "agent_wake"]), authorized_by_user: z.boolean()}),
        list: z.object({intent: z.enum(["reminder", "agent_wake"]).optional()}),
        cancel: z.object({event_id: z.string(), expected_version: z.number().int().min(1), intent: z.enum(["reminder", "agent_wake"]).optional()}),
        reschedule: z.object({event_id: z.string(), expected_version: z.number().int().min(1), at: z.string(), intent: z.enum(["reminder", "agent_wake"]).optional()}),
        binding_create: z.object({todo_id: z.string().min(1).max(512), name: z.string().min(1).max(64).optional(),
            ...bindingBase, authorized_by_user: z.boolean()}),
        binding_list: z.object({status: z.enum(["active", "pending", "ineligible", "disabled", "conflict"]).optional()}),
        binding_show: z.object({binding_id: z.string().min(1)}),
        binding_update: z.object({binding_id: z.string().min(1), expected_version: z.number().int().min(1), ...bindingBase,
            name: z.string().min(1).max(64).optional()}),
        binding_cancel: z.object({binding_id: z.string().min(1), expected_version: z.number().int().min(1)}),
        binding_sync: z.object({binding_id: z.string().min(1).optional()}),
        binding_status: z.object({}),
    };
    if (config.tools_enabled) for (const [operation, parameters] of Object.entries(definitions)) {
        const binding = operation.startsWith("binding_");
        pi.registerTool({name: "paveway_wakeup_" + operation, label: "Paveway wakeup " + operation,
            description: binding ?
                "Manage an explicitly authorized Tijian Todo WakeBinding in the current workflow/task. A follow binding uses the Todo time and is reconciled by the Runtime even while the TUI is closed. Todo data is read through native tijian-mcp; no Todo content is written." :
                operation === "schedule" ?
                "Choose intent=reminder for 'remind me': durable Inbox and optional desktop notification, no model or Agent start. Choose agent_wake only when the user explicitly wants future AI work/checks/decisions or has opted in; it runs a background Agent and saves its result to Inbox, without a reminder popup. after_now starts at Runtime receipt; native after_turn_settled is unavailable. Never create an autonomous loop. Reuse the same dedupe_key for an uncertain identical request; inspect list before retrying. Scheduling is not delivery or user-seen evidence." :
                "Manage current bound workflow/task events; filter intent to distinguish reminder from agent_wake. Mutations require the event CAS version; pass intent to prevent changing another type. Do not supply another scope.",
            parameters, approval: ["list", "binding_list", "binding_show", "binding_status"].includes(operation) ? "read" : "write", loadMode: "essential",
            async execute(_id, args, _signal, _update, ctx) {
                if (ctx?.agent?.kind !== "main") return {content: [{type: "text", text: "Only the main GPT may use its managed wakeup capability"}], isError: true};
                try {
                    const result = binding ? await bindingCall(operation.slice("binding_".length), args) : await call(operation, args, ctx);
                    return {content: [{type: "text", text: JSON.stringify(result)}]};
                }
                catch (error) { return {content: [{type: "text", text: error.message}], isError: true}; }
            }});
    }
    const parseAt = value => {
        if (/^\d\d:\d\d$/.test(value)) {
            const [hours, minutes] = value.split(":").map(Number);
            if (hours > 23 || minutes > 59) throw new Error("Invalid local clock time");
            const date = new Date();
            date.setHours(hours, minutes, 0, 0);
            if (date.getTime() <= Date.now()) date.setDate(date.getDate() + 1);
            return date.toISOString();
        }
        if (!/(Z|[+-]\d\d:\d\d)$/.test(value) || !Number.isFinite(Date.parse(value)))
            throw new Error("Use HH:mm (system timezone, next occurrence) or RFC3339 with timezone");
        return new Date(value).toISOString();
    };
    let context;
    let upcoming = [];
    let unread;
    let unavailable = false;
    let inboxInitialized = false;
    const observedDeliveries = new Set();
    let clock;
    const status = () => {
        if (!context) return;
        const event = upcoming.find(row => row.not_before_utc);
        if (event && !unavailable && !clock) {
            clock = setInterval(status, 1000);
            clock.unref();
        } else if ((!event || unavailable) && clock) {
            clearInterval(clock);
            clock = undefined;
        }
        const seconds = event ? Math.max(0, Math.ceil((Date.parse(event.not_before_utc) - Date.now()) / 1000)) : 0;
        const remaining = seconds >= 86400 ? `${Math.ceil(seconds / 86400)}d` :
            seconds >= 3600 ? `${Math.floor(seconds / 3600)}h${Math.floor(seconds % 3600 / 60)}m` :
            seconds >= 60 ? `${Math.floor(seconds / 60)}m${seconds % 60}s` : `${seconds}s`;
        publishStatus({
            wakeup: {unavailable, timing: unavailable ? undefined : event ? event.status === "running" ? "running" : remaining : "none",
                running: !unavailable && event?.status === "running"},
            inbox: unavailable ? undefined : unread,
        });
    };
    const refresh = async ctx => {
        context = ctx || context;
        try {
            upcoming = await call("list", {});
            const inbox = await inboxCall("list");
            unread = inbox.unacknowledged;
            const fresh = inbox.messages.filter(row => !observedDeliveries.has(row.delivery_id) && row.state !== "acknowledged");
            for (const row of inbox.messages) observedDeliveries.add(row.delivery_id);
            // Existing Inbox contents remain visible on reconnect without
            // replaying old notifications. A notice is not acknowledgement,
            // a model message, or evidence that the user has seen the result.
            if (inboxInitialized && fresh.length && context) {
                const previews = [...fresh].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.delivery_id.localeCompare(b.delivery_id)).map(row => {
                    const title = row.kind === "reminder" ? "定时提醒" : "GPT · 后台定时任务回复／执行状态";
                    const text = row.text.replace(/\r\n?/g, "\n").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/g, "");
                    return `${title}${row.truncated ? "（节选）" : ""}\n${text}`;
                });
                context.ui.notify(`Paveway · 新的唤醒消息\n\n${previews.join("\n\n")}\n\n/inbox 查看完整记录或确认已读。`, "info");
            }
            inboxInitialized = true;
            if (observedDeliveries.size > 1000) {
                observedDeliveries.clear();
                for (const row of inbox.messages) observedDeliveries.add(row.delivery_id);
            }
            unavailable = false;
            status();
        } catch (error) { unavailable = true; status(); throw error; }
    };
    pi.on("session_shutdown", (_event, ctx) => {
        if (ctx.agent?.kind !== "main") return;
        clearInterval(clock);
        clock = undefined;
        context = undefined;
    });
    for (const [command, intent] of [["reminder", "reminder"], ["wakeup", "agent_wake"]]) pi.registerCommand(command, {
        description: (intent === "reminder" ? "纯提醒，不调用模型；" : "唤醒后台 GPT 执行任务；") + "in 10m TEXT | at HH:mm TEXT | list | cancel ID VERSION | reschedule ID VERSION TIME | bind TODO_ID [name] | bindings | sync [ID] | unbind ID VERSION",
        handler: async (args, ctx) => {
            try {
                let result;
                const parts = args.trim().split(/\s+/);
                const [operation, value] = parts;
                if (operation === "list" || !args.trim()) result = await call("list", {intent});
                else if (operation === "cancel") result = await call("cancel", {event_id: value, expected_version: Number(parts[2]), intent});
                else if (operation === "reschedule") result = await call("reschedule", {event_id: value,
                    expected_version: Number(parts[2]), at: parseAt(parts[3] || ""), intent});
                else if (operation === "bind") {
                    if (!value) throw new Error(`Use /${command} bind TODO_ID [name]`);
                    result = await bindingCall("create", {todo_id: value, name: parts[2] || "default",
                        mode: "follow", time_source: "due", eligibility: "open_and_incomplete",
                        resume_policy: "manual", authorized_by_user: true});
                }
                else if (operation === "bindings") result = await bindingCall("list", {});
                else if (operation === "sync") result = await bindingCall("sync", value ? {binding_id: value} : {});
                else if (operation === "unbind") result = await bindingCall("cancel", {binding_id: value,
                    expected_version: Number(parts[2])});
                else if (operation === "in" || operation === "at") {
                    let timing;
                    if (operation === "in") {
                        const duration = /^(\d+(?:\.\d+)?)(s|m|h|d)$/.exec(value || "");
                        if (!duration) throw new Error("Use a duration such as 10m or 30s");
                        timing = {kind: "after_now", seconds: Number(duration[1]) * {s: 1, m: 60, h: 3600, d: 86400}[duration[2]]};
                    } else timing = {kind: "at", at: parseAt(value || "")};
                    result = await call("schedule", {when: timing, reason: parts.slice(2).join(" "),
                        dedupe_key: "native-user-" + randomUUID(), intent, authorized_by_user: true}, ctx);
                } else throw new Error(`/${command} in 10m TEXT | at HH:mm TEXT | list | cancel ID VERSION | reschedule ID VERSION TIME | bind TODO_ID [name] | bindings | sync [ID] | unbind ID VERSION`);
                ctx.ui.notify(JSON.stringify(result, null, 2), "info");
                await refresh(ctx);
            } catch (error) { ctx.ui.notify(error.message, "error"); }
        }});
    pi.registerCommand("inbox", {description: "Read persistent messages (/inbox [page]) and explicitly acknowledge them", handler: async (args, ctx) => {
        try {
            const page = args.trim() ? Number(args.trim()) : 1;
            if (!Number.isInteger(page) || page < 1) throw new Error("Use /inbox [page], starting at 1");
            const inbox = await inboxCall("list", {offset: (page - 1) * 20});
            if (!inbox.messages.length) return ctx.ui.notify("Inbox is empty", "info");
            const labels = inbox.messages.map(row => `${row.delivery_id.slice(0, 10)} · ${row.state} · ${row.kind} · ${row.created_at}`);
            const selected = await ctx.ui.select("Paveway Inbox", labels);
            const chosen = inbox.messages[labels.indexOf(selected)];
            const row = chosen && await inboxCall("show", {delivery_id: chosen.delivery_id});
            if (row && await ctx.ui.confirm("Acknowledge this message?", row.text))
                await inboxCall("acknowledge", {delivery_id: row.delivery_id, expected_version: row.version});
            await refresh(ctx);
        } catch (error) { ctx.ui.notify(error.message, "error"); }
    }});
    return {refresh};
}
