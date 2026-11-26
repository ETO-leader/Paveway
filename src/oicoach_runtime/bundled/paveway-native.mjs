// Thin native OMP extension. Runtime remains the owner of durable operations.
import {createHash} from "node:crypto";
import {lstatSync, readFileSync, readSync, realpathSync, writeFileSync, writeSync} from "node:fs";
import {isAbsolute, relative, sep} from "node:path";
import {fileURLToPath} from "node:url";
import {runtimeRequest} from "./paveway-runtime.mjs";
import {registerWakeups} from "./paveway-wakeup.mjs";
import {registerManagement} from "./paveway-management.mjs";
import {registerMemory} from "./paveway-memory.mjs";
import {registerMemoryContext} from "./paveway-memory-context.mjs";
import {registerNativePresentation} from "./paveway-native-ui.mjs";
import {registerDisabledTools} from "./paveway-tool-policy.mjs";

const admissionDescriptor = Number(process.env.PAVEWAY_NATIVE_FD);
const admissionNonce = process.env.PAVEWAY_NATIVE_NONCE;

const extensionDirectory = fileURLToPath(new URL(".", import.meta.url));
const errorTypes = /^(?:Error|TypeError|SyntaxError|RangeError|ReferenceError|URIError|EvalError|AggregateError|DOMException)$/;
const safeMessages = new Map([
    ["Composer shape id must be a non-empty trimmed string", "composer_id"],
    ["Runtime socket is not private", "runtime_socket"],
    ["Runtime unavailable; no automatic retry", "runtime_unavailable"],
    ["Launch data is not an owned private regular file", "launch_file"],
    ["Launch channel or nonce is invalid", "launch_channel"],
    ["Session context is missing required UI, branch, prompt or cwd", "context"],
    ["Host does not support managed composer registration", "composer_api"],
    ["Session keybinding interface is not supported", "resume_keys"],
    ["Guardian did not acknowledge admission", "rejected_ack"],
]);

function startupDiagnostic(error) {
    if (error === undefined) return undefined;
    const exception_type = error instanceof Error && errorTypes.test(error.name) ? error.name : "ThrownValue";
    const message_code = safeMessages.get(error?.message) ??
        (["ENOENT", "EACCES", "EPERM"].includes(error?.code) ? error.code : "redacted");
    const stack = [];
    for (const line of (typeof error?.stack === "string" ? error.stack.slice(0, 12000).split("\n") : [])) {
        const text = line.trim();
        if (!text.startsWith("at ")) continue;
        const frame = text.endsWith(")") ? text.slice(text.lastIndexOf(" (") + 2, -1) : text.slice(3);
        const match = /^(.+?)(?:\?mtime=\d+)?:([0-9]{1,7}):([0-9]{1,6})$/.exec(frame);
        if (!match) continue;
        const [, source, row, column] = match;
        const path = source.startsWith("file://") ? source.slice(7) : source;
        const local = path.startsWith(extensionDirectory) ? path.slice(extensionDirectory.length) : "";
        const file = /^paveway-[a-z-]{1,40}\.mjs$/.test(local) ? local :
            source.startsWith("/$bunfs/root/") ? "omp-host" : undefined;
        if (file) stack.push(`${file}:${row}:${column}`);
        if (stack.length === 12) break;
    }
    return {exception_type, message_code, stack};
}

function stop(stage, error) {
    try {
        if (Number.isInteger(admissionDescriptor) && admissionDescriptor >= 3)
            writeSync(admissionDescriptor, JSON.stringify({failure: stage, nonce: admissionNonce,
                diagnostic: startupDiagnostic(error)}) + "\n");
    } finally { process.kill(process.pid, "SIGKILL"); }
}

