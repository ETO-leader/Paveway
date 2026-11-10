import test from "node:test";
import assert from "node:assert/strict";
import {MEMORY_TYPE, memoryState, planMemory, registerMemoryContext, restoreCompactedMemory} from "../src/oicoach_runtime/bundled/paveway-memory-context.mjs";

const scope = {workflow_id: "profile", task_id: "training"};
const note = (key = "preference", version = 1, extra = {}) => ({...scope, task_id: "", key, version, content: "prefer hints", kind: "preference", ...extra});
const snapshot = (...notes) => ({notes, truncated: false});
const empty = () => ({schema: 1, scope, notes: []});
const entry = (message, id = "memory-1") => ({type: "custom_message", id, ...message});
const modelMessage = message => ({role: "custom", ...message});
const compact = (state, id = "compact-1", firstKeptEntryId = "") => ({type: "compaction", id, firstKeptEntryId, timestamp: "2026-11-09T00:00:00Z", preserveData: {pavewayMemory: state}});
const summary = {role: "compactionSummary", summary: "Lossy summary"};

test("only changed scoped notes are delivered; stable notes are not resent", () => {
    const initial = planMemory(snapshot(note("a"), note("b")), empty(), scope);
    assert.equal(planMemory(snapshot(note("b"), note("a")), initial.details.state, scope), undefined);
    const changed = planMemory(snapshot(note("a", 2, {content: "new"}), note("b"), note("c")), initial.details.state, scope);
    assert.deepEqual(changed.details.delta.upserts.map(n => n.key), ["a", "c"]);
    assert.deepEqual(changed.details.delta.withdrawals, []);
    assert.ok(!changed.content.includes('"key":"b"'));
});

test("content changes cannot hide behind unchanged snapshot or note versions", () => {
    const first = planMemory(snapshot(note()), empty(), scope);
    const update = planMemory(snapshot(note("preference", 1, {content: "corrected historical fact"})), first.details.state, scope);
    assert.equal(update.details.delta.upserts[0].content, "corrected historical fact");
});

test("delete, expiration, bounded omission and resurrection have explicit semantics", () => {
    const initial = planMemory(snapshot(note("a"), note("b"), note("c")), empty(), scope);
    const removed = planMemory({notes: [], withdrawn: [
        {...note("a"), reason: "deleted"}, {...note("b"), reason: "expired"}], truncated: true}, initial.details.state, scope);
    assert.deepEqual(removed.details.delta.withdrawals.map(n => n.reason), ["deleted", "expired", "outside_current_context"]);
    assert.deepEqual(removed.details.state.notes, []);
    assert.match(removed.content, /not current facts/);
    assert.equal(planMemory(snapshot(), removed.details.state, scope), undefined);
    const revived = planMemory(snapshot(note("a", 3)), removed.details.state, scope);
    assert.equal(revived.details.delta.upserts[0].version, 3);
});

test("workflow and task isolation are enforced", () => {
    assert.throws(() => planMemory(snapshot(note("a", 1, {workflow_id: "foreign"})), empty(), scope));
    assert.throws(() => planMemory(snapshot(note("a", 1, {task_id: "foreign"})), empty(), scope));
    const foreign = {schema: 1, scope: {...scope, task_id: "foreign"}, notes: [note()]};
    assert.equal(planMemory(snapshot(note()), foreign, scope).details.delta.upserts.length, 1);
});

test("restart restores only accepted branch receipts; failed staging commits nothing", async () => {
    const handlers = {};
    const original = [Object.freeze({type: "message", id: "user", message: {role: "user", content: "hello"}})];
    let calls = 0;
    registerMemoryContext({on: (name, handler) => handlers[name] = handler}, {
        scope, recall: async () => { calls++; return snapshot(note()); }, fail: error => {throw error;},
    });
    const ctx = {agent: {kind: "main"}, sessionManager: {getBranch: () => original}};
    const first = await handlers.before_agent_start({reason: "prompt"}, ctx);
    const retry = await handlers.before_agent_start({reason: "prompt"}, ctx);
    assert.deepEqual(first, retry);
    assert.equal(original.length, 1);
    original.push(entry(first.message));
    assert.equal(await handlers.before_agent_start({reason: "prompt"}, ctx), undefined);
    assert.equal(planMemory(snapshot(note()), memoryState(original, scope), scope), undefined);
    assert.equal(calls, 3);
    assert.equal(await handlers.before_agent_start({reason: "prompt"}, {...ctx, agent: {kind: "task"}}), undefined);
    assert.equal(calls, 3);
});

test("recall failure cannot create delivery evidence", async () => {
    const handlers = {};
    const branch = [];
    let failures = 0;
    registerMemoryContext({on: (name, handler) => handlers[name] = handler}, {
        scope, recall: async () => {throw new Error("runtime unavailable");}, fail: () => {failures++;},
    });
    const ctx = {agent: {kind: "main"}, sessionManager: {getBranch: () => branch}};
    await assert.rejects(handlers.before_agent_start({reason: "prompt"}, ctx), /runtime unavailable/);
    assert.equal(failures, 1);
    assert.deepEqual(memoryState(branch, scope), empty());
});

test("compaction restores missing selected data at a stable anchor without growing history", () => {
    const initial = planMemory(snapshot(note("a"), note("b")), empty(), scope);
    const entries = [entry(initial), compact(initial.details.state)];
    const tail = {role: "user", content: "continue"};
    const messages = [summary, tail];
    const restored = restoreCompactedMemory(messages, entries, scope);
    assert.equal(restored[0], summary);
    assert.equal(restored[2], tail);
    assert.deepEqual(restored[1].details, {compaction_id: "compact-1"});
    assert.deepEqual(JSON.parse(restored[1].content.split("\n")[1]).upserts.map(n => n.key), ["a", "b"]);
    assert.deepEqual(restoreCompactedMemory(messages, entries, scope), restored);
    assert.deepEqual(restoreCompactedMemory(restored, entries, scope), restored);
    assert.deepEqual(messages, [summary, tail]);
    assert.equal(planMemory(snapshot(note("a"), note("b")), memoryState(entries, scope), scope), undefined);
    assert.equal(entries.length, 2);
});

