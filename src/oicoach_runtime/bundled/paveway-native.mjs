// Thin native OMP extension. Runtime remains the owner of durable operations.
import {createHash} from "node:crypto";
import {lstatSync, readFileSync, readSync, realpathSync, writeFileSync, writeSync} from "node:fs";
import {isAbsolute, relative, sep} from "node:path";
import {runtimeRequest} from "./paveway-runtime.mjs";
import {registerWakeups} from "./paveway-wakeup.mjs";
import {registerManagement} from "./paveway-management.mjs";
import {registerMemory} from "./paveway-memory.mjs";
import {registerMemoryContext} from "./paveway-memory-context.mjs";
import {registerNativePresentation} from "./paveway-native-ui.mjs";

const admissionDescriptor = Number(process.env.PAVEWAY_NATIVE_FD);
const admissionNonce = process.env.PAVEWAY_NATIVE_NONCE;

function stop(stage) {
    try {
        writeSync(admissionDescriptor, JSON.stringify({failure: stage, nonce: admissionNonce}) + "\n");
    } finally { process.kill(process.pid, "SIGKILL"); }
}

const workspaceNotice = "Use /paveway cwd to change projects without moving this Conversation.";
const resumeNotice = "Use /paveway profile → Resume conversation for another Paveway conversation";
const isShellChain = /^(?:'[^']*'|"[^"]*"|\\.|[^"'\\&;|\r\n])*[&;|\r\n]/;

function managedInput(text) {
    const input = text.trim();
    if (/^\/(?:move|wt|worktree)(?:\s|$)/.test(input)) return workspaceNotice;
    if (/^\/(?:resume|import)(?:\s|$)/.test(input)) return resumeNotice;
    if (/^\/(?:restart|reload-plugins)(?:\s|$)/.test(input))
        return "Use /paveway restart to restart this Conversation through Runtime.";
    const shell = /^!!?\s*(cd(?:\s|$)[\s\S]*)$/.exec(input)?.[1];
    if (shell && !isShellChain.test(shell)) return workspaceNotice;
}


