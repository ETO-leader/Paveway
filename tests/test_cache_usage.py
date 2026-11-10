import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.cache_usage import session_usage
from oicoach_runtime.cli import parser, remote_arguments
from oicoach_runtime.errors import ConflictError, NotFoundError, RuntimeContractError


class CacheUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "session.jsonl"
        self.header = {"type": "session", "version": 3, "id": "test-session", "cwd": "/synthetic-workspace"}

    def write(self, entries, header=None):
        self.path.write_text("".join(json.dumps(entry) + "\n" for entry in [header or self.header, *entries]), encoding="utf-8")
        return self.path

    def call(self, identity="a", parent=None, usage=None, **extra):
        return {"type": "message", "id": identity, "parentId": parent,
                "message": {"role": "assistant", "api": "anthropic-messages", "provider": "anthropic",
                            "model": "synthetic-model", "usage": usage,
                            "content": [{"type": "text", "text": "PRIVATE RESPONSE NOT RETURNED"}],
                            "errorMessage": "PRIVATE PROVIDER ERROR", **extra}}

    def usage(self, **extra):
        return {"input": 20, "output": 5, "cacheRead": 60, "cacheWrite": 20,
                "totalTokens": 105, "cost": {"total": 0.25}, **extra}

    def test_empty_archive_and_calls_without_usage_are_unavailable(self):
        result = session_usage(self.write([]))
        self.assertEqual(result["calls"], [])
        self.assertEqual(result["cumulative"]["status"], "unavailable")
        self.assertEqual(result["cumulative"]["call_count"], 0)
        self.assertIsNone(result["cumulative"]["cache_hit_ratio"])
        user = {"type": "message", "id": "u", "parentId": None,
                "message": {"role": "user", "content": "PRIVATE USER INPUT"}}
        result = session_usage(self.write([user, self.call(parent="u")]))
        self.assertEqual(result["cumulative"]["call_count"], 1)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                    "cache_hit_ratio", "omp_normalized_input_tokens", "omp_calculated_cost_usd"):
            self.assertIsNone(result["calls"][0][key])
            self.assertIsNone(result["cumulative"][key])
        encoded = json.dumps(result)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("content", result["calls"][0])

    def test_positive_cache_metrics_have_explicit_normalized_denominator(self):
        result = session_usage(self.write([self.call(usage=self.usage())]), "test-session")
        call = result["calls"][0]
        self.assertEqual(call["input_tokens"], 20)
        self.assertEqual(call["output_tokens"], 5)
        self.assertEqual(call["cache_read_tokens"], 60)
        self.assertEqual(call["cache_write_tokens"], 20)
        self.assertEqual(call["omp_normalized_input_tokens"], 100)
        self.assertEqual(call["cache_hit_ratio"], 0.6)
        self.assertTrue(call["on_last_persisted_entry_ancestry"])
        self.assertEqual(result["cumulative"]["cache_hit_ratio"], 0.6)
        self.assertIn("OMP normalized", result["semantics"]["input_denominator"])
        self.assertEqual(result["source"], "omp_session_jsonl")

    def test_normalized_zero_does_not_claim_provider_reported_zero(self):
        for usage in (self.usage(cacheRead=0, cacheWrite=0, cost={"total": 0}),
                      {"input": 20, "output": 0},
                      self.usage(cacheRead=None, cacheWrite=None, cost=None)):
            with self.subTest(usage=usage):
                result = session_usage(self.write([self.call(usage=usage)]))
                call = result["calls"][0]
                self.assertIsNone(call["cache_read_tokens"])
                self.assertIsNone(call["cache_write_tokens"])
                self.assertIsNone(call["cache_hit_ratio"])
                self.assertIsNone(call["omp_normalized_input_tokens"])
                self.assertIsNone(call["omp_calculated_cost_usd"])
                self.assertIn("not provider-reported zero", result["semantics"]["cache"])
        result = session_usage(self.write([self.call(usage=self.usage(cacheWrite=0))]))
        self.assertEqual(result["calls"][0]["cache_read_tokens"], 60)
        self.assertIsNone(result["calls"][0]["cache_hit_ratio"])
        self.assertIsNone(result["calls"][0]["omp_normalized_input_tokens"])

    def test_partial_totals_preserve_positive_evidence_without_zero_filling(self):
        result = session_usage(self.write([
            self.call("a", usage=self.usage()),
            self.call("b", "a", self.usage(cacheRead=0, cacheWrite=0, cost={"total": 0})),
        ]))
        totals = result["cumulative"]
        self.assertEqual(totals["input_tokens"], 40)
        self.assertEqual(totals["output_tokens"], 10)
        self.assertIsNone(totals["cache_read_tokens"])
        self.assertIsNone(totals["cache_write_tokens"])
        self.assertIsNone(totals["omp_calculated_cost_usd"])
        self.assertIsNone(totals["cache_hit_ratio"])
        self.assertEqual(totals["observed_subtotals"]["cache_read_tokens"], 60)
        self.assertEqual(totals["observed_subtotals"]["omp_calculated_cost_usd"], 0.25)
        self.assertEqual(totals["available_call_counts"]["cache_read_tokens"], 1)

    def test_compaction_and_reopening_do_not_erase_session_cumulative_usage(self):
        self.write([self.call("before", usage=self.usage()),
                    {"type": "compaction", "id": "compact", "parentId": "before",
                     "summary": "PRIVATE COMPACTION SUMMARY", "firstKeptEntryId": "before", "tokensBefore": 1000},
                    self.call("after", "compact", self.usage())])
        first = session_usage(self.path)
        self.assertEqual(first["cumulative"]["call_count"], 2)
        self.assertEqual(first["cumulative"]["input_tokens"], 40)
        self.assertEqual(first["cumulative"]["cache_read_tokens"], 120)
        self.assertEqual(first["cumulative"]["omp_calculated_cost_usd"], 0.5)
        self.assertEqual(first["last_persisted_entry_ancestry"]["call_count"], 2)
        self.assertEqual(session_usage(Path(str(self.path))), first)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.call("resumed", "after", self.usage())) + "\n")
        restarted = session_usage(self.path)
        self.assertEqual(restarted["cumulative"]["call_count"], 3)
        self.assertEqual(restarted["cumulative"]["cache_read_tokens"], 180)
        self.assertNotIn("PRIVATE", json.dumps(restarted))

    def test_fork_statistics_describe_last_persisted_entry_not_live_branch(self):
        # branch("old") or resetLeaf() can change the live leaf without writing
        # another entry, so even an ordinary tail cannot prove the live branch.
        entries = [self.call("root", usage=self.usage()),
                   self.call("old", "root", self.usage()),
                   self.call("new", "root", self.usage())]
        result = session_usage(self.write(entries))
        ancestry = result["last_persisted_entry_ancestry"]
        self.assertEqual(result["cumulative"]["call_count"], 3)
        self.assertEqual(ancestry["call_count"], 2)
        self.assertEqual(ancestry["cache_read_tokens"], 120)
        self.assertEqual([call["on_last_persisted_entry_ancestry"] for call in result["calls"]], [True, False, True])
        self.assertEqual(result["cumulative"]["scope"], "session_file_all_calls_all_branches")
        self.assertEqual(ancestry["scope"], "last_persisted_entry_ancestry")
        self.assertIn("unavailable", result["semantics"]["live_active_branch"])
        self.assertNotIn("active_branch", result)
        self.assertNotIn("active_branch", result["semantics"])
        self.assertTrue(all("on_active_branch" not in call for call in result["calls"]))

    def test_final_off_branch_model_usage_uses_physical_tail_ancestry(self):
        entries = [self.call("root", usage=self.usage()),
                   self.call("old", "root", self.usage()),
                   self.call("new", "root", self.usage())]
        late = {"type": "model_usage", "id": "late", "parentId": "old",
                "purpose": "PRIVATE PURPOSE", "usage": self.usage()}
        # Late usage can leave the live leaf on "new". Older OMP writers omit
        # activeLeafId; newer writers record it for replay, not live navigation.
        for marker in ({}, {"activeLeafId": "new"}):
            with self.subTest(marker=marker):
                result = session_usage(self.write([*entries, {**late, **marker}]))
                self.assertEqual(result["cumulative"]["call_count"], 4)
                self.assertEqual(result["cumulative"]["cache_read_tokens"], 240)
                ancestry = result["last_persisted_entry_ancestry"]
                self.assertEqual(ancestry["call_count"], 3)
                self.assertEqual(ancestry["cache_read_tokens"], 180)
                self.assertEqual([call["on_last_persisted_entry_ancestry"] for call in result["calls"]],
                                 [True, True, False, True])
                self.assertEqual(result["calls"][-1]["entry_type"], "model_usage")
                self.assertEqual(session_usage(self.path), result)
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_active_leaf_metadata_is_not_used_for_physical_ancestry(self):
        for marker in ("missing", None, {"PRIVATE": "not-an-entry-id"}):
            with self.subTest(marker=marker):
                entry = {**self.call(usage=self.usage()), "activeLeafId": marker}
                result = session_usage(self.write([entry]))
                self.assertEqual(result["last_persisted_entry_ancestry"]["call_count"], 1)
                self.assertTrue(result["calls"][0]["on_last_persisted_entry_ancestry"])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_model_usage_and_mixed_provider_apis_keep_separate_call_identity(self):
        tool = {"type": "message", "id": "tool", "parentId": "a", "message": {
            "role": "toolResult", "toolName": "task", "details": {"usage": self.usage()},
            "content": "PRIVATE TOOL OUTPUT"}}
        auxiliary = {"type": "model_usage", "id": "aux", "parentId": "tool", "purpose": "PRIVATE PURPOSE",
                     "provider": "openai", "model": "synthetic-other", "api": "openai-responses",
                     "usage": self.usage(), "errorMessage": "PRIVATE PROVIDER ERROR"}
        result = session_usage(self.write([self.call(usage=self.usage()), tool, auxiliary]))
        self.assertEqual(result["cumulative"]["call_count"], 2)
        self.assertEqual(result["calls"][0]["api"], "anthropic-messages")
        self.assertEqual(result["calls"][1]["api"], "openai-responses")
        self.assertEqual(result["calls"][1]["entry_type"], "model_usage")
        self.assertEqual(result["calls"][1]["provider"], "openai")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_costs_are_omp_calculated_not_provider_invoices(self):
        result = session_usage(self.write([self.call(usage=self.usage())]))
        self.assertEqual(result["calls"][0]["omp_calculated_cost_usd"], 0.25)
        self.assertEqual(result["cumulative"]["omp_calculated_cost_usd"], 0.25)
        self.assertIn("OMP calculated USD cost", result["semantics"]["cost"])
        self.assertIn("not an actual provider invoice", result["semantics"]["cost"])

    def test_bounded_recent_calls_do_not_truncate_totals(self):
        entries = [self.call(f"c{i}", f"c{i - 1}" if i else None, self.usage()) for i in range(24)]
        result = session_usage(self.write(entries))
        self.assertTrue(result["calls_truncated"])
        self.assertEqual(len(result["calls"]), 20)
        self.assertEqual(result["calls"][0]["entry_id"], "c4")
        self.assertEqual(result["calls"][-1]["entry_id"], "c23")
        self.assertEqual(result["cumulative"]["call_count"], 24)
        self.assertEqual(result["cumulative"]["omp_calculated_cost_usd"], 6)
        self.assertEqual(result["last_persisted_entry_ancestry"]["call_count"], 24)
        self.assertEqual(result["calls_scope"], "latest_persisted_calls_all_branches")

    def test_legacy_archive_without_tree_does_not_invent_entry_ancestry(self):
        header = dict(self.header, version=1)
        entry = self.call(usage=self.usage())
        del entry["id"]
        del entry["parentId"]
        result = session_usage(self.write([entry], header))
        self.assertEqual(result["cumulative"]["call_count"], 1)
        self.assertIsNone(result["calls"][0]["on_last_persisted_entry_ancestry"])
        self.assertEqual(result["last_persisted_entry_ancestry"]["status"], "unavailable")
        self.assertEqual(result["last_persisted_entry_ancestry"]["reason"], "persisted_tree_unavailable")
        self.assertIsNone(result["last_persisted_entry_ancestry"]["call_count"])

    def test_invalid_metrics_and_free_text_metadata_are_not_returned(self):
        result = session_usage(self.write([self.call(usage={
            "input": True, "output": -1, "cacheRead": float("nan"), "cacheWrite": "PRIVATE VALUE",
            "cost": {"total": float("inf")}}, provider="PRIVATE PROVIDER TEXT", model="\nPRIVATE MODEL")]))
        call = result["calls"][0]
        self.assertIsNone(call["provider"])
        self.assertIsNone(call["model"])
        for metric in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "omp_calculated_cost_usd"):
            self.assertIsNone(call[metric])
        self.assertNotIn("PRIVATE", json.dumps(result, allow_nan=False))

    def test_missing_archive_and_mismatched_session_have_safe_errors(self):
        with self.assertRaises(NotFoundError) as error:
            session_usage(self.path)
        self.assertNotIn(str(self.path), str(error.exception))
        self.write([])
        with self.assertRaises(ConflictError) as error:
            session_usage(self.path, "wrong-private-session")
        self.assertNotIn("wrong-private-session", str(error.exception))
        with self.assertRaises(RuntimeContractError):
            session_usage(self.path, "PRIVATE INVALID ID")

    def test_header_and_malformed_archive_errors_do_not_leak_payloads(self):
        header = json.dumps(self.header).encode() + b"\n"
        cases = [b"", b'{"PRIVATE BROKEN PAYLOAD"\n', b'[]\n',
                 json.dumps(self.call()).encode() + b"\n", header + header,
                 header + b'{"type":"message","PRIVATE":"secret"}',
                 header + b"x" * (1024 * 1024 + 1),
                 json.dumps(dict(self.header, version=True)).encode() + b"\n",
                 json.dumps(dict(self.header, cwd="relative-private-workspace")).encode() + b"\n"]
        for data in cases:
            with self.subTest(size=len(data)):
                self.path.write_bytes(data)
                with self.assertRaises(RuntimeContractError) as error:
                    session_usage(self.path)
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertNotIn("secret", str(error.exception))

    def test_corrupt_tree_is_rejected_without_fabricated_ancestry_totals(self):
        cases = [[self.call("a", "missing", self.usage())],
                 [self.call("a", usage=self.usage()), self.call("a", "a", self.usage())],
                 [self.call("a", 42, self.usage())]]
        for entries in cases:
            with self.subTest(entries=len(entries)):
                with self.assertRaises(RuntimeContractError):
                    session_usage(self.write(entries))

    def test_symlinks_nonregular_foreign_owned_and_oversized_files_are_rejected(self):
        target = self.root / "target.jsonl"
        target.write_text(json.dumps(self.header) + "\n", encoding="utf-8")
        self.path.symlink_to(target)
        with self.assertRaises(RuntimeContractError):
            session_usage(self.path)
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(RuntimeContractError):
            session_usage(self.path)
        self.path.unlink()
        with self.assertRaises(RuntimeContractError):
            session_usage(self.root)
        self.write([])
        foreign_uid = os.getuid() + 1
        with patch("oicoach_runtime.cache_usage.os.getuid", return_value=foreign_uid):
            with self.assertRaises(RuntimeContractError):
                session_usage(self.path)
        with self.path.open("r+b") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        with self.assertRaises(RuntimeContractError):
            session_usage(self.path)


