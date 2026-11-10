// Real OMP SessionManager/context reconstruction only; no model or network calls.
import assert from "node:assert/strict";
import {mkdirSync, readFileSync, writeFileSync} from "node:fs";
import {resolve, join} from "node:path";
import {pathToFileURL} from "node:url";
import {MEMORY_TYPE, memoryState, planMemory, restoreCompactedMemory} from "../src/oicoach_runtime/bundled/paveway-memory-context.mjs";

const source = resolve(process.argv[2]);
const directory = resolve(process.argv[3]);
assert.ok(directory.startsWith("/tmp/"), "SDK probe must use disposable /tmp storage");
mkdirSync(directory, {recursive: true});
const {SessionManager} = await import(pathToFileURL(join(source, "packages/coding-agent/src/session/session-manager.ts")).href);
const scope = {workflow_id: "sdk-probe", task_id: "task"};
const note = (key, version = 1) => ({...scope, key, version, content: `SDK_CURRENT_${key}_V${version}`, kind: "fact"});
const usage = {input: 100, output: 10, cacheRead: 80, cacheWrite: 0, totalTokens: 190, cost: {total: 0}};
const user = content => ({role: "user", content, timestamp: Date.now()});
const assistant = () => ({role: "assistant", content: [{type: "text", text: "SDK reply"}], api: "anthropic-messages",
    provider: "synthetic", model: "local-no-call", usage, stopReason: "stop", timestamp: Date.now()});
const snapshot = (...notes) => ({notes, truncated: false});
function appendMemory(manager, notes) {
    const message = planMemory(snapshot(...notes), memoryState(manager.getBranch(), scope), scope);
    if (!message) return undefined;
    return manager.appendCustomMessageEntry(message.customType, message.content, message.display, message.details, message.attribution);
}
const context = manager => manager.buildSessionContext().messages;
const restore = manager => restoreCompactedMemory(context(manager), manager.getBranch(), scope);
const wire = messages => messages.map(message => typeof message.content === "string" ? message.content : JSON.stringify(message.content)).join("\n");
const report = {source, provider_calls: 0, checks: []};

{
    const manager = SessionManager.inMemory(directory);
    const root = manager.appendMessage(user("PRE_CLEAR_PRIVATE_CONVERSATION"));
    appendMemory(manager, [note("A")]);
    const original = manager.appendMessage(assistant());
    manager.appendCompaction("old summary", undefined, "", 100, {preserveData: {pavewayMemory: memoryState(manager.getBranch(), scope)}});
    manager.appendResetBoundary();
    assert.equal(context(manager).length, 0);
    assert.deepEqual(memoryState(manager.getBranch(), scope).notes, []);
    assert.deepEqual(restore(manager), []);
    assert.ok(appendMemory(manager, [note("A")]));
    assert.ok(wire(restore(manager)).includes(note("A").content));
    assert.ok(!wire(restore(manager)).includes("PRE_CLEAR_PRIVATE_CONVERSATION"));
    assert.equal(appendMemory(manager, [note("A")]), undefined);
    const postReset = manager.getLeafId();
    manager.branch(root);
    assert.deepEqual(memoryState(manager.getBranch(), scope).notes, []);
    manager.branch(original);
    assert.equal(memoryState(manager.getBranch(), scope).notes[0].content, note("A").content);
    manager.branch(postReset);
    manager.appendModelUsage({purpose: "late-sdk", api: "anthropic-messages", provider: "synthetic", model: "local-no-call", usage, stopReason: "stop"},
        {sessionId: manager.getSessionId(), parentId: original});
    assert.equal(manager.getLeafId(), postReset);
    assert.ok(!manager.getBranch().some(entry => entry.type === "model_usage"));
    assert.ok(!wire(restore(manager)).includes("PRE_CLEAR_PRIVATE_CONVERSATION"));
    report.checks.push("reset_actual_visible_range", "unchanged_reinjection_once", "branch_switch_and_late_usage");
}

