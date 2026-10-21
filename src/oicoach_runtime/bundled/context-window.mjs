// OMP context hook: modifies only outbound copies, never the persisted session.
export const TOKEN_LIMIT = 10000;
const NOTICE = "[Runtime: older conversation or oversized content was omitted to fit the 10,000-token conversation budget. Use memory/history tools for missing details; do not guess.]";

export function windowMessages(original, count, limit = TOKEN_LIMIT) {
    const size = messages => count(JSON.stringify(messages));
    if (size(original) <= limit) return original;
    let messages = original.slice();
    // Drop entire old user turns, preserving tool-call/result adjacency.
    const lastUser = messages.findLastIndex(message => message.role === "user");
    if (lastUser > 0) {
        while (size(messages) > limit - 200) {
            const next = messages.findIndex((message, index) => index > 0 && message.role === "user");
            if (next < 0) break;
            messages = messages.slice(next);
        }
    }
    // A single tool-heavy turn may itself exceed the budget. Remove oldest
    // complete assistant/tool exchanges, never leave an orphaned tool result.
    while (size(messages) > limit - 200 && messages.length > 1) {
        const start = messages[0].role === "user" ? 1 : 0;
        let end = start + 1;
        while (end < messages.length && messages[end].role === "toolResult") end++;
        messages.splice(start, end - start);
    }
    // An individual large input is visibly shortened, including multibyte text.
    if (size(messages) > limit - 200) {
        const first = messages[0];
        let text = typeof first?.content === "string" ? first.content :
            (first?.content ?? []).filter(block => block.type === "text").map(block => block.text).join("\n");
        const chars = Array.from(text);
        let low = 0, high = chars.length;
        const make = n => [{role: "user", content: chars.slice(0, Math.ceil(n / 2)).join("") +
            "\n[Runtime: middle of oversized input omitted]\n" +
            (n > 1 ? chars.slice(-Math.floor(n / 2)).join("") : ""), timestamp: first?.timestamp ?? 0}];
        while (low < high) {
            const mid = Math.ceil((low + high) / 2);
            if (size(make(mid)) <= limit - 200) low = mid;
            else high = mid - 1;
        }
        messages = make(low);
    }
    // The notice is part of the same budget and is always visible to the model.
    const notice = {role: "user", content: NOTICE, timestamp: 0};
    const result = [notice, ...messages];
    if (size(result) > limit) return [notice];
    return result;
}

export default async function contextWindow(pi) {
    // Resolve through the host OMP installation, not the user's workspace.
    const {Tokenizer} = await import(Bun.resolveSync("@oh-my-pi/pi-agent-core", process.argv[1]));
    const tokenizer = new Tokenizer();
    const count = text => tokenizer.countTokens(text, "strict");
    pi.on("context", event => ({messages: windowMessages(event.messages, count)}));
    process.stdout.write(JSON.stringify({type: "paveway_context_ready", tokenLimit: TOKEN_LIMIT}) + "\n");
}
