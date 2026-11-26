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
    let redraws = 0;
    const tui = {terminal: {rows: 24}, requestRender() { redraws++; }};
    const theme = {fg: (_role, text) => text, getColorMode: () => "none", getSymbolPreset: () => "unicode"};
    const pi = {
        on(event, handler) { handlers.set(event, handler); },
        registerComposerShape(shape) { shapes.push(shape); },
    };
    const ctx = {
        agent: {kind: "main"}, mode: "tui", hasUI: true, isIdle: () => true,
        ui: {
            setStatus(key, text) { statuses.set(key, text); },
            setWidget(key, widget, options) {
                widgets.get(key)?.component.dispose?.();
                if (widget) widgets.set(key, {component: widget(tui, theme), options});
                else widgets.delete(key);
            },
        },
    };
    return {pi, ctx, statuses, widgets, shapes, tui, redraws: () => redraws,
        emit: (event, target = ctx) => handlers.get(event)?.({type: event}, target)};
}

test("Windline fits live runtime facts in narrow terminals and doesn't redraw unchanged state", () => {
    const {pi, ctx, statuses, widgets, redraws} = host();
    const show = registerNativePresentation(pi, config);
    const active = {wakeup: {timing: "1m2s"}, inbox: 2, busy: true};
    show(ctx, active);
    const [{component, options}] = [...widgets.values()];
    expect(options.placement).toBe("belowEditor");
    expect(statuses.size).toBe(0);
    const line = component.render(42)[0];
    expect(Bun.stringWidth(line)).toBeLessThanOrEqual(42);
    for (const label of ["PAVEWAY", "Wake 1m2s", "Inbox 2", "Busy"]) expect(line).toContain(label);
    show(ctx, {...active});
    expect(redraws()).toBe(0);
    show(ctx, {wakeup: {running: true}, inbox: 0});
    expect(redraws()).toBe(1);
    expect(component.render(80)[0]).toContain("Wake running");
    expect(component.render(80)[0]).not.toMatch(/\b(?:Inbox|Busy)\b/);
    show(ctx, {wakeup: {timing: "none"}, inbox: 0});
    const idle = component.render(80)[0];
    expect(idle).toContain("olympiad/practice");
    expect(idle).toContain("Workflow training");
    expect(idle).not.toMatch(/\b(?:Wake|Inbox|Busy)\b/);
});

test("the welcome is a width-bounded startup widget that disappears on main input", () => {
    const {pi, ctx, widgets, emit, tui} = host();
    const show = registerNativePresentation(pi, config);
    show(ctx, {});
    expect(widgets.has("paveway.welcome")).toBe(false);
    show(ctx, {}, {banner: true});
    const {component, options} = widgets.get("paveway.welcome");
    expect(options.placement).toBe("aboveEditor");
    // Render the installed widget at several physical terminal dimensions.
    for (const width of [80, 40, 16, 5]) {
        expect(component.render(width).every(row => Bun.stringWidth(row) <= width)).toBe(true);
    }
    const wide = component.render(80).join("\n");
    expect(wide).toMatch(/Paveway v2\.0\.0 [·|] training [·|] OMP 19\.1\.1/);
    tui.terminal.rows = 10;
    expect(component.render(80)).toHaveLength(1);
    emit("agent_start", {...ctx, agent: {kind: "subagent"}});
    show(ctx, {inbox: 1});
    expect(widgets.has("paveway.welcome")).toBe(true);
    emit("input");
    expect(widgets.has("paveway.welcome")).toBe(false);
    show(ctx, {inbox: 2});
    expect(widgets.has("paveway.welcome")).toBe(false);
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
    show({...ctx, ui: {setStatus: (key, text) => statuses.set(key, text)}}, {inbox: 1, wakeup: {timing: "none"}});
    expect(statuses.get("paveway.windline")).toBe("PAVEWAY olympiad/practice · Inbox 1");
});

test("OMP 19.2 widgets use the lazy size-based host and the current theme", () => {
    const {pi, ctx, shapes, statuses, emit} = host();
    const factories = new Map();
    let rows = 24;
    let redraws = 0;
    let placement;
    const widgetHost = {size: () => ({rows, columns: 80}), requestRender: () => redraws++};
    ctx.ui.theme = {fg: (_role, text) => text, depth: "truecolor", symbols: {preset: "unicode"}};
    ctx.ui.setWidget = (key, factory, opts) => {
        if (factory) factories.set(key, factory);
        else factories.delete(key);
        if (key === "paveway.windline") placement = opts.placement;
    };
    const show = registerNativePresentation(pi, {...config, omp_version: "19.2.1"});
    expect(shapes[0].id).toBe("paveway.chrome");
    show(ctx, {inbox: 2}, {banner: true});
    show(ctx, {inbox: 3});
    expect(placement).toBe("belowEditor");
    expect(statuses.size).toBe(0);
    const status = factories.get("paveway.windline")(widgetHost, ctx.ui.theme);
    const welcome = factories.get("paveway.welcome")(widgetHost, ctx.ui.theme);
    expect(status.render(80)[0]).toContain(" · Inbox 3");
    expect(Bun.stripANSI(welcome.render(80).join("\n"))).toContain("OMP 19.2.1");
    show(ctx, {inbox: 4});
    expect(redraws).toBe(1);
    ctx.ui.theme = {...ctx.ui.theme, symbols: {preset: "ascii"}};
    expect(status.render(80)[0]).toContain(" | Inbox 4");
    expect(Bun.stripANSI(welcome.render(80).join("\n"))).not.toMatch(/[█▀▄─●…]/);
    rows = 10;
    expect(welcome.render(80)).toEqual(["PAVEWAY"]);
    emit("agent_start");
    expect(factories.has("paveway.welcome")).toBe(false);
});
