import {lstatSync, readFileSync, writeSync} from "node:fs";
import {registerMemoryContext} from "./paveway-memory-context.mjs";
import {runtimeRequest} from "./paveway-runtime.mjs";

export default function memoryContext(pi) {
    const fail = () => {
        writeSync(2, "paveway_memory_failed: required Runtime Memory context unavailable\n");
        process.kill(process.pid, "SIGKILL");
    };
    try {
        const path = process.env.PAVEWAY_MEMORY_DATA;
        const info = lstatSync(path);
        if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o077))
            throw new Error("Memory launch data is not private");
        const config = JSON.parse(readFileSync(path, "utf8"));
        registerMemoryContext(pi, {
            scope: config.scope,
            enabled: config.enabled,
            recall: known => config.socket_path
                ? runtimeRequest(config.socket_path, "memory.recall", {...config.scope, known})
                : JSON.parse(readFileSync(path, "utf8")).snapshot,
            fail,
        });
        writeSync(1, JSON.stringify({type: "paveway_memory_ready", contractVersion: 1, runId: config.run_id}) + "\n");
    } catch (error) { fail(error); }
}
