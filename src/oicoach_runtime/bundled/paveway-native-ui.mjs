import {createBanner} from "./paveway-banner.mjs";
import {registerComposer} from "./paveway-composer.mjs";

const STATUS = "paveway.windline";
const WELCOME = "paveway.welcome";
const isInteractiveMain = ctx => ctx.hasUI && ctx.mode === "tui" && ctx.agent?.kind === "main";

// Launch fields are validated ASCII IDs; bound long IDs before adding the live facts.
const shortId = value => value.length > 20 ? `${value.slice(0, 19)}~` : value;

function wakeStatus(wakeup) {
    if (wakeup?.unavailable) return "unavailable";
    if (wakeup?.running) return "running";
    const timing = wakeup?.timing;
    if (timing === "none" || (typeof timing === "string" && /^\d+[dhms](?:\d+[ms])?$/.test(timing))) return timing;
    return "?";
}

/** Register chrome at extension load; call the returned presenter only after admission. */
export function registerNativePresentation(pi, config) {
    registerComposer(pi);
    const {workflow_source: workflow, coach_id: profile, task_id: task, paveway_version: version, omp_version: ompVersion} = config;
    const identity = `PAVEWAY ${shortId(profile)}/${shortId(task)}${workflow === profile ? "" : ` (${shortId(workflow)})`}`;
    let welcomeUI;
    const dismissWelcome = (_event, ctx) => {
        if (!welcomeUI || !isInteractiveMain(ctx)) return;
        const ui = welcomeUI;
        welcomeUI = undefined;
        ui.setWidget(WELCOME, undefined);
    };
    // The welcome is not transcript content and must not take space during a turn.
    for (const event of ["input", "agent_start", "session_switch"]) pi.on(event, dismissWelcome);

    return function show(ctx, state, {banner = false} = {}) {
        if (!isInteractiveMain(ctx)) return;
        const ui = ctx.ui;
        if (ui.setStatus) {
            const busy = state.busy ?? !ctx.isIdle();
            const inbox = Number.isSafeInteger(state.inbox) && state.inbox >= 0 ? state.inbox : "?";
            const wake = wakeStatus(state.wakeup);
            ui.setStatus(STATUS, `${identity}${wake !== "none" ? ` · Wake ${wake}` : ""}` +
                `${inbox !== 0 ? ` · Inbox ${inbox}` : ""}${busy ? " · Busy" : ""}`);
        }
        if (banner && !welcomeUI && ui.setWidget) {
            ui.setWidget(WELCOME, (tui, theme) => {
                const render = createBanner({version, workflow}, theme);
                return {render: width => tui.terminal.rows < 18
                    ? width > 0 ? ["PAVEWAY".slice(0, width)] : []
                    : render(width, ompVersion)};
            }, {placement: "aboveEditor"});
            welcomeUI = ui;
        }
    };
}
