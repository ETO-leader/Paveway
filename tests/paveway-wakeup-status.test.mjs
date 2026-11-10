import {describe, expect, test} from "bun:test";
import {registerWakeups} from "../src/oicoach_runtime/bundled/paveway-wakeup.mjs";

// Tool schemas are irrelevant to these UI-only scenarios; no tools are enabled.
const schema = new Proxy(() => schema, {get: () => schema});
function fixture() {
    const handlers = new Map(), commands = new Map(), updates = [], notices = [];
    let rows = [], inbox = {unacknowledged: 0, messages: []}, failure;
    const requests = [];
    const ctx = {agent: {kind: "main"}, ui: {
        notify: (...args) => notices.push(args), select: async () => undefined,
        confirm: async () => false,
    }};
    const pi = {zod: schema, on: (name, handler) => handlers.set(name, handler),
        registerCommand: (name, value) => commands.set(name, value)};
    const request = async (_socket, operation, args) => {
        requests.push({operation, args});
        if (failure) throw failure;
        if (operation === "wakeup.list") return rows;
        if (["wakeup.schedule", "wakeup.cancel", "wakeup.reschedule"].includes(operation)) return args.parameters;
        if (operation === "inbox.bound-list") return inbox;
        if (operation === "inbox.bound-show") return inbox.messages.find(row => row.delivery_id === args.parameters.delivery_id);
        if (operation === "inbox.bound-acknowledge") { inbox = {...inbox, unacknowledged: inbox.unacknowledged - 1}; return {}; }
        throw new Error("Unexpected operation: " + operation);
    };
    const wakeups = registerWakeups(pi, {tools_enabled: false, socket_path: "/fixture"},
        () => ({conversation_id: "bound", generation: 7, owner_token: "fixture"}), request,
        async () => {}, value => updates.push(value));
    return {ctx, handlers, commands, updates, notices, requests, wakeups,
        rows: value => {rows = value;}, inbox: value => {inbox = value;}, fail: value => {failure = value;},
        close: () => handlers.get("session_shutdown")({}, ctx)};
}

describe("Runtime-owned status publication", () => {
    test("reminder and wakeup share parsing but create distinct intents and filter all mutations", async () => {
        const f = fixture();
        try {
            for (const [command, intent] of [["reminder", "reminder"], ["wakeup", "agent_wake"]]) {
                const handler = f.commands.get(command).handler;
                expect(f.commands.get(command).description).toContain(command === "reminder" ? "不调用模型" : "后台 GPT");
                await handler("in 2.5m Explicit task", f.ctx);
                let scheduled = f.requests.filter(row => row.operation === "wakeup.schedule").at(-1).args.parameters;
                expect(scheduled.intent).toBe(intent);
                expect(scheduled.when).toEqual({kind: "after_now", seconds: 150});
                expect(scheduled.authorized_by_user).toBe(true);
                await handler("at 2099-01-01T21:00:00+08:00 Explicit task", f.ctx);
                scheduled = f.requests.filter(row => row.operation === "wakeup.schedule").at(-1).args.parameters;
                expect(scheduled.intent).toBe(intent);
                expect(scheduled.when).toEqual({kind: "at", at: "2099-01-01T13:00:00.000Z"});
                await handler("at 21:00 Local clock", f.ctx);
                scheduled = f.requests.filter(row => row.operation === "wakeup.schedule").at(-1).args.parameters;
                expect(new Date(scheduled.when.at).getHours()).toBe(21);
                await handler("list", f.ctx);
                expect(f.requests.some(row => row.operation === "wakeup.list" && row.args.parameters.intent === intent)).toBe(true);
                await handler("cancel event-id 3", f.ctx);
                expect(f.requests.filter(row => row.operation === "wakeup.cancel").at(-1).args.parameters)
                    .toEqual({event_id: "event-id", expected_version: 3, intent});
                await handler("reschedule event-id 3 2099-01-01T00:00:00Z", f.ctx);
                expect(f.requests.filter(row => row.operation === "wakeup.reschedule").at(-1).args.parameters.intent).toBe(intent);
            }
        } finally {f.close();}
    });
    test("unavailable usage is unknown, not an empty inbox; recovery publishes real unread state", async () => {
        const f = fixture();
        try {
            f.fail(new Error("disconnected"));
            await expect(f.wakeups.refresh(f.ctx)).rejects.toThrow("disconnected");
            expect(f.updates.at(-1)).toEqual({wakeup: {unavailable: true, timing: undefined, running: false}, inbox: undefined});
            f.fail(undefined);
            f.inbox({unacknowledged: 3, messages: []});
            await f.wakeups.refresh();
            expect(f.updates.at(-1)).toEqual({wakeup: {unavailable: false, timing: "none", running: false}, inbox: 3});
            expect(f.requests.every(row => row.args.conversation_id === "bound" && row.args.generation === 7)).toBe(true);
        } finally {f.close();}
    });

    test("display refresh and notification never acknowledge; explicit confirmation retains CAS version", async () => {
        const f = fixture();
        const message = {delivery_id: "delivery-a", state: "unacknowledged", kind: "reminder", text: "real reminder", version: 4, created_at: "2026-01-01T00:00:00Z"};
        try {
            await f.wakeups.refresh(f.ctx);
            f.inbox({unacknowledged: 1, messages: [message]});
            await f.wakeups.refresh();
            await f.wakeups.refresh();
            expect(f.notices.length).toBe(1);
            expect(f.updates.at(-1).inbox).toBe(1);
            expect(f.requests.some(row => row.operation === "inbox.bound-acknowledge")).toBe(false);
            f.ctx.ui.select = async (_title, labels) => labels[0];
            await f.commands.get("inbox").handler("", f.ctx);
            expect(f.updates.at(-1).inbox).toBe(1);
            f.ctx.ui.confirm = async () => true;
            await f.commands.get("inbox").handler("", f.ctx);
            expect(f.requests.find(row => row.operation === "inbox.bound-acknowledge").args.parameters)
                .toEqual({delivery_id: "delivery-a", expected_version: 4});
            expect(f.updates.at(-1).inbox).toBe(0);
        } finally {f.close();}
    });

    test("only scheduled wakeups own a countdown timer; main shutdown disposes it", async () => {
        const originalSet = globalThis.setInterval, originalClear = globalThis.clearInterval;
        const active = new Set();
        globalThis.setInterval = fn => {const handle = {fn, unref() {}}; active.add(handle); return handle;};
        globalThis.clearInterval = handle => {active.delete(handle);};
        const f = fixture();
        try {
            await f.wakeups.refresh(f.ctx);
            expect(active.size).toBe(0);
            f.rows([{not_before_utc: "2099-01-01T00:00:00Z", status: "pending"}]);
            await f.wakeups.refresh();
            expect(active.size).toBe(1);
            await f.wakeups.refresh();
            expect(active.size).toBe(1);
            f.handlers.get("session_shutdown")({}, {agent: {kind: "subagent"}});
            expect(active.size).toBe(1);
            f.rows([{not_before_utc: "2099-01-01T00:00:00Z", status: "running"}]);
            await f.wakeups.refresh();
            expect(f.updates.at(-1).wakeup).toEqual({unavailable: false, timing: "running", running: true});
            f.close();
            expect(active.size).toBe(0);
        } finally {f.close(); globalThis.setInterval = originalSet; globalThis.clearInterval = originalClear;}
    });
});
