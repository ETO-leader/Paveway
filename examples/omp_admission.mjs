// M0 experiment, not a managed application entry. No stdout protocol frames.
import {createHash} from "node:crypto";
import {readFileSync, readSync, writeSync} from "node:fs";
import {installContextGuard, TOKEN_LIMIT} from "./context-window.mjs";

function stop() {
    try {
        writeSync(2, "paveway_admission_failed: native admission denied; session stopped\n");
    } finally {
        process.kill(process.pid, "SIGKILL");
    }
}

function digest(url) {
    const path = new URL(url);
    path.search = "";
    return createHash("sha256").update(readFileSync(path)).digest("hex");
}

export default async function admission(pi) {
    try {
        const descriptor = Number(process.env.PAVEWAY_ADMISSION_FD);
        const nonce = process.env.PAVEWAY_ADMISSION_NONCE;
        const guardDigest = digest(new URL("./context-window.mjs", import.meta.url));
        const entryDigest = digest(import.meta.url);
        if (!Number.isInteger(descriptor) || descriptor < 3 || !/^[a-f0-9]{64}$/.test(nonce ?? "") ||
            guardDigest !== process.env.PAVEWAY_ADMISSION_GUARD_SHA256 ||
            entryDigest !== process.env.PAVEWAY_ADMISSION_ENTRY_SHA256) stop();
        const reply = Buffer.allocUnsafe(1);
        let sequence = 0;
        let admitted = false;
        let sessionId;
        const exchange = (phase, extra = {}) => {
            const bytes = Buffer.from(JSON.stringify({apiVersion: 1, phase, nonce, pid: process.pid,
                sequence: sequence++, guardDigest, entryDigest, tokenLimit: TOKEN_LIMIT, ...extra}) + "\n");
            for (let offset = 0; offset < bytes.length;) {
                const written = writeSync(descriptor, bytes, offset, bytes.length - offset);
                if (written <= 0) stop();
                offset += written;
            }
            if (readSync(descriptor, reply, 0, 1, null) !== 1 || reply[0] !== 65) stop();
        };
        await installContextGuard({on(event, handler) {
            pi.on(event, (message, context) => {
                try {
                    if (!admitted || context.sessionManager.getSessionId() !== sessionId) stop();
                    // Synchronous per-request authorization: an EOF, rejected
                    // grant or broken channel cannot return unguarded context.
                    exchange("context", {sessionId});
                    return handler(message, context);
                } catch {
                    stop();
                }
            });
        }}, () => exchange("guard"));
        pi.on("session_start", (_event, context) => {
            try {
                if (!context.hasUI) stop();
                sessionId = context.sessionManager.getSessionId();
                if (!sessionId) stop();
                exchange("session", {sessionId});
                admitted = true;
            } catch {
                stop();
            }
        });
    } catch {
        stop();
    }
}
