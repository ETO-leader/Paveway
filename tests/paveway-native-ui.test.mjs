import {expect, test} from "bun:test";
import {registerNativePresentation} from "../src/oicoach_runtime/bundled/paveway-native-ui.mjs";

const config = {
    workflow_source: "training", coach_id: "olympiad", task_id: "practice",
    paveway_version: "2.0.0", omp_version: "19.1.1",
};

function host() {
    const handlers = new Map();
    const statuses = new Map();
    const widgets = new Map();
    const shapes = [];
    const pi = {
        on(event, handler) { handlers.set(event, handler); },
        registerComposerShape(shape) { shapes.push(shape); },
    };
    const ctx = {
        agent: {kind: "main"}, mode: "tui", hasUI: true, isIdle: () => true,
        ui: {
            setStatus(key, text) { statuses.set(key, text); },
            setWidget(key, widget, options) {
                if (widget) widgets.set(key, {widget, options});
                else widgets.delete(key);
            },
        },
    };
    return {pi, ctx, statuses, widgets, shapes, emit: (event, target = ctx) => handlers.get(event)?.({type: event}, target)};
}

const status = statuses => [...statuses.values()][0];

test("Paveway publishes Runtime facts as one native status, not a footer replacement", () => {
    const {pi, ctx, statuses, shapes} = host();
    const show = registerNativePresentation(pi, config);
    expect(shapes.map(shape => shape.style.id)).toEqual(["paveway.chrome"]);
    show(ctx, {wakeup: {timing: "1m2s"}, inbox: 2, busy: true});
    expect(statuses.size).toBe(1);
    expect(status(statuses)).toBe("PAVEWAY olympiad/practice (training) · Wake 1m2s · Inbox 2 · Busy");
    show(ctx, {wakeup: {running: true}, inbox: 0});
    expect(status(statuses)).toBe("PAVEWAY olympiad/practice (training) · Wake running");
    expect(status(statuses)).not.toContain("Busy");
    show({...ctx, isIdle: () => false}, {wakeup: {unavailable: true}});
    expect(status(statuses)).toContain("Wake unavailable · Inbox ? · Busy");
    show(ctx, {wakeup: {timing: "none"}, inbox: 0});
    expect(status(statuses)).toBe("PAVEWAY olympiad/practice (training)");
    show(ctx, {});
    expect(status(statuses)).toContain("Wake ? · Inbox ?");
});

test("the welcome is a width-bounded startup widget that disappears on main input", () => {
    const {pi, ctx, widgets, emit} = host();
    const show = registerNativePresentation(pi, config);
    show(ctx, {});
    expect(widgets.size).toBe(0);
    show(ctx, {}, {banner: true});
    expect(widgets.size).toBe(1);
    const [{widget, options}] = [...widgets.values()];
    expect(options.placement).toBe("aboveEditor");
    const tui = {terminal: {rows: 24}};
    const component = widget(tui, {getColorMode: () => "none", getSymbolPreset: () => "unicode"});
    for (const width of [80, 40, 16, 5]) {
        expect(component.render(width).every(row => Bun.stringWidth(row) <= width)).toBe(true);
    }
    const wide = component.render(80).join("\n");
    expect(wide).toMatch(/Paveway v2\.0\.0 [·|] training [·|] OMP 19\.1\.1/);
    tui.terminal.rows = 10;
    expect(component.render(80)).toHaveLength(1);
    emit("agent_start", {...ctx, agent: {kind: "subagent"}});
    show(ctx, {inbox: 1});
    expect(widgets.size).toBe(1);
    emit("input");
    expect(widgets.size).toBe(0);
    show(ctx, {inbox: 2});
    expect(widgets.size).toBe(0);
});

test("presentation never accesses child or headless UI and tolerates missing visual hooks", () => {
    const {pi, ctx, widgets, statuses} = host();
    delete pi.registerComposerShape;
    const show = registerNativePresentation(pi, config);
    show({...ctx, agent: {kind: "subagent"}}, {}, {banner: true});
    show({...ctx, hasUI: false}, {}, {banner: true});
    show({...ctx, mode: "rpc"}, {}, {banner: true});
    expect(widgets.size).toBe(0);
    expect(statuses.size).toBe(0);
    expect(() => show({...ctx, ui: {}}, {}, {banner: true})).not.toThrow();
});