async function installResumeGuard(ui) {
    let matches;
    if (ui.composer) {
        const [{getKeybindings}, {decodeInput}] = await Promise.all([
            import("@oh-my-pi/pi-coding-agent"),
            import("@oh-my-pi/pi-tui/driver/input/decoder"),
        ]);
        if (typeof getKeybindings !== "function" || typeof decodeInput !== "function")
            throw new TypeError("Session keybinding interface is not supported");
        const keys = getKeybindings();
        matches = data => decodeInput(data).some(event => event.type === "key" && keys.matches(event.key, "app.session.resume"));
    } else {
        const {getKeybindings} = await import("@oh-my-pi/pi-tui");
        if (typeof getKeybindings !== "function")
            throw new TypeError("Session keybinding interface is not supported");
        const keys = getKeybindings();
        matches = data => keys.matches(data, "app.session.resume");
    }
    const remove = ui.onTerminalInput(data => {
        if (!matches(data)) return;
        ui.notify(resumeNotice, "warning");
        return {consume: true};
    });
    if (typeof remove !== "function") throw new TypeError("Session keybinding interface is not supported");
    return remove;
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
    let stage = "launch_data";
    try {
        const file = process.env.PAVEWAY_NATIVE_DATA;
        const info = lstatSync(file);
        if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o077))
            throw new Error("Launch data is not an owned private regular file");
        const config = JSON.parse(readFileSync(file, "utf8"));
        const descriptor = admissionDescriptor;
        const nonce = admissionNonce;
        if (!Number.isInteger(descriptor) || descriptor < 3 || !/^[a-f0-9]{64}$/.test(nonce ?? ""))
            throw new Error("Launch channel or nonce is invalid");
        stage = "source";
        const sha = createHash("sha256").update(readFileSync(new URL(import.meta.url))).digest("hex");
        let admitted = false;
        const statusState = {};
        let statusContext, removeResumeKeyGuard;
        stage = "register";
        if (typeof pi.registerComposerShape !== "function") throw new TypeError("Host does not support managed composer registration");
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
            if (!admitted || ctx.cwd !== config.cwd) stop("input_scope");
            const notice = managedInput(event.text);
            if (notice) {
                ctx.ui.notify(notice, "warning");
                return {handled: true};
            }
        });
        pi.on("before_agent_start", (_event, ctx) => {
            if (ctx.agent?.kind === "main" && (!admitted || ctx.cwd !== config.cwd))
                stop("agent_scope");
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
            fail: error => stop("memory_context", error),
        });
        stage = "managed_tools";
        const applyDisabledTools = config.tools_enabled ? null :
            await registerDisabledTools(pi, error => stop("managed_tools", error));
        const handshake = frame => {
            writeSync(descriptor, JSON.stringify({apiVersion: 1, nonce, pid: process.pid, ...frame}) + "\n");
            const ack = Buffer.alloc(1);
            if (readSync(descriptor, ack, 0, 1, null) !== 1 || ack[0] !== 65)
                throw new Error("Guardian did not acknowledge admission");
        };
        const sessionReady = async (event, ctx) => {
            // Spawned sessions bind extensions too. Never grant their context
            // ownership of the main Conversation or its local control channel.
            if (ctx.agent?.kind !== "main") return;
            admitted = false;
            let stage = "context";
            try {
                const prompt = ctx.getSystemPrompt();
                if (!ctx.hasUI || ctx.mode !== "tui" || typeof ctx.ui.setWidget !== "function" ||
                    ctx.cwd !== config.cwd || !Array.isArray(ctx.sessionManager.getBranch()) ||
                    !(typeof prompt === "string" || (Array.isArray(prompt) && prompt.every(part => typeof part === "string"))))
                    throw new TypeError("Session context is missing required UI, branch, prompt or cwd");
                stage = "resume_keys";
                if (!removeResumeKeyGuard) removeResumeKeyGuard = await installResumeGuard(ctx.ui);
                stage = "managed_tools";
                if (applyDisabledTools) await applyDisabledTools();
                stage = "session_disk";
                if (config.materialize && event.type === "session_start") await ctx.sessionManager.ensureOnDisk();
                stage = "runtime_handshake";
                await runtimeRequest(config.socket_path, "status");
                handshake({extensionDigest: sha, sessionId: ctx.sessionManager.getSessionId(),
                    sessionFile: ctx.sessionManager.getSessionFile(), contextPolicy: "omp-native",
                    toolsDisabled: Boolean(applyDisabledTools)});
                stage = "runtime_owner";
                const owner = await runtimeRequest(config.socket_path, "frontend.show", {conversation_id: config.conversation_id});
                config.generation = owner.generation;
                statusContext = ctx;
                stage = "presentation";
                showPresentation(ctx, statusState, {banner: event.type === "session_start" && !statusState.started});
                statusState.started = true;
                stage = "wakeup";
                await wakeups.refresh(ctx);
                stage = "runtime_handshake";
                handshake({ready: true});
                admitted = true;
            } catch (error) { stop(stage, error); }
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
            if (!admitted) stop("command_scope");
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
    } catch (error) { stop(stage, error); }
}
