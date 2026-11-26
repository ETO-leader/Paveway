import {lstatSync, readFileSync, writeSync} from "node:fs";
import {registerMemoryContext} from "./paveway-memory-context.mjs";
import {runtimeRequest} from "./paveway-runtime.mjs";
import {registerDisabledTools} from "./paveway-tool-policy.mjs";

export default async function memoryContext(pi) {
    const fail = (_error, diagnostic) => {
        writeSync(1, JSON.stringify({type: "paveway_memory_error", contractVersion: 1,
            code: diagnostic?.code || "memory_recall_failed", phase: diagnostic?.phase || "before_dispatch"}) + "\n");
        writeSync(2, "paveway_memory_failed: required Runtime Memory context unavailable\n");
        process.kill(process.pid, "SIGKILL");
    };
    const failTools = () => {
        writeSync(1, JSON.stringify({type: "paveway_tools_error", contractVersion: 1}) + "\n");
        process.kill(process.pid, "SIGKILL");
    };
    try {
        const path = process.env.PAVEWAY_MEMORY_DATA;
        const info = lstatSync(path);
        if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o077))
            throw new Error("Memory launch data is not private");
        const config = JSON.parse(readFileSync(path, "utf8"));
        if (config.tools_enabled === false) {
            try {
                const apply = await registerDisabledTools(pi, failTools);
                const enforce = async (event, ctx) => {
                    if (ctx.agent?.kind !== "main") return;
                    try {
                        await apply();
                        if (event.type === "session_start")
                            writeSync(1, JSON.stringify({type: "paveway_tools_ready", contractVersion: 1, runId: config.run_id}) + "\n");
                    } catch { failTools(); }
                };
                pi.on("session_start", enforce);
                pi.on("session_switch", enforce);
            } catch { failTools(); }
        }
        if (config.scope) {
            registerMemoryContext(pi, {
                scope: config.scope,
                enabled: config.enabled,
                recallPhase: "after_dispatch",
                recall: known => config.socket_path
                    ? runtimeRequest(config.socket_path, "memory.recall", {...config.scope, known})
                    : JSON.parse(readFileSync(path, "utf8")).snapshot,
                fail,
            });
            writeSync(1, JSON.stringify({type: "paveway_memory_ready", contractVersion: 1, runId: config.run_id}) + "\n");
        }
    } catch (error) { fail(error); }
}
