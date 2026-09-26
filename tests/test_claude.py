"""`claude --output-format json` returns an array of messages, not an object.

Reading it as an object raised, the raw transcript was returned as if it were the
model's answer, and every posting ended up scored 0. These lock that shape in.
"""
import json
import unittest
from unittest import mock

import helpers  # noqa: F401 - installs the suite settings guard

from cairn import claude, paths

# Trimmed from a real `claude -p ... --output-format json --tools ""` run: same
# element order and the same keys we depend on.
TRANSCRIPT = json.dumps([
    {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6", "tools": []},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": '[{"id":"a","fit":72,"reason":"test"}]'}]}},
    {"type": "rate_limit_event", "rate_limit_info": {}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
     "result": '[{"id":"a","fit":72,"reason":"test"}]', "total_cost_usd": 0.0428},
])


class ClaudeResultTest(unittest.TestCase):
    def test_answer_comes_from_the_result_message(self):
        text, err = claude.result(TRANSCRIPT)
        self.assertIsNone(err)
        self.assertEqual(json.loads(text), [{"id": "a", "fit": 72, "reason": "test"}])

    def test_legacy_single_object_shape_still_works(self):
        payload = json.dumps({"type": "result", "subtype": "success", "result": "hi"})
        self.assertEqual(claude.result(payload), ("hi", None))

    def test_unrecognised_shape_is_an_error_not_the_raw_transcript(self):
        payload = json.dumps([{"type": "assistant", "message": {}}])
        text, err = claude.result(payload)
        self.assertIsNone(text)
        self.assertIn("couldn't read", err)

    def test_non_json_output_is_an_error(self):
        text, err = claude.result("claude: command failed")
        self.assertIsNone(text)
        self.assertIn("couldn't read", err)

    def test_empty_result_is_an_error(self):
        payload = json.dumps([{"type": "result", "subtype": "error_max_turns", "result": ""}])
        text, err = claude.result(payload)
        self.assertIsNone(text)
        self.assertIn("empty answer", err)

    def test_a_result_flagged_as_an_error_is_never_the_answer(self):
        payload = json.dumps([{"type": "result", "subtype": "success", "is_error": True,
                               "result": "You've hit your session limit · resets 10:10pm"}])
        text, err = claude.result(payload)
        self.assertIsNone(text)
        self.assertIn("session limit", err)


class ClaudeInvocationTest(unittest.TestCase):
    def test_every_call_disables_built_in_tools(self):
        captured = {}

        class Result:
            returncode, stdout, stderr = 0, TRANSCRIPT, ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            return Result()

        real_run = claude.subprocess.run
        claude.subprocess.run = fake_run
        try:
            text, err = claude.run("hello")
        finally:
            claude.subprocess.run = real_run
        self.assertIsNone(err)
        self.assertTrue(text)
        cmd = captured["cmd"]
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")

    def test_the_prompt_goes_on_stdin_never_the_command_line(self):
        captured = {}

        class Result:
            returncode, stdout, stderr = 0, TRANSCRIPT, ""

        def fake_run(cmd, **kwargs):
            captured["cmd"], captured["input"] = cmd, kwargs.get("input")
            return Result()

        prompt = "x" * 2_000_000
        with mock.patch.object(claude.subprocess, "run", fake_run):
            self.assertIsNone(claude.run(prompt)[1])
        self.assertEqual(captured["input"], prompt)
        self.assertNotIn(prompt, captured["cmd"])

    def _command(self, **run_kwargs):
        """(cmd, kwargs) claude.run passes to subprocess.run."""
        done = mock.Mock(returncode=0, stdout=TRANSCRIPT, stderr="")
        with mock.patch.object(claude.subprocess, "run", return_value=done) as run:
            self.assertIsNone(claude.run("hello", **run_kwargs)[1])
        return run.call_args.args[0], run.call_args.kwargs

    def test_every_call_leaves_out_claude_codes_own_context(self):
        cmd, kwargs = self._command()
        for flag in ("--strict-mcp-config", "--disable-slash-commands",
                     "--no-session-persistence"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "Answer exactly as asked.")
        self.assertNotIn("--bare", cmd)
        self.assertEqual(kwargs["cwd"], paths.home())

    def test_summaries_run_without_thinking_and_ranking_with_it(self):
        _, cheap = self._command(model="haiku", tier="cheap")
        _, strong = self._command(tier="strong")
        self.assertEqual(cheap["env"]["MAX_THINKING_TOKENS"], "0")
        self.assertIsNone(strong["env"])

    def test_the_command_runs_from_where_path_lookup_finds_it(self):
        with mock.patch.object(claude.shutil, "which", return_value=r"C:\npm\claude.CMD"):
            self.assertEqual(self._command()[0][0], r"C:\npm\claude.CMD")
        with mock.patch.object(claude.shutil, "which", return_value=None):
            self.assertEqual(self._command()[0][0], "claude")

    def test_an_os_error_is_returned_as_an_error(self):
        with mock.patch.object(claude.subprocess, "run",
                               side_effect=OSError(7, "Argument list too long")):
            text, err = claude.run("hello")
        self.assertIsNone(text)
        self.assertIn("Argument list too long", err)


if __name__ == "__main__":
    unittest.main()
