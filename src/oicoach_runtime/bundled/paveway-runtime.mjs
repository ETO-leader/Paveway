import {randomUUID} from "node:crypto";
import {lstatSync} from "node:fs";
import {createConnection} from "node:net";
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