test("compaction retained notes are not restored twice and later withdrawals override restoration", () => {
    const initial = planMemory(snapshot(note("a")), empty(), scope);
    const added = planMemory(snapshot(note("a"), note("b")), initial.details.state, scope);
    const entries = [entry(initial), entry(added, "memory-2"), compact(added.details.state, "compact-1", "memory-2")];
    const messages = [summary, modelMessage(added)];
    const restored = restoreCompactedMemory(messages, entries, scope);
    assert.deepEqual(JSON.parse(restored[1].content.split("\n")[1]).upserts.map(n => n.key), ["a"]);
    const removed = planMemory(snapshot(note("b")), added.details.state, scope);
    const later = restoreCompactedMemory([summary, modelMessage(added), modelMessage(removed)], [...entries, entry(removed)], scope);
    assert.deepEqual(later[1], restored[1], "later Memory edits must not rewrite the restoration prefix");
    assert.equal(later.at(-1).details.delta.withdrawals[0].key, "a");
});

test("compaction during staged update retains old checkpoint followed by the accepted correction", () => {
    const initial = planMemory(snapshot(note()), empty(), scope);
    const update = planMemory(snapshot(note("preference", 2, {content: "new value"})), initial.details.state, scope);
    const branch = [entry(initial), compact(initial.details.state)];
    const outbound = [summary, {role: "user", content: "update"}, modelMessage(update)];
    const restored = restoreCompactedMemory(outbound, branch, scope);
    assert.equal(restored.at(-1), outbound.at(-1));
    assert.deepEqual(JSON.parse(restored[1].content.split("\n")[1]).upserts.map(n => n.version), [1]);
    const next = memoryState([...branch, entry(update)], scope);
    assert.equal(next.notes[0].version, 2);
});

test("model context keeps Memory user-level, raw user input and prior messages untouched", async () => {
    const handlers = {};
    registerMemoryContext({on: (name, handler) => handlers[name] = handler},
        {scope, recall: async () => snapshot(note()), fail: error => {throw error;}});
    const companion = planMemory(snapshot(note()), empty(), scope);
    const prior = Object.freeze({role: "assistant", content: "earlier reply"});
    const user = Object.freeze({role: "user", content: "exact original request"});
    const ctx = {agent: {kind: "main"}, sessionManager: {getBranch: () => [entry(companion)]}};
    const incoming = [prior, user, modelMessage(companion)];
    const result = await handlers.context({messages: incoming}, ctx);
    assert.equal(result.messages[0], prior);
    assert.equal(result.messages[2], user);
    assert.equal(result.messages[1].role, "user");
    assert.equal(incoming[2].role, "custom");
    assert.equal(result.messages[1].content, companion.content);
});

test("disabling Memory withdraws accepted notes without recalling disabled storage", async () => {
    const first = planMemory(snapshot(note()), empty(), scope);
    const branch = [entry(first)];
    const handlers = {};
    registerMemoryContext({on: (name, handler) => handlers[name] = handler}, {
        scope, enabled: false, recall: () => {throw new Error("disabled storage accessed");},
        fail: error => {throw error;},
    });
    const ctx = {agent: {kind: "main"}, sessionManager: {getBranch: () => branch}};
    const result = await handlers.before_agent_start({reason: "prompt"}, ctx);
    assert.equal(result.message.details.delta.withdrawals[0].reason, "memory_disabled");
    branch.push(entry(result.message, "withdrawal"));
    assert.equal(await handlers.before_agent_start({reason: "prompt"}, ctx), undefined);
    const wire = handlers.context({messages: [modelMessage(first)]}, ctx);
    assert.equal(wire.messages[0].role, "user");
});

test("Memory precedes raw or Skill input, not an unrelated extension companion", () => {
    const handlers = {};
    registerMemoryContext({on: (name, handler) => handlers[name] = handler},
        {scope, recall: () => snapshot(), fail: error => {throw error;}});
    const memory = modelMessage(planMemory(snapshot(note()), empty(), scope));
    const ctx = {agent: {kind: "main"}, sessionManager: {getBranch: () => []}};
    for (const input of [{role: "user", content: "plain"}, {
        role: "custom", customType: "skill-prompt", attribution: "user", content: "expanded Skill"}]) {
        const other = {role: "custom", customType: "configured-companion", attribution: "user", content: "other"};
        const result = handlers.context({messages: [input, other, memory]}, ctx).messages;
        assert.equal(result[0].content, memory.content);
        assert.equal(result[1], input);
        assert.equal(result[2], other);
    }
});

test("extension-owned compaction without preserveData recovers the immutable accepted branch", () => {
    const initial = planMemory(snapshot(note()), empty(), scope);
    const checkpoint = {type: "compaction", id: "external", firstKeptEntryId: "", timestamp: "2026-11-09T00:00:00Z"};
    const branch = [entry(initial), checkpoint];
    const original = restoreCompactedMemory([summary], branch, scope);
    assert.equal(JSON.parse(original[1].content.split("\n")[1]).upserts[0].version, 1);
    const update = planMemory(snapshot(note("preference", 2)), initial.details.state, scope);
    const later = restoreCompactedMemory([summary, modelMessage(update)], [...branch, entry(update)], scope);
    assert.deepEqual(later[1], original[1]);
    assert.equal(later[2].details.delta.upserts[0].version, 2);
});
