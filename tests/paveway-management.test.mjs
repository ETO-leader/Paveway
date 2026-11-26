import assert from "node:assert/strict";
import {mkdtempSync, mkdirSync, realpathSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import test from "node:test";
import {registerManagement} from "../src/oicoach_runtime/bundled/paveway-management.mjs";
import {openSettings} from "../src/oicoach_runtime/bundled/paveway-settings.mjs";

const main = {hasUI: true, mode: "tui", agent: {kind: "main"}};
const management = (config, {exec, switchSession} = {}) => registerManagement({
    exec: exec ?? (() => assert.fail("cannot execute a CLI operation during cancellation")),
}, {workflow_id: "test", cli_prefix: ["paveway"], config_path: "private-config.toml", ...config},
switchSession ?? (() => assert.fail("cannot switch the session")));

for (const omp_version of ["19.1.1", "19.2.1"]) {
    test(`OMP ${omp_version} credentials use only the verified masked input`, async () => {
        for (const [action, title] of [["login", "VJudge password"], ["cookie", "VJudge Cookie"]]) {
            const prompts = [];
            await management({omp_version}).run("vjudge", {...main, ui: {
                select: async () => action,
                input: async (...args) => {
                    prompts.push(args);
                    return args[0] === "VJudge username" ? "fixture-user" : undefined;
                },
                notify: message => assert.fail(message),
            }});
            assert.deepEqual(prompts, [...(action === "login" ? [["VJudge username"]] : []), [title, undefined, {secret: true}]]);
        }
    });
}

test("unknown and nonterminal hosts cannot prompt for a credential", async () => {
    for (const [ctx, omp_version] of [[main, "19.2.99"], [{...main, mode: "rpc"}, "19.2.1"]]) {
        const notices = [];
        await management({omp_version}).run("vjudge", {...ctx, ui: {
            select: async () => "cookie",
            input: () => assert.fail("unverified input must stay closed"),
            notify: (...message) => notices.push(message),
        }});
        assert.equal(notices.length, 1);
        assert.match(notices[0][0], /no verified masked input/);
        assert.equal(notices[0][1], "error");
    }
});

test("OMP 19.2 cwd uses a native input; only confirmation hands off a validated directory", async t => {
    const workspace = realpathSync(mkdtempSync(join(tmpdir(), "paveway-cwd-test-")));
    t.after(() => rmSync(workspace, {recursive: true, force: true}));
    const target = join(workspace, "checkout path");
    mkdirSync(target);
    let consent = false;
    const handoffs = [];
    const flow = management({workspace, omp_version: "19.2.1"}, {
        exec: async (_command, args) => {
            assert.deepEqual(args.slice(0, 4), ["workflow", "tijian", "show", "test"]);
            return {code: 0, stdout: '{"configured":false}'};
        },
        switchSession: (_ctx, action) => handoffs.push(action),
    });
    const ctx = {...main, cwd: workspace, ui: {
        composer: {},
        input: async title => {
            assert.ok(title.includes(`Current directory: ${workspace}`));
            assert.ok(title.includes(`Workflow workspace: ${workspace}`));
            return '"checkout path"';
        },
        confirm: async (title, path) => {
            assert.equal(title, "Start a new conversation here?");
            assert.equal(path, target);
            return consent;
        },
        notify: message => assert.fail(message),
    }};
    await flow.run("cwd", ctx);
    assert.deepEqual(handoffs, []);
    consent = true;
    await flow.run("cwd", ctx);
    assert.deepEqual(handoffs, [{kind: "cwd", cwd: target}]);
});

test("settings uses a host rows view while reading and restores the selected preference", async () => {
    const screens = [];
    const indices = [];
    let models = 0;
    const ctx = {...main, ui: {
        custom: factory => new Promise(done => {
            const rows = factory({}, {fg: (_role, text) => text}, {}, done);
            screens.push(rows.render(80));
        }),
        select: async (title, options, {initialIndex}) => {
            assert.equal(title, "Paveway / Settings");
            assert.deepEqual(options.map(option => option.label), ["Tijian MCP: Configure", "Model preference: test/default", "Thinking preference: inherit"]);
            indices.push(initialIndex);
            return indices.length === 1 ? options[1].label : undefined;
        },
        notify: message => assert.fail(message),
    }};
    await openSettings(ctx, {
        preferences: async () => ({inference: {model: "test/default"}}),
        model: async () => { models++; },
    });
    assert.deepEqual(screens, [["Reading Paveway preferences…"], ["Reading Paveway preferences…"]]);
    assert.deepEqual(indices, [0, 1]);
    assert.equal(models, 1);
});
