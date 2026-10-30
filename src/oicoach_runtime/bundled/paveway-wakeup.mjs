import {randomUUID} from "node:crypto";

export function registerWakeups(pi, config, bound, request, captureInference) {
    const call = async (operation, parameters, ctx) => {
        if (operation === "schedule" && ctx) await captureInference(ctx);
        return request(config.socket_path, "wakeup." + operation, {...bound(), parameters});
    };
    const inboxCall = (operation, parameters = {}) => request(config.socket_path, "inbox.bound-" + operation, {...bound(), parameters});
    const z = pi.zod;
    const when = z.union([
        z.object({kind: z.literal("at"), at: z.string()}),
        z.object({kind: z.literal("after_now"), seconds: z.number().min(0).max(31536000)}),
    ]);
    const definitions = {
        schedule: z.object({when, reason: z.string().min(1).max(4096), dedupe_key: z.string().min(1).max(256),
            intent: z.enum(["reminder", "agent_wake"]), authorized_by_user: z.boolean()}),
        list: z.object({}),
        cancel: z.object({event_id: z.string(), expected_version: z.number().int().min(1)}),
        reschedule: z.object({event_id: z.string(), expected_version: z.number().int().min(1), at: z.string()}),
    };
    if (config.tools_enabled) for (const [operation, parameters] of Object.entries(definitions)) {
        pi.registerTool({name: "paveway_wakeup_" + operation, label: "Paveway wakeup " + operation,
            description: operation === "schedule" ?
                "Schedule only an explicit user reminder or existing opt-in. after_now starts at Runtime receipt; native after_turn_settled is unavailable. Never create an autonomous loop. Reuse the same dedupe_key for an uncertain identical request; inspect list before retrying. Scheduling is not delivery or user-seen evidence." :
                "Manage current bound workflow/task wakeups; mutations require the event CAS version. Do not supply another scope.",
            parameters, approval: operation === "list" ? "read" : "write", loadMode: "essential",
            async execute(_id, args, _signal, _update, ctx) {
                if (ctx?.agent?.kind !== "main") return {content: [{type: "text", text: "Only the main GPT may use its managed wakeup capability"}], isError: true};
                try { return {content: [{type: "text", text: JSON.stringify(await call(operation, args, ctx))}]}; }
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
    let unread = 0;
    let unavailable = false;
    const status = () => {
        if (!context) return;
        if (unavailable) return context.ui.setStatus("paveway-wakeup", "Runtime unavailable or owner uncertain · no automatic replay");
        const event = upcoming.find(row => row.not_before_utc);
        const timing = event ? `${new Date(event.not_before_utc).toLocaleString()} · ` +
            (event.status === "running" ? "executing" : `${Math.max(0, Math.ceil((Date.parse(event.not_before_utc) - Date.now()) / 1000))}s`) : "none";
        context.ui.setStatus("paveway-wakeup", `Wakeup: ${timing} · Inbox: ${unread}`);
    };
    const refresh = async ctx => {
        context = ctx || context;
        try {
            upcoming = await call("list", {});
            const inbox = await inboxCall("list");
            unread = inbox.unacknowledged;
            unavailable = false;
            status();
        } catch (error) { unavailable = true; status(); throw error; }
    };
    const clock = setInterval(status, 1000);
    clock.unref();
    pi.registerCommand("wakeup", {description: "in 10m TEXT | at HH:mm TEXT | list | cancel ID VERSION | reschedule ID VERSION TIME",
        handler: async (args, ctx) => {
            try {
                let result;
                const parts = args.trim().split(/\s+/);
                const [operation, value] = parts;
                if (operation === "list" || !args.trim()) result = await call("list", {});
                else if (operation === "cancel") result = await call("cancel", {event_id: value, expected_version: Number(parts[2])});
                else if (operation === "reschedule") result = await call("reschedule", {event_id: value,
                    expected_version: Number(parts[2]), at: parseAt(parts[3] || "")});
                else if (operation === "in" || operation === "at") {
                    let timing;
                    if (operation === "in") {
                        const duration = /^(\d+(?:\.\d+)?)(s|m|h|d)$/.exec(value || "");
                        if (!duration) throw new Error("Use a duration such as 10m or 30s");
                        timing = {kind: "after_now", seconds: Number(duration[1]) * {s: 1, m: 60, h: 3600, d: 86400}[duration[2]]};
                    } else timing = {kind: "at", at: parseAt(value || "")};
                    result = await call("schedule", {when: timing, reason: parts.slice(2).join(" "),
                        dedupe_key: "native-user-" + randomUUID(), intent: "reminder", authorized_by_user: true}, ctx);
                } else throw new Error("/wakeup in 10m TEXT | at HH:mm TEXT | list | cancel ID VERSION | reschedule ID VERSION TIME");
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