for (const kind of ["soft", "custom", "anthropic", "openai-full", "openai-prefix"]) {
    const manager = SessionManager.inMemory(directory);
    manager.appendMessage(user("compaction input"));
    appendMemory(manager, [note("A")]);
    const through = manager.appendMessage(assistant());
    if (kind === "openai-prefix") appendMemory(manager, [note("A"), note("INTERVENING")]);
    const keptMemory = appendMemory(manager, kind === "openai-prefix"
        ? [note("A"), note("INTERVENING"), note("B")] : [note("A"), note("B")]);
    const checkpoint = memoryState(manager.getBranch(), scope);
    const options = {preserveData: {pavewayMemory: checkpoint}};
    let firstKept = "";
    if (kind === "custom") delete options.preserveData.pavewayMemory;
    if (kind === "anthropic") {
        options.preserveData.anthropicCompaction = {provider: "synthetic", content: "native summary", signature: "fixture-signature", exactTail: true};
        options.providerReplayThroughEntryId = through;
    }
    if (kind.startsWith("openai-")) {
        options.preserveData.openaiRemoteCompaction = {provider: "synthetic", replacementHistory: [{type: "compaction", encrypted_content: "opaque-fixture"}]};
        if (kind === "openai-prefix") options.providerReplayThroughEntryId = through;
        firstKept = keptMemory;
    }
    manager.appendCompaction("native host summary", undefined, firstKept, 100, options);
    const messages = context(manager);
    assert.equal(messages[0].role, "compactionSummary", `${kind}: native host always supplies the supported anchor`);
    const before = restore(manager);
    const text = wire(before);
    assert.equal(text.split(note("A").content).length - 1, 1, `${kind}: lost or duplicated A`);
    assert.equal(text.split(note("B").content).length - 1, 1, `${kind}: lost or duplicated B`);
    if (kind === "openai-prefix")
        assert.equal(text.split(note("INTERVENING").content).length - 1, 1, "native prefix replay must retain the replay-to-tail interval");
    const frozen = before.find(message => message.customType === MEMORY_TYPE + "-restore").content;
    manager.appendMessage(user("future input"));
    appendMemory(manager, [note("A", 2)]);
    assert.equal(restore(manager).find(message => message.customType === MEMORY_TYPE + "-restore").content, frozen,
        `${kind}: future update/deletion changed an earlier restoration frame`);
    manager.appendResetBoundary();
    assert.deepEqual(restore(manager), [], `${kind}: reset restored an older provider payload`);
    report.checks.push(`${kind}_summary_and_exact_memory`, `${kind}_frozen_prefix_and_reset`);
}

{
    const manager = SessionManager.create(directory, join(directory, "sessions"));
    await manager.ensureOnDisk();
    const root = manager.appendMessage(user("root"));
    const oldLeaf = manager.appendMessage(assistant());
    manager.branch(root);
    manager.appendMessage(user("new branch"));
    const successor = manager.appendMessage(assistant());
    manager.appendModelUsage({purpose: "late-off-branch", api: "anthropic-messages", provider: "synthetic", model: "local-no-call", usage, stopReason: "stop"},
        {sessionId: manager.getSessionId(), parentId: oldLeaf});
    assert.equal(manager.getLeafId(), successor);
    await manager.flush();
    const path = manager.getSessionFile();
    const before = readFileSync(path, "utf8");
    const tail = JSON.parse(before.trimEnd().split("\n").at(-1));
    assert.equal(tail.type, "model_usage");
    assert.equal(tail.parentId, oldLeaf);
    manager.branch(root);
    await manager.flush();
    assert.equal(readFileSync(path, "utf8"), before, "live branch navigation must not be mistaken for persisted state");
    report.persisted_usage_fixture = {path, session_id: manager.getSessionId(), live_leaf: root,
        physical_tail: tail.id, recorded_replay_leaf: tail.activeLeafId, physical_tail_parent: oldLeaf};
    await manager.close();
    report.checks.push("real_physical_title_and_off_branch_usage", "live_leaf_changes_without_disk_record");
}
writeFileSync(join(directory, "report.json"), JSON.stringify(report, null, 2) + "\n");
console.log(JSON.stringify(report));
