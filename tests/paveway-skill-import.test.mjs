import test from "node:test";
import assert from "node:assert/strict";
import {analyzeSkillSource, parseSkillDraft, reviewSkillImport, selectLocalSkills, validateSkillDraft} from "../src/oicoach_runtime/bundled/paveway-skill-import.mjs";

const proposal = () => ({name: "fixture-skill", category: "personal-skill", description: "Use for fixture analysis",
    content: "# Fixture\nPreserve constraints and do not invent facts.", analysis: Object.fromEntries(
        ["purpose", "triggers", "inputs", "workflow", "outputs", "constraints", "dependencies", "warnings"].map(key => [key, "Fixture " + key]))});

test("local selection preserves bodies, toggles enabled, confirms replacements and submits one batch", async () => {
    const preview = {revision: 7, resources_not_installed: ["reference.md"], skills: [
        {name: "existing", description: "Existing", content: "\r\n正文\r\n", category: "personal-skill", enabled: false, conflict: true, source: "a/SKILL.md"},
        {name: "new", description: "New", content: "Untouched", category: "personal-skill", enabled: true, conflict: false, source: "b/SKILL.md"}]};
    const actions = ["全选", "切换启停", "existing", "查看正文", "existing", "导入所选项"];
    let writes = 0;
    const ctx = {ui: {select: async () => actions.shift(), editor: async (_title, body) => {assert.equal(body, "\r\n正文\r\n"); return "Ignored edit";},
        confirm: async (_title, summary) => {assert.ok(summary.includes("替换 / 启用")); assert.ok(summary.includes("额外资源不会安装")); return true;}}};
    assert.equal(await selectLocalSkills(ctx, preview, async payload => {
        writes++;
        assert.deepEqual(payload.replace, ["existing"]);
        assert.equal(payload.skills[0].content, "\r\n正文\r\n");
        assert.equal(payload.skills[0].enabled, true);
        assert.equal(payload.skills[0].source, undefined);
        assert.equal(payload.skills.length, 2);
    }), true);
    assert.equal(writes, 1);
    assert.equal(preview.skills[0].enabled, false);
});

test("local import cancel, declined confirmation and save conflict do not report success", async () => {
    const preview = {revision: 0, resources_not_installed: [], skills: []};
    assert.equal(await selectLocalSkills({ui: {select: async () => "取消"}}, preview, () => assert.fail("cancel saved")), false);
    const actions = ["全选", "导入所选项", "取消"];
    const skill = {name: "fixture", description: "Fixture", content: "Body", enabled: true, conflict: false};
    const ctx = {ui: {select: async () => actions.shift(), confirm: async () => false}};
    assert.equal(await selectLocalSkills(ctx, {...preview, skills: [skill]}, () => assert.fail("declined saved")), false);
    const conflictActions = ["全选", "导入所选项"];
    ctx.ui.select = async () => conflictActions.shift(); ctx.ui.confirm = async () => true;
    await assert.rejects(selectLocalSkills(ctx, {...preview, skills: [skill]}, async () => {throw new Error("revision conflict");}), /revision conflict/);
});

test("draft validation rejects partial, malformed and oversized proposals without returning model excerpts", () => {
    assert.deepEqual(parseSkillDraft("```json\n" + JSON.stringify(proposal()) + "\n```"), proposal());
    for (const draft of [{...proposal(), name: "Bad Name"}, {...proposal(), category: "auto-execute"},
        {...proposal(), content: "中".repeat(21846)}, {...proposal(), analysis: {}}, {...proposal(), enabled: true}])
        assert.throws(() => validateSkillDraft(draft));
    assert.throws(() => parseSkillDraft("PRIVATE_BAD_PROVIDER_RESPONSE"), error => !error.message.includes("PRIVATE_BAD"));
});

test("analysis sends selected file only, with current model and no tools or conversation history", async () => {
    const source = {documents: [{path: "SKILL.md", text: "UNTRUSTED_SOURCE_INSTRUCTIONS"}]};
    let call;
    const ctx = {model: {id: "fixture"}, modelRegistry: {getApiKeyAndHeaders: async () => ({ok: true, apiKey: "private-fixture", headers: {fixture: "yes"}})},
        ui: {custom: handler => new Promise(done => handler({}, {}, {}, done))}};
    const draft = await analyzeSkillSource({getThinkingLevel: () => "off"}, ctx, source, async () => ({
        Loader: class {}, complete: async (...args) => { call = args; return {stopReason: "stop", content: [{type: "text", text: JSON.stringify(proposal())}]}; },
    }));
    assert.equal(draft.name, "fixture-skill");
    assert.equal(call[0], ctx.model);
    assert.equal(call[1].messages.length, 1);
    assert.equal(call[1].tools, undefined);
    assert.ok(call[1].messages[0].content[0].text.includes("UNTRUSTED_SOURCE_INSTRUCTIONS"));
    assert.ok(!call[1].systemPrompt.join().includes("UNTRUSTED_SOURCE_INSTRUCTIONS"));
    assert.equal(call[2].disableReasoning, true);
    assert.ok(!JSON.stringify(draft).includes("private-fixture"));
});

test("cancelling analysis ignores a late provider result", async () => {
    let loader, finishProvider, signal;
    const ctx = {model: {id: "fixture"}, modelRegistry: {getApiKeyAndHeaders: async () => ({ok: true, apiKey: "fixture"})},
        ui: {custom: handler => new Promise(done => handler({}, {}, {}, done))}};
    const pending = analyzeSkillSource({getThinkingLevel: () => "off"}, ctx, {}, async () => ({
        Loader: class { constructor() { loader = this; } },
        complete: (_model, _context, options) => { signal = options.signal; return new Promise(resolve => finishProvider = resolve); },
    }));
    while (!finishProvider) await new Promise(resolve => setTimeout(resolve, 0));
    loader.onAbort();
    assert.equal(await pending, undefined);
    assert.equal(signal.aborted, true);
    finishProvider({stopReason: "stop", content: [{type: "text", text: JSON.stringify(proposal())}]});
});

test("model errors and output truncation cannot produce a saved or partial draft", async () => {
    for (const stopReason of ["error", "length", "toolUse"]) {
        const ctx = {model: {id: "fixture"}, modelRegistry: {getApiKeyAndHeaders: async () => ({ok: true, apiKey: "fixture"})},
            ui: {custom: handler => new Promise(done => handler({}, {}, {}, done))}};
        await assert.rejects(analyzeSkillSource({getThinkingLevel: () => "off"}, ctx, {}, async () => ({
            Loader: class {}, complete: async () => ({stopReason, errorMessage: "PRIVATE_ERROR_WITH_TOKEN", content: []}),
        })), error => !error.message.includes("PRIVATE_ERROR"));
    }
});

test("review requires explicit save/overwrite; conflicts retain the editable draft", async () => {
    const actions = ["编辑名称", "保存 Skill", "保存 Skill", "返回（保留草稿）"];
    let writes = 0, confirmations = 0;
    const ctx = {ui: {select: async () => actions.shift(), input: async () => "existing-skill",
        confirm: async title => { assert.equal(title, "替换已有 Skill？"); return ++confirmations > 1; }, notify: () => {}}};
    const result = await reviewSkillImport(ctx, proposal(), [{name: "existing-skill"}], async () => { writes++; throw new Error("conflict"); });
    assert.equal(writes, 1);
    assert.equal(result.saved, false);
    assert.equal(result.draft.name, "existing-skill");
    assert.equal(proposal().name, "fixture-skill");
});
