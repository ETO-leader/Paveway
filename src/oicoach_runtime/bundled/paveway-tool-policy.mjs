// OMP 19.2 rejects --no-tools with extensions; an empty --tools list alone
// still auto-includes extension, custom and MCP tools in both supported hosts.
export async function registerDisabledTools(pi, fail) {
    // Both hosts call explicit factories before SDK discovery. Suppress only
    // these capability sources, not the providers' unrelated extensions,
    // models, rules or skills. The registry lives only in this OMP process.
    const {getCapability} = await import("@oh-my-pi/pi-coding-agent/capability");
    const capabilities = [getCapability("mcps"), getCapability("tools")];
    for (const capability of capabilities) {
        if (!Array.isArray(capability?.providers)) throw new Error("OMP tool discovery registry unavailable");
        if (!Object.isFrozen(capability.providers)) {
            capability.providers.length = 0;
            Object.freeze(capability.providers);
        }
    }

    const requireDisabled = () => {
        if (capabilities.some(capability => capability.providers.length !== 0) || pi.getActiveTools().length !== 0)
            throw new Error("OMP has active tools in a managed tool-disabled session");
    };
    const apply = async () => {
        if (pi.getActiveTools().length !== 0) await pi.setActiveTools([]);
        requireDisabled();
    };
    pi.on("before_agent_start", async () => {
        try { await apply(); } catch (error) { fail(error); }
    });
    pi.on("before_provider_request", () => {
        try { requireDisabled(); } catch (error) { fail(error); }
    });
    pi.on("tool_call", () => ({block: true, reason: "Tools are disabled by the Paveway Runtime"}));
    return apply;
}