class CacheStatsCliTests(unittest.TestCase):
    def test_invalid_selector_combinations_are_rejected(self):
        for argv in (["--workflow", "synthetic"], ["--conversation", "c", "--task", "training"]):
            with self.subTest(argv=argv):
                with self.assertRaises(RuntimeContractError):
                    remote_arguments(parser().parse_args(["cache-stats", *argv]))
        for argv in ([], ["--task", "training"], ["--conversation", "c", "--workflow", "synthetic"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser().parse_args(["cache-stats", *argv])


class PhysicalOmpArchiveTests(unittest.TestCase):
    def test_padded_title_slot_precedes_header_and_is_never_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "physical.jsonl"
            title = {"type": "title", "v": 1, "title": "PRIVATE TITLE",
                     "source": "user", "updatedAt": "2026-11-10T00:00:00.000Z", "pad": ""}
            unpadded = json.dumps(title, separators=(",", ":")) + "\n"
            title["pad"] = " " * (256 - len(unpadded.encode("utf-8")))
            title_line = json.dumps(title, separators=(",", ":")) + "\n"
            self.assertEqual(len(title_line.encode("utf-8")), 256)
            header = {"type": "session", "version": 3, "id": "real-format", "cwd": "/workspace"}
            call = {"type": "message", "id": "m1", "parentId": None, "message": {
                "role": "assistant", "usage": {"input": 64, "output": 8, "cacheRead": 96, "cacheWrite": 0, "totalTokens": 168}}}
            path.write_text(title_line + json.dumps(header) + "\n" + json.dumps(call) + "\n")
            result = session_usage(path, "real-format")
            self.assertEqual(result["cumulative"]["cache_read_tokens"], 96)
            self.assertIsNone(result["cumulative"]["cache_write_tokens"])
            self.assertEqual(result["last_persisted_entry_ancestry"]["call_count"], 1)
            self.assertNotIn("PRIVATE TITLE", json.dumps(result))
if __name__ == "__main__":
    unittest.main()
