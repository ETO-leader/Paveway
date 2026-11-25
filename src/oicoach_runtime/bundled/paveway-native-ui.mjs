import {createBanner} from "./paveway-banner.mjs";
import {registerComposer} from "./paveway-composer.mjs";

const STATUS = "paveway.windline";
const WELCOME = "paveway.welcome";
const isInteractiveMain = ctx => ctx.hasUI && ctx.mode === "tui" && ctx.agent?.kind === "main";

const shortId = (value, width) => value.length > width ? value.slice(0, width - 1) + "~" : value;

function windline({profile, task, workflow}, {busy, inbox, wake}, width, theme) {
    if (width <= 0) return [];
    const paint = (role, text) => theme.fg(role, text);
    const divider = theme.getSymbolPreset() === "ascii" ? " | " : " · ";
    const flags = [
        ...(busy ? [["accent", "Busy"]] : []),
        ...(wake === "none" ? [] : [[wake === "unavailable" || wake === "?" ? "warning" : "text", `Wake ${wake}`]]),
        ...(inbox === 0 ? [] : [[inbox === "?" ? "warning" : "success", `Inbox ${inbox}`]]),
    ];
    const prefix = "PAVEWAY";
    const flagsWidth = flags.reduce((sum, [, label]) => sum + divider.length + label.length, 0);
    const identityWidth = Math.min(41, width - prefix.length - 1 - flagsWidth);
    const parts = [paint("accent", prefix.slice(0, width))];
    let available = width - prefix.length;
    if (identityWidth >= 7) {
        const taskWidth = Math.min(task.length, Math.max(3, Math.floor((identityWidth - 1) / 2)));
        const identity = ` ${shortId(profile, identityWidth - taskWidth - 1)}/${shortId(task, taskWidth)}`;
        parts.push(paint("text", identity));
        available -= identity.length;
    }
    for (const [role, label] of flags) {
        if (available < divider.length + label.length) continue;
        parts.push(paint("muted", divider), paint(role, label));
        available -= divider.length + label.length;
    }
    if (workflow !== profile && available >= 15) {
        parts.push(paint("muted", `${divider}Workflow ${shortId(workflow, available - 12)}`));
    }
    return [parts.join("")];
}

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
    const identity = {profile, task, workflow};
    let statusUI, display, requestRender;
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
        const busy = state.busy ?? !ctx.isIdle();
        const inbox = Number.isSafeInteger(state.inbox) && state.inbox >= 0 ? state.inbox : "?";
        const wake = wakeStatus(state.wakeup);
        if (ui !== statusUI || !display || busy !== display.busy || inbox !== display.inbox || wake !== display.wake) {
            display = {busy, inbox, wake};
            if (ui !== statusUI) requestRender = undefined;
            if (ui !== statusUI && ui.setWidget) {
                ui.setWidget(STATUS, (tui, theme) => {
                    requestRender = () => tui.requestRender();
                    return {
                        render: width => windline(identity, display, width, ui.theme ?? theme),
                        dispose() { statusUI = requestRender = undefined; },
                    };
                }, {placement: "belowEditor"});
            } else if (requestRender) {
                requestRender();
            } else if (ui.setStatus) {
                ui.setStatus(STATUS, `PAVEWAY ${profile}/${task}${busy ? " · Busy" : ""}` +
                    `${wake !== "none" ? ` · Wake ${wake}` : ""}${inbox !== 0 ? ` · Inbox ${inbox}` : ""}`);
            }
            statusUI = ui;
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
