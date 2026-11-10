#!/usr/bin/env bun
// Local-only pressure study using the production Memory selector; no provider calls.
// bun examples/stress_memory_context.mjs [engine-module] [temporary-output-directory] [OMP-source]
import {createHash} from "node:crypto";
import {mkdtempSync, mkdirSync, readFileSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {dirname, resolve, join} from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {performance} from "node:perf_hooks";
import {spawnSync} from "node:child_process";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const modulePath = resolve(process.argv[2] || join(root, "src/oicoach_runtime/bundled/paveway-memory-context.mjs"));
const directory = process.argv[3] ? resolve(process.argv[3]) : mkdtempSync(join(tmpdir(), "paveway-memory-stress-"));
// Refuse accidental writes into a real session/configuration tree.
if (!directory.startsWith(`${tmpdir()}/`)) throw new Error("Output directory must be under the OS temporary directory");
mkdirSync(directory, {recursive: true});
const {planMemory, memoryState} = await import(pathToFileURL(modulePath).href);
const producer = spawnSync(process.env.PYTHON || "python3",
    [join(root, "examples/prepare_memory_pressure.py"), directory, "1000"], {
        env: {...process.env, PYTHONPATH: join(root, "src")}, encoding: "utf8",
    });
if (producer.status !== 0) throw new Error(producer.stderr || "Production Memory fixture generation failed");
const fixture = JSON.parse(producer.stdout);
const sourceRoot = process.argv[4] || process.env.PAVEWAY_OMP_SOURCE;
let tokenizer;
if (sourceRoot) {
    const {Tokenizer} = await import(pathToFileURL(join(resolve(sourceRoot), "packages/agent/src/tokenizer.ts")).href);
    tokenizer = new Tokenizer();
}
const scope = {workflow_id: "stress", task_id: "synthetic"};
const timestamp = "2026-01-01T00:00:00.000Z";
const limit = 16 * 1024 * 1024;
const checkpoints = new Set([100, 300, 500, 1000]);
const bytes = value => Buffer.byteLength(value);
const line = value => JSON.stringify(value) + "\n";
const noteBytes = fixture.note_content_bytes;
const notes = fixture.initial.notes;
let current = notes;
let parentId = null;
let nextId = 0;
const entries = [];
const lines = [line({type: "session", version: 3, id: "synthetic-memory-pressure", timestamp, cwd: "/tmp"})];
let totalBytes = bytes(lines[0]);
let contentBytes = 0;
let contentJsonBytes = 0;
let detailsJsonBytes = 0;
let stateJsonBytes = 0;
let contentTokens = tokenizer ? 0 : null;
const contentDigest = createHash("sha256");
let firstCrossing;
const rows = [];
function append(value) {
    const entry = {id: `entry-${++nextId}`, parentId, timestamp, ...value};
    parentId = entry.id;
    entries.push(entry);
    const serialized = line(entry);
    lines.push(serialized);
    totalBytes += bytes(serialized);
}
function timing(operation) {
    for (let i = 0; i < 2; i++) operation();
    const samples = [];
    for (let i = 0; i < 5; i++) {
        const start = performance.now();
        operation();
        samples.push(performance.now() - start);
    }
    const sorted = [...samples].sort((a, b) => a - b);
    return {samples_ms: samples, median_ms: sorted[2], warmups: 2};
}
function usage(path) {
    const script = `import json,sys\nfrom oicoach_runtime.cache_usage import session_usage\ntry:\n result=session_usage(sys.argv[1], 'synthetic-memory-pressure')\n print(json.dumps({'accepted':True,'call_count':result['cumulative']['call_count']}))\nexcept Exception as error:\n print(json.dumps({'accepted':False,'error_type':type(error).__name__,'error':str(error)}))\n`;
    const child = spawnSync(process.env.PYTHON || "python3", ["-c", script, path], {
        env: {...process.env, PYTHONPATH: join(root, "src")}, encoding: "utf8",
    });
    if (child.status !== 0) throw new Error(child.stderr || `Python exited ${child.status}`);
    return JSON.parse(child.stdout);
}
function save(name, text) {
    const path = join(directory, name);
    writeFileSync(path, text);
    return {path, bytes: bytes(text), usage: usage(path)};
}
for (let round = 0; round <= 1000; round++) {
    if (round) {
        const index = (round - 1) % notes.length;
        current = current.map((note, i) => i === index ? {...note, version: note.version + 1,
            content: String(round).padStart(8, "0") + "x".repeat(noteBytes - 8)} : note);
    }
    // Replay the real persisted branch, rather than retaining planMemory's private state.
    const previous = memoryState(entries, scope);
    const receipt = planMemory({...fixture.initial, notes: current}, previous, scope);
    if (!receipt) throw new Error(`Missing changed receipt at round ${round}`);
    if (round && receipt.content.length > 10000) throw new Error("Single-note update unexpectedly resends selected notes");
    contentBytes += bytes(typeof receipt.content === "string" ? receipt.content : JSON.stringify(receipt.content));
    contentDigest.update(receipt.content);
    if (tokenizer) contentTokens += tokenizer.countTokens(receipt.content, "strict");
    contentJsonBytes += bytes(JSON.stringify(receipt.content));
    detailsJsonBytes += bytes(JSON.stringify(receipt.details ?? null));
    stateJsonBytes += receipt.details?.state ? bytes(JSON.stringify(receipt.details.state)) : 0;
    const beforeLines = lines.length;
    const beforeBytes = totalBytes;
    append({type: "custom_message", ...receipt});
    append({type: "message", message: {role: "user", content: [{type: "text", text: "Continue synthetic task."}], timestamp: Date.parse(timestamp)}});
    append({type: "message", message: {role: "assistant", content: [{type: "text", text: "Synthetic acknowledgement."}], api: "anthropic-messages", provider: "anthropic", model: "synthetic-no-provider-call", usage: {input: 100, output: 10, cacheRead: 80, cacheWrite: 0, totalTokens: 190, cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}}, stopReason: "stop", timestamp: Date.parse(timestamp)}});
    if (!firstCrossing && totalBytes > limit) {
        firstCrossing = {round, before_bytes: beforeBytes, after_bytes: totalBytes,
            below: save("crossing-below.jsonl", lines.slice(0, beforeLines).join("")),
            above: save("crossing-above.jsonl", lines.join(""))};
    }
    if (!checkpoints.has(round)) continue;
    const text = lines.join("");
    const archive = save(`round-${round}.jsonl`, text);
    const parsed = text.trimEnd().split("\n").map(item => JSON.parse(item));
    const restored = memoryState(parsed.slice(1), scope);
    if (JSON.stringify(restored.notes) !== JSON.stringify(current)) throw new Error(`Replay mismatch at ${round}`);
    if (round === 1000 && JSON.stringify(current) !== JSON.stringify(fixture.final.notes))
        throw new Error("Synthetic update sequence diverged from actual Memory.put/context results");
    rows.push({round, entries: entries.length, total_bytes: totalBytes, model_visible_memory_utf8_bytes: contentBytes,
        model_visible_memory_json_bytes: contentJsonBytes, details_json_bytes: detailsJsonBytes,
        model_visible_memory_strict_tokens: contentTokens, model_content_sha256: contentDigest.copy().digest("hex"),
        state_json_bytes: stateJsonBytes, other_journal_bytes: totalBytes - contentJsonBytes - detailsJsonBytes,
        json_parse: timing(() => text.trimEnd().split("\n").map(item => JSON.parse(item))),
        memory_state_replay: timing(() => memoryState(parsed.slice(1), scope)), archive});
}
const result = {schema: 1, engine_module: modulePath, engine_sha256: createHash("sha256").update(readFileSync(modulePath)).digest("hex"),
    node: process.version, output_directory: directory,
    workload: {selected_note_content_bytes: notes.length * noteBytes, selected_notes_json_bytes: bytes(JSON.stringify(notes)),
        selected_context_json_bytes: bytes(JSON.stringify(fixture.initial)), note_count: notes.length,
        update: "one note per round, cyclic selection, fixed content length, CAS-style version increments", initial_receipts: 1,
        compactions: 0, paid_provider_calls: 0, journal_format: "OMP v3 session/custom_message/user/assistant shapes; synthetic values"},
    producer: fixture.producer,
    token_counter: tokenizer ? "OMP Tokenizer.countTokens(text, strict); appended Memory text only, not provider billing" : "unavailable; supply pinned OMP source",
    byte_accounting: "Cumulative persisted Memory contents (not cumulative provider prompt tokens); JSON costs include value serialization, not property names. state_json_bytes is a subset of details_json_bytes.",
    limit_bytes: limit, first_crossing: firstCrossing ?? null, checkpoints: rows};
writeFileSync(join(directory, "report.json"), JSON.stringify(result, null, 2) + "\n");
console.log(JSON.stringify(result, null, 2));