export default async function paveway(pi) {
    let stage = "invalid Paveway Extension launch data";
    try {
        const file = process.env.PAVEWAY_NATIVE_DATA;
        const info = lstatSync(file);
        if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o077)) stop(stage);
        const config = JSON.parse(readFileSync(file, "utf8"));
        const descriptor = admissionDescriptor;
        const nonce = admissionNonce;
        if (!Number.isInteger(descriptor) || descriptor < 3 || !/^[a-f0-9]{64}$/.test(nonce ?? "")) stop(stage);
        stage = "cannot read the loaded Paveway Extension source";
        const sha = createHash("sha256").update(readFileSync(new URL(import.meta.url))).digest("hex");
        let admitted = false;
        const statusState = {};
        let statusContext, removeResumeKeyGuard;
        stage = "OMP Extension registration failed";
        const showPresentation = registerNativePresentation(pi, config);
        const refreshPresentation = () => {
            if (statusContext) showPresentation(statusContext, statusState);
        };
        const publishStatus = update => {
            Object.assign(statusState, update);
            refreshPresentation();
        };
        pi.on("input", (event, ctx) => {
            if (ctx.agent?.kind !== "main") return;
            if (!admitted || ctx.cwd !== config.cwd) stop("main input outside admitted Conversation");
            const notice = managedInput(event.text);
            if (notice) {
                ctx.ui.notify(notice, "warning");
                return {handled: true};
            }
        });
        pi.on("before_agent_start", (_event, ctx) => {
            if (ctx.agent?.kind === "main" && (!admitted || ctx.cwd !== config.cwd))
                stop("OMP main context escaped the admitted Conversation");
        });
        const management = registerManagement(pi, config, (ctx, action) => {
            const jobs = ctx.getAsyncJobSnapshot?.();
            if (ctx.agent?.kind !== "main" || !admitted || !ctx.isIdle() || ctx.hasPendingMessages() ||
                    jobs?.running?.length || jobs?.delivery?.queued || jobs?.delivery?.delivering)
                return ctx.ui.notify("Wait for the current activity before switching/restarting", "warning");
            const inference = action.kind === "cwd" ? {
                model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "", thinking: pi.getThinkingLevel() || "off",
            } : {};
            const payload = JSON.stringify({nonce, ...action, ...inference});
            if (Buffer.byteLength(payload) > 4096) throw new Error("Working directory is too long for native handoff");
            writeFileSync(config.switch_path, payload, {mode: 0o600, flag: "wx"});
            ctx.shutdown();
        });
        const bound = () => ({conversation_id: config.conversation_id, generation: config.generation, owner_token: config.owner_token});
        const captureInference = async ctx => {
            if (ctx?.agent?.kind !== "main") throw new Error("Subagents cannot use the main GPT's managed capability; return the request to GPT");
            if (ctx?.model) await runtimeRequest(config.socket_path, "frontend.inference", {...bound(),
                model: ctx.model.provider + "/" + ctx.model.id, thinking: pi.getThinkingLevel() || ""});
        };
        const wakeups = registerWakeups(pi, config, bound, runtimeRequest, captureInference, publishStatus);
        registerMemory(pi, config, bound, runtimeRequest);
        registerMemoryContext(pi, {
            scope: {workflow_id: config.workflow_id, task_id: config.task_id},
            enabled: config.memory_enabled,
            allowBypass: true,
            recall: known => runtimeRequest(config.socket_path, "native_memory.context",
                {...bound(), parameters: {known}}),
            fail: (_error, diagnostic) => {
                writeSync(2, JSON.stringify({component: "memory", ...diagnostic}) + "\n");
                stop("Runtime Memory context failed");
            },
        });
        const sessionReady = async (_event, ctx) => {
            try {
                // OMP emits spawned-session hooks in the same extension host.
                // A child is neither a main-session switch nor a TUI failure.
                if (ctx.agent?.kind !== "main") return;
                stage = "OMP cannot provide the managed interactive context";
                const prompt = ctx.getSystemPrompt();
                if (!ctx.hasUI || ctx.cwd !== config.cwd || !Array.isArray(ctx.sessionManager.getBranch()) ||
                    !(typeof prompt === "string" || (Array.isArray(prompt) && prompt.every(part => typeof part === "string")))) stop(stage);
                if (!removeResumeKeyGuard) {
                    stage = "OMP cannot install the managed resume key guard";
                    const {getKeybindings} = await import("@oh-my-pi/pi-tui");
                    const keys = getKeybindings();
                    removeResumeKeyGuard = ctx.ui.onTerminalInput(data => {
                        if (!keys.matches(data, "app.session.resume")) return;
                        ctx.ui.notify(resumeNotice, "warning");
                        return {consume: true};
                    });
                }
                if (config.materialize && _event.type === "session_start") await ctx.sessionManager.ensureOnDisk();
                stage = "Runtime Extension handshake failed";
                await runtimeRequest(config.socket_path, "status");
                const frame = Buffer.from(JSON.stringify({apiVersion: 1, nonce, pid: process.pid,
                    extensionDigest: sha, sessionId: ctx.sessionManager.getSessionId(),
                    sessionFile: ctx.sessionManager.getSessionFile(), contextPolicy: "omp-native"}) + "\n");
                writeSync(descriptor, frame);
                const ack = Buffer.alloc(1);
                if (readSync(descriptor, ack, 0, 1, null) !== 1 || ack[0] !== 65) stop(stage);
                const owner = await runtimeRequest(config.socket_path, "frontend.show", {conversation_id: config.conversation_id});
                config.generation = owner.generation;
                admitted = true;
                statusContext = ctx;
                showPresentation(ctx, statusState, {banner: _event.type === "session_start" && !statusState.started});
                statusState.started = true;
                stage = "Runtime Wakeup state unavailable";
                await wakeups.refresh(ctx);
            } catch { stop(stage); }
        };
        pi.on("session_start", sessionReady);
        pi.on("session_switch", sessionReady);
        pi.on("session_before_switch", (event, ctx) => {
            if (ctx.agent?.kind !== "main" || event.reason !== "resume") return;
            try {
                const file = realpathSync(event.targetSessionFile);
                const within = relative(realpathSync(config.session_dir), file);
                if (within && within !== ".." && !within.startsWith(".." + sep) && !isAbsolute(within)) return;
            } catch { /* Reject missing paths without changing the active session. */ }
            ctx.ui.notify(resumeNotice, "warning");
            return {cancel: true};
        });
        for (const phase of ["agent_start", "agent_end", "session_shutdown"]) {
            pi.on(phase, async (_event, ctx) => {
                if (!admitted || ctx.agent?.kind !== "main") return;
                statusState.busy = phase === "agent_start";
                if (phase !== "session_shutdown") refreshPresentation();
                try { await runtimeRequest(config.socket_path, "frontend.observe", {...bound(), phase}); }
                catch { /* Unknown observation is not a completion or retry. */ }
            });
        }
        const heartbeat = setInterval(() => {
            if (admitted) runtimeRequest(config.socket_path, "frontend.observe", {...bound(), phase: "heartbeat"}).catch(() => {});
            if (admitted) wakeups.refresh().catch(() => {});
        }, 10000);
        heartbeat.unref();
        pi.on("session_shutdown", (_event, ctx) => {
            if (ctx.agent?.kind !== "main") return;
            clearInterval(heartbeat);
            removeResumeKeyGuard?.();
            admitted = false;
            statusContext = undefined;
        });
        pi.registerCommand("paveway", {description: "Paveway Runtime status and management", handler: async (args, ctx) => {
            if (!admitted) stop("Paveway command before Runtime admission");
            try {
                if (args.trim() === "cache") {
                    const usage = await runtimeRequest(config.socket_path, "cache.stats",
                        {conversation_id: config.conversation_id});
                    ctx.ui.notify(JSON.stringify(usage, null, 2), "info");
                    return;
                }
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
            ctx.ui.notify(`GPT · ${config.coach_id}/${config.task_id}\nCurrent directory: ${ctx.cwd}\nConversation: ${config.conversation_id}\nSession: ${ctx.sessionManager.getSessionId()}`, "info");
        }});
    } catch { stop(stage); }
}
