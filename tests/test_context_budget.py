import json
import os
import shutil
import subprocess
import unittest
from importlib.resources import files
from pathlib import Path
import tempfile
import uuid
from dataclasses import replace

from oicoach_runtime.context_budget import CONTEXT_PROMPT, LIMIT_TOKENS, count_tokens
from oicoach_runtime.adapters.omp_rpc import OmpRpc
from oicoach_runtime.config import load_config


class ContextBudgetTests(unittest.TestCase):
    def test_token_count_is_not_bytes_and_accepts_special_token_text(self):
        self.assertLess(count_tokens("a " * 15000), len(("a " * 15000).encode()))
        self.assertGreater(count_tokens("漢字🙂" * 5000), LIMIT_TOKENS)
        self.assertGreater(count_tokens("<|endoftext|>"), 0)
        self.assertIn("10,000-token", CONTEXT_PROMPT)

    @unittest.skipUnless(shutil.which("bun") and shutil.which("omp"), "native OMP tokenizer requires bun and omp")
    def test_native_window_budget_tool_pairs_multibyte_and_archive_immutability(self):
        module = str(files("oicoach_runtime").joinpath("bundled/context-window.mjs"))
        host = str(Path(shutil.which("omp")).resolve())
        script = r'''
const {Tokenizer} = await import(Bun.resolveSync("@oh-my-pi/pi-agent-core", process.argv[1]));
const {windowMessages,conversationJson} = await import(process.argv[2]);
const tokenizer = new Tokenizer();
const count = text => tokenizer.countTokens(text, "strict");
const u = text => ({role:"user",content:text,timestamp:1});
const a = text => ({role:"assistant",content:[{type:"text",text}],timestamp:2});
const old = [];
for(let i=0;i<40;i++) old.push(u("OLD_"+i+" 漢字 ".repeat(300)),a("reply ".repeat(300)));
const call = {role:"assistant",content:[{type:"toolCall",id:"call-1",name:"bash",arguments:{command:"echo test"}}]};
const result = {role:"toolResult",toolCallId:"call-1",toolName:"bash",content:[{type:"text",text:"OK"}]};
const recent = [u("LATEST_USER"),call,result,a("LATEST_REPLY")];
const bookkeeping = [u("LATEST_USER"),call,{...result,details:{localOnly:"metadata ".repeat(100000)}},a("LATEST_REPLY")];
const longTurn = [u("LATEST_USER")];
for(let i=0;i<12;i++) {
    longTurn.push({role:"assistant",content:[...(i===0?[{type:"thinking",thinking:"PRIVATE_THOUGHTS"}]:[]),
        {type:"toolCall",id:"long-"+i,name:"bash",arguments:{command:"query-"+i}}]},
        {role:"toolResult",toolCallId:"long-"+i,toolName:"bash",content:[{type:"text",text:"FOUND_PUBLIC_CANDIDATE_"+i+" data ".repeat(5000)}]});
}
const fixtures = [[],[u("short")], [...old,...recent],
    [u("HUGE_HEAD"+"漢字🙂".repeat(20000)+"HUGE_TAIL")],
    [u("LATEST_USER"),call,{...result,content:[{type:"text",text:"tool data ".repeat(40000)}]}],
    [u("LATEST_USER"),{...call,content:[{type:"toolCall",id:"call-1",name:"bash",arguments:{command:"x".repeat(100000)}}]},result],
    [{role:"toolResult",content:[{type:"text",text:"orphan ".repeat(20000)}]}],longTurn,bookkeeping];
const checks = fixtures.map(messages=>{
    const before=JSON.stringify(messages);
    const out=windowMessages(messages,count);
    const calls=new Set();
    let valid=true;
    for(const message of out){
        if(message.role==="assistant") for(const block of message.content??[]) if(block.type==="toolCall") calls.add(block.id);
        if(message.role==="toolResult"&&!calls.has(message.toolCallId)) valid=false;
    }
    return {tokens:count(conversationJson(out)), originalUnchanged:before===JSON.stringify(messages),valid,
            text:conversationJson(out), preserved:out===messages};
});
console.log(JSON.stringify(checks));
'''
        process = subprocess.run([shutil.which("bun"), "-e", script, host, module],
                                 capture_output=True, text=True, timeout=40)
        self.assertEqual(process.returncode, 0, process.stderr)
        checks = json.loads(process.stdout)
        for check in checks:
            self.assertLessEqual(check["tokens"], LIMIT_TOKENS)
            self.assertTrue(check["originalUnchanged"])
            self.assertTrue(check["valid"])
        self.assertIn("LATEST_REPLY", checks[2]["text"])
        self.assertIn("LATEST_USER", checks[2]["text"])
        self.assertNotIn("OLD_0 ", checks[2]["text"])
        self.assertIn("HUGE_HEAD", checks[3]["text"])
        self.assertIn("HUGE_TAIL", checks[3]["text"])
        self.assertIn("omitted", checks[3]["text"])
        for check in checks[4:6]:
            self.assertIn("LATEST_USER", check["text"])
        self.assertTrue(checks[-1]["preserved"])
        self.assertIn("LATEST_REPLY", checks[-1]["text"])
        self.assertIn("OK", checks[-1]["text"])
        self.assertIn("earlier tool activity", checks[-2]["text"])
        self.assertIn("FOUND_PUBLIC_CANDIDATE_10", checks[-2]["text"])
        self.assertNotIn("PRIVATE_THOUGHTS", checks[-2]["text"])


@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG"), "set OICOACH_TEST_REAL_OMP_CONFIG for model acceptance")
class RealContextBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_input_is_filtered_for_model_but_preserved_in_session(self):
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-context-model-") as directory:
            root = Path(directory)
            session = root / "sessions"
            session.mkdir(mode=0o700)
            config = replace(base, agent=replace(base.agent, tools_enabled=False, skills_enabled=False,
                extensions_enabled=False, rules_enabled=False, turn_timeout_seconds=120,
                settle_timeout_seconds=20, graceful_stop_seconds=1))
            instruction = ("Reply with the unique identifier MEMORY_MARKER_ followed by 32 hexadecimal characters "
                           "if one is visible in this message. If none is visible, reply exactly MISSING. "
                           "Ignore filler; do not guess or call tools.\n")
            marker = "MEMORY_MARKER_" + uuid.uuid4().hex
            message = instruction
            message += "漢字🙂 filler " * 10000 + "\n" + marker + "\n" + "漢字🙂 filler " * 10000
            message += "\n" + instruction
            adapter = OmpRpc(config)
            try:
                await adapter.start({"workspace": str(root), "session_dir": str(session), "session_file": None}, "context-model")
                await adapter.ready()
                result = await adapter.prompt("context-model", message, lambda: None, lambda _: None)
                self.assertEqual(result, "completed", getattr(adapter, "failure_message", None))
                reply = (await adapter.command("get_last_assistant_text"))["text"]
                self.assertEqual(reply.strip(), "MISSING")
                state = await adapter.state()
            finally:
                await adapter.stop()
            archive = Path(state["sessionFile"]).read_text()
            self.assertIn(marker, archive)
            self.assertGreater(len(archive), len(message))
