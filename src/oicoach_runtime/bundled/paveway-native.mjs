// Thin native OMP extension. Runtime remains the owner of durable operations.
import {createHash, randomUUID} from "node:crypto";
import {lstatSync, readFileSync, readSync, writeFileSync, writeSync} from "node:fs";
import {createConnection} from "node:net";
import {registerWakeups} from "./paveway-wakeup.mjs";
import {registerManagement} from "./paveway-management.mjs";
import {registerMemory} from "./paveway-memory.mjs";

function stop() {
    try { writeSync(2, "paveway_native_failed: required native integration failed\n"); }
    finally { process.kill(process.pid, "SIGKILL"); }
}

export function runtimeRequest(path, command, args = {}) {
    return new Promise((resolve, reject) => {
        const info = lstatSync(path);
        if (!info.isSocket() || info.uid !== process.getuid() || (info.mode & 0o077))
            return reject(new Error("Runtime socket is not private"));
        const id = randomUUID();
        const frame = JSON.stringify({api_version: 1, id, command, arguments: args}) + "\n";
        if (Buffer.byteLength(frame) > 131072) return reject(new Error("Request too large"));
        const socket = createConnection(path);
        let data = Buffer.alloc(0);
        const fail = () => { socket.destroy(); reject(new Error("Runtime unavailable; no automatic retry")); };
        socket.setTimeout(10000, fail);
        socket.on("error", fail);
        socket.on("connect", () => socket.write(frame));
        socket.on("data", chunk => {
            data = Buffer.concat([data, chunk]);
            if (data.length > 131072) return fail();
            if (!data.includes(10)) return;
            socket.destroy();
            try {
                const reply = JSON.parse(data.toString("utf8"));
                if (reply.api_version !== 1 || reply.id !== id) return fail();
                if (reply.error) return reject(new Error(reply.error.code + ": " + reply.error.message));
                resolve(reply.result);
            } catch { fail(); }
        });
        socket.on("end", () => { if (!data.includes(10)) fail(); });
    });
}

export default async function paveway(pi) {
    try {
        const file = process.env.PAVEWAY_NATIVE_DATA;
        const info = lstatSync(file);
        if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o077)) stop();
        const config = JSON.parse(readFileSync(file, "utf8"));
        const descriptor = Number(process.env.PAVEWAY_NATIVE_FD);
        const nonce = process.env.PAVEWAY_NATIVE_NONCE;
        if (!Number.isInteger(descriptor) || descriptor < 3 || !/^[a-f0-9]{64}$/.test(nonce ?? "")) stop();
        const sha = createHash("sha256").update(readFileSync(new URL(import.meta.url))).digest("hex");
        let admitted = false;
        const management = registerManagement(pi, config, (ctx, action) => {
            if (!ctx.isIdle()) return ctx.ui.notify("Wait for the current activity before switching/restarting", "warning");
            writeFileSync(config.switch_path, JSON.stringify({nonce, ...action}), {mode: 0o600, flag: "wx"});
            ctx.shutdown();
        });
        const bound = () => ({conversation_id: config.conversation_id, generation: config.generation, owner_token: config.owner_token});
        const captureInference = async ctx => {
            if (ctx?.agent?.kind !== "main") throw new Error("Subagents cannot use the main GPT's managed capability; return the request to GPT");
            if (ctx?.model) await runtimeRequest(config.socket_path, "frontend.inference", {...bound(),
                model: ctx.model.provider + "/" + ctx.model.id, thinking: pi.getThinkingLevel() || ""});
        };
        const wakeups = registerWakeups(pi, config, bound, runtimeRequest, captureInference);
        registerMemory(pi, config, bound, runtimeRequest);
        const sessionReady = async (_event, ctx) => {
            try {
                // OMP emits spawned-session hooks in the same extension host.
                // A child is neither a main-session switch nor a TUI failure.
                if (ctx.agent?.kind !== "main") return;
                if (!ctx.hasUI) stop();
                await runtimeRequest(config.socket_path, "status");
                const frame = Buffer.from(JSON.stringify({apiVersion: 1, nonce, pid: process.pid,
                    extensionDigest: sha, sessionId: ctx.sessionManager.getSessionId(),
                    sessionFile: ctx.sessionManager.getSessionFile(), contextPolicy: "omp-native"}) + "\n");
                writeSync(descriptor, frame);
                const ack = Buffer.alloc(1);
                if (readSync(descriptor, ack, 0, 1, null) !== 1 || ack[0] !== 65) stop();
                const owner = await runtimeRequest(config.socket_path, "frontend.show", {conversation_id: config.conversation_id});
                config.generation = owner.generation;
                admitted = true;
                ctx.ui.setStatus("paveway", `GPT · ${config.coach_id}/${config.task_id} · OMP native`);
                await wakeups.refresh(ctx);
            } catch { stop(); }
        };
        pi.on("session_start", sessionReady);
        pi.on("session_switch", sessionReady);
        for (const phase of ["agent_start", "agent_end", "session_shutdown"]) {
            pi.on(phase, async (_event, ctx) => {
                if (!admitted || ctx.agent?.kind !== "main") return;
                try { await runtimeRequest(config.socket_path, "frontend.observe", {...bound(), phase}); }
                catch { /* Unknown observation is not a completion or retry. */ }
            });
        }
        const heartbeat = setInterval(() => {
            if (admitted) runtimeRequest(config.socket_path, "frontend.observe", {...bound(), phase: "heartbeat"}).catch(() => {});
            if (admitted) wakeups.refresh().catch(() => {});
        }, 10000);
        heartbeat.unref();
        pi.registerCommand("paveway", {description: "Paveway Runtime status and management", handler: async (args, ctx) => {
            if (!admitted) stop();
            try {
                if (args.trim() && args.trim() !== "status") {
                    await management.run(args, ctx);
                    return;
                }
                if (!args.trim()) return management.run("", ctx);
                const status = await runtimeRequest(config.socket_path, "status");
                ctx.ui.notify(JSON.stringify(status, null, 2), "info");
            } catch (error) { ctx.ui.notify(error.message, "error"); }
        }});
        pi.registerCommand("coach", {description: "Show the bound GPT environment", handler: async (_args, ctx) => {
            ctx.ui.notify(`GPT · ${config.coach_id}/${config.task_id}\nSession: ${ctx.sessionManager.getSessionId()}\nContext: OMP native`, "info");
        }});
    } catch { stop(); }
}
