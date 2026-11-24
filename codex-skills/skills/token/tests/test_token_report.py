import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "token_report.py"
sys.path.insert(0, str(SCRIPT.parent))
import token_report as report


def event(kind, **data):
    return {"type": "event_msg", "payload": {"type": kind, **data}}


def row(kind, **data):
    return {"type": kind, "payload": data}


def numbers(i, c, o, r=0):
    return dict(zip(report.FIELDS, (i, c, o, r, i + o)))


class AccountingTests(unittest.TestCase):
    def parse(self, *rows):
        result = report.Report()
        for item in rows:
            result.digest(item)
        return result

    def test_native_request_turn_thread_and_mirror(self):
        one, two, total = numbers(100, 60, 20, 5), numbers(120, 75, 30, 8), numbers(220, 135, 50, 13)
        first = row("token_usage_record", response_id="one", turn_id="a",
                    usage=one, turn_token_usage=one, thread_token_usage=one)
        second = row("token_usage_record", response_id="two", turn_id="a",
                     usage=two, turn_token_usage=total, thread_token_usage=total)
        result = self.parse(event("task_started", turn_id="a", model_context_window=1000),
                            row("turn_context", turn_id="a", model="gpt-test"),
                            first, event("token_count", info={"total_token_usage": one, "last_token_usage": one}),
                            second, second, event("token_count", info={"total_token_usage": total, "last_token_usage": two}),
                            event("task_complete", turn_id="a"))
        self.assertEqual(result.latest, two)
        self.assertEqual(len(result.requests), 2)
        self.assertEqual(result.session_total(), total)
        self.assertEqual(result.turns["a"].totals(), total)
        self.assertEqual(result.capacity, 1000)
        output = report.render(result, report.Candidate(Path("unused")), "test")
        self.assertIn("Estimated utilization: 12.0%", output)
        self.assertIn("cached 75 | uncached 45 | output 30 | reasoning 8", output)

    def test_legacy_deltas_and_latest_from_multiple_requests(self):
        first, last, total = numbers(50, 20, 10), numbers(75, 25, 15), numbers(125, 45, 25)
        counter = lambda cumulative, latest: event("token_count", info={
            "total_token_usage": cumulative, "last_token_usage": latest})
        result = self.parse(event("task_started", turn_id="a"), counter(first, first),
                            event("task_complete", turn_id="a"),
                            event("task_started", turn_id="b"), counter(first, first),
                            counter(total, last), event("task_complete", turn_id="b"))
        self.assertEqual(result.latest, last)
        self.assertEqual(result.session_total(), total)
        self.assertEqual(result.turns["a"].totals(), first)
        self.assertEqual(result.turns["b"].totals(), last)

    def test_direct_turn_aggregates_and_incomplete_metrics(self):
        result = self.parse(
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 10}},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        )
        self.assertEqual(result.session_total()[report.TOTAL], 75)
        self.assertIsNone(result.session_total()[report.CACHE])
        self.assertEqual(result.latest, {})
        self.assertEqual(len(result.completed), 2)

    def test_compaction_replaces_history_and_deduplicates_notification(self):
        result = self.parse(event("task_started", turn_id="a"),
                            row("response_item", type="message", role="user", content="X" * 1000),
                            row("token_usage_record", usage=numbers(500, 0, 1)),
                            row("compacted", message="fallback", replacement_history=[
                                {"type": "compaction", "content": "short summary"},
                                {"type": "message", "role": "user", "content": "retained"},
                            ]),
                            event("context_compacted"))
        self.assertTrue(result.snapshot_invalid)
        self.assertEqual(result.compactions, 1)
        counts, _ = result.context.summarize("a")
        self.assertEqual(counts["Compaction summaries"], 4)
        self.assertEqual(counts["Conversation history"], 2)
        self.assertEqual(counts["User prompts"], 0)
        result.digest(row("token_usage_record", usage=numbers(20, 0, 1)))
        self.assertFalse(result.snapshot_invalid)
        result.digest(row("compacted", message="partial"))
        self.assertFalse(result.context.available)
        self.assertEqual(result.compactions, 2)

    def test_categories_are_exclusive_and_output_is_redacted(self):
        result = self.parse(event("task_started", turn_id="a"),
                            row("response_item", type="message", role="user", content=[
                                {"text": "# AGENTS.md instructions for synthetic\nSECRET_MARKER"},
                                {"text": "<skill>SECRET_MARKER</skill>"},
                                {"text": "SECRET_MARKER"},
                            ]),
                            row("response_item", type="function_call", name="exec_command",
                                arguments='{"cmd":"Get-Content example.py"}', call_id="1"),
                            row("response_item", type="function_call_output", output="SECRET_MARKER", call_id="1"),
                            row("response_item", type="function_call", name="mcp__example", call_id="2"),
                            row("response_item", type="function_call_output", output="SECRET_MARKER", call_id="2"),
                            row("response_item", type="function_call", name="exec_command", arguments="pwd", call_id="3"),
                            row("response_item", type="function_call_output", output="SECRET_MARKER", call_id="3"))
        counts, _ = result.context.summarize("a")
        for category in ("User prompts", "Source code and files read", "MCP tool results", "Terminal command output"):
            self.assertEqual(counts[category], 4)
        self.assertGreater(counts["AGENTS.md"], 0)
        self.assertGreater(counts["Skill instructions"], 0)
        self.assertEqual(counts["Tool results"], 0)
        self.assertNotIn("SECRET_MARKER", report.render(result, report.Candidate(Path("unused")), "test"))

    def test_base_system_instructions_deduplicate_only_matching_text(self):
        initial = "A" * 100
        result = self.parse(row("session_meta", base_instructions={"text": initial}),
                            row("response_item", type="message", role="system", content="additional"))
        self.assertEqual(result.context.summarize(None)[0]["System instructions"], 28)
        result.digest(row("response_item", type="message", role="system", content=initial))
        self.assertEqual(result.context.summarize(None)[0]["System instructions"], 28)

    def test_malformed_field_types_and_counter_reset(self):
        result = self.parse({"type": []}, {"type": "event_msg", "payload": {"type": {}}},
                            row("response_item", type=[], role={}),
                            row("token_usage_record", usage={"input_tokens": True, "output_tokens": "4"}))
        self.assertEqual(result.latest, {})
        self.assertEqual(result.context.summarize(None)[0], {})
        self.assertIsNone(report.subtract(numbers(2, 0, 0), numbers(3, 0, 0))[report.INPUT])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.cwd = self.home / "project"
        self.sessions = self.home / "sessions"
        self.sessions.mkdir()

    def rollout(self, cwd, modified, active=False, name=None):
        identity = str(uuid4())
        path = self.sessions / (name or f"rollout-{identity}.jsonl")
        rows = [row("session_meta", id=identity, cwd=str(cwd)), event("task_started", turn_id="a")]
        if not active:
            rows.append(event("task_complete", turn_id="a"))
        path.write_text('malformed JSON\n[]\n' + '\n'.join(map(json.dumps, rows)), encoding="utf-8")
        os.utime(path, (modified, modified))
        return path, identity

    def test_cwd_selection_ignores_newer_unrelated_session(self):
        self.rollout(self.cwd, 10)
        path, _ = self.rollout(self.cwd, 20)
        self.rollout(self.home / "other", 30)
        self.assertEqual(report.select(self.home, self.cwd, env={})[0].path, path)

    def test_concurrency_and_explicit_selection(self):
        path, identity = self.rollout(self.cwd, 10, active=True)
        self.rollout(self.cwd, 20, active=True)
        with self.assertRaises(report.SelectionError):
            report.select(self.home, self.cwd, env={})
        self.assertEqual(report.select(self.home, self.cwd, env={"CODEX_THREAD_ID": identity})[0].path, path)
        with self.assertRaises(report.SelectionError):
            report.select(self.home, self.cwd, str(uuid4()), env={})

    def test_cli_codex_home_and_metadata_id_fallback(self):
        _, identity = self.rollout(self.cwd, 10, name="renamed.jsonl")
        output = subprocess.run([sys.executable, str(SCRIPT), "--session-id", identity],
                                env={**os.environ, "CODEX_HOME": str(self.home)},
                                capture_output=True, text=True, check=True)
        self.assertIn(identity, output.stdout)
        self.assertIn("Recorded session total: N/A", output.stdout)


if __name__ == "__main__":
    unittest.main()

