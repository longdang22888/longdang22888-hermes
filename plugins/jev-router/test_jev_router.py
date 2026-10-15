"""Unittest suite for the jev-router plugin (mocked network, no real API calls).

Covers: question schema, choice guards, CLI availability suppression,
truncation/redaction, request/response contract, invalid API results,
network fail-open, and hook registration against the real Hermes hook
contract (pre_llm_call payload fields -> str context).
"""
import importlib.util
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

DIR = Path(__file__).resolve().parent

# Load the package (hyphenated dir) so register() binds against the real module
# object the same way Hermes loads it; alias that module as jr everywhere below.
_spec = importlib.util.spec_from_file_location(
    "jev_router_pkg", DIR / "__init__.py",
    submodule_search_locations=[str(DIR)])
pkg = importlib.util.module_from_spec(_spec)
sys.modules["jev_router_pkg"] = pkg
_spec.loader.exec_module(pkg)
jr = pkg.jev_router

ANSWER_KEYS = {"difficulty", "needs_execution", "missing_critical_context",
               "coding_scope", "delegation_value", "independent_workstream",
               "parallel_edit_risk", "needs_deep_iteration",
               "completion_evidence", "delegate_target", "needs_task_breakdown", "kanban_intent"}

CANNED_ANSWERS = {
    "difficulty": {"type": "score", "score": 2.0, "legend": {}, "probabilities": {}},
    "needs_execution": {"type": "noul", "noul": 0.9},
    "missing_critical_context": {"type": "noul", "noul": 0.1},
    "coding_scope": {"type": "choice", "choice": "implement_or_fix", "probabilities": {}},
    "delegation_value": {"type": "noul", "noul": 0.8},
    "independent_workstream": {"type": "noul", "noul": 0.7},
    "parallel_edit_risk": {"type": "noul", "noul": 0.2},
    "needs_deep_iteration": {"type": "noul", "noul": 0.6},
    "completion_evidence": {"type": "choice", "choice": "in_progress", "probabilities": {}},
    "delegate_target": {"type": "choice", "choice": "subagent", "probabilities": {}},
}


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RouterCase(unittest.TestCase):
    """Shared sandbox: temp HERMES_HOME, fixed key, cache reset."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.home = Path(self._td.name)
        patcher = mock.patch.object(jr, "_hermes_home", lambda: self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        jr._env_secrets["stamp"] = None
        jr._env_secrets["values"] = []

    def _key_patched(self):
        p = mock.patch.object(jr, "_read_api_key", return_value="test-key-123456")
        p.start()
        self.addCleanup(p.stop)


class TestQuestionSchema(RouterCase):
    def test_exactly_twelve_questions(self):
        q = jr.build_questions(which=lambda b: None)
        self.assertEqual(set(q), ANSWER_KEYS)
        self.assertEqual(len(q), 12)

    def test_difficulty_is_score_with_five_levels_0_to_4(self):
        d = jr.build_questions(which=lambda b: None)["difficulty"]
        self.assertEqual(d["type"], "score")
        self.assertIsInstance(d["criteria"], list)
        self.assertEqual(len(d["criteria"]), 5)  # levels 0..4
        self.assertTrue(all(isinstance(level, str) and level for level in d["criteria"]))
        self.assertEqual(len(jr.DIFFICULTY_LABELS), 5)

    def test_noul_questions(self):
        q = jr.build_questions(which=lambda b: None)
        for key in ("needs_execution", "missing_critical_context", "delegation_value",
                    "independent_workstream", "parallel_edit_risk", "needs_deep_iteration"):
            self.assertEqual(q[key]["type"], "noul", key)
            self.assertEqual(set(q[key]["criteria"]), {"true", "false"}, key)

    def test_kanban_questions_and_defaults(self):
        q = jr.build_questions(which=lambda b: None)
        self.assertEqual(q["needs_task_breakdown"]["type"], "noul")
        self.assertEqual(q["kanban_intent"]["type"], "choice")
        self.assertEqual(set(q["kanban_intent"]["criteria"]), set(jr.KANBAN_INTENTS))
        for intent in jr.KANBAN_INTENTS:
            verdict = jr.parse_answers({"kanban_intent": {"choice": intent}, "needs_task_breakdown": {"noul": 0.9}})
            self.assertEqual(verdict["kanban_intent"], intent)
            self.assertIn("needs_task_breakdown=y", jr.advisory_block(verdict))
        self.assertEqual(jr.parse_answers({})["kanban_intent"], "none")
        self.assertEqual(jr.parse_answers({"kanban_intent": {"choice": "close_all"}})["kanban_intent"], "none")

    def test_coding_scope_choice_options(self):
        c = jr.build_questions(which=lambda b: None)["coding_scope"]
        self.assertEqual(c["type"], "choice")
        self.assertEqual(set(c["criteria"]),
                         {"non_coding", "inspect_or_review", "implement_or_fix", "mixed"})

    def test_completion_evidence_choice_options(self):
        c = jr.build_questions(which=lambda b: None)["completion_evidence"]
        self.assertEqual(c["type"], "choice")
        self.assertEqual(set(c["criteria"]),
                         {"not_started", "in_progress", "blocked",
                          "claimed_but_unverified", "verified_complete"})

    def test_delegate_target_choice_options(self):
        c = jr.build_questions(which=lambda b: None)["delegate_target"]
        self.assertEqual(c["type"], "choice")
        self.assertEqual(set(c["criteria"]), {"none", "subagent"})

    def test_every_question_has_instructions(self):
        for key, q in jr.build_questions(which=lambda b: None).items():
            self.assertIn(q["type"], ("noul", "choice", "score"), key)
            self.assertTrue(q.get("instructions"), key)


class TestAvailability(RouterCase):
    def test_unavailable_clis_suppressed(self):
        targets = jr.delegate_target_criteria(which=lambda b: None)
        self.assertEqual(set(targets), {"none", "subagent"})

    def test_available_clis_included(self):
        targets = jr.delegate_target_criteria(which=lambda b: "/usr/local/bin/" + b)
        self.assertEqual(set(targets), {"none", "subagent", "agy", "claude_code"})

    def test_partial_availability(self):
        targets = jr.delegate_target_criteria(
            which=lambda b: "/opt/agy" if b == "agy" else None)
        self.assertIn("agy", targets)
        self.assertNotIn("claude_code", targets)

    def test_choice_guard_unavailable_target_falls_back_to_none(self):
        # Model chose a CLI that is not (or no longer) available -> guard to none.
        answers = dict(CANNED_ANSWERS)
        answers["delegate_target"] = {"type": "choice", "choice": "agy"}
        v = jr.parse_answers(answers, available_targets=("none", "subagent"))
        self.assertEqual(v["delegate_target"], "none")


class TestTruncationRedaction(RouterCase):
    def test_bounds_are_consistent(self):
        self.assertLessEqual(jr.MSG_HEAD_CHARS + jr.MSG_TAIL_CHARS + 8, jr.MAX_MESSAGE_CHARS)

    def test_long_message_truncated(self):
        out = jr._clean_user_message("x" * 5000)
        self.assertLessEqual(len(out), jr.MAX_MESSAGE_CHARS)
        self.assertIn("[...]", out)
        self.assertTrue(out.startswith("x" * 10))
        self.assertTrue(out.endswith("x" * 10))

    def test_short_message_passthrough(self):
        self.assertEqual(jr._clean_user_message("fix the bug"), "fix the bug")

    def test_pattern_secrets_redacted(self):
        out = jr._clean_user_message("use key sk-abcdefghijklmnop123456 ok")
        self.assertNotIn("sk-abcdefghijklmnop123456", out)
        self.assertIn("[REDACTED]", out)

    def test_known_env_secret_redacted(self):
        (self.home / ".env").write_text("MY_API_KEY=supersecretvalue123\n")
        out = jr._clean_user_message("value supersecretvalue123 here")
        self.assertNotIn("supersecretvalue123", out)
        self.assertIn("[REDACTED]", out)

    def test_request_body_carries_no_secret_and_no_history(self):
        self._key_patched()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            captured["timeout"] = timeout
            return _Resp({"model": "jev-1.13.0", "answers": CANNED_ANSWERS})

        secret = "sk-abcdefghijklmnop123456"
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            v = jr.route(f"deploy with {secret} now")
        body = captured["req"].data.decode()
        self.assertNotIn(secret, body)
        self.assertNotIn("test-key-123456", body)
        self.assertIn("[REDACTED]", body)
        self.assertEqual(captured["timeout"], jr.REQUEST_TIMEOUT_S)
        payload = json.loads(body)
        self.assertEqual(set(payload["state"]), {"user_message"})
        self.assertNotIn("conversation_history", payload["state"])
        self.assertEqual(v["coding_scope"], "implement_or_fix")

    def test_metrics_log_has_no_request_text_or_key(self):
        self._key_patched()

        def fake_urlopen(req, timeout=None):
            return _Resp({"model": "jev-1.13.0", "answers": CANNED_ANSWERS})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            jr.route("find the frobnicator module please")
        log = (self.home / "logs" / "jev_metrics.jsonl").read_text()
        self.assertNotIn("frobnicator", log)
        self.assertNotIn("test-key-123456", log)
        self.assertIn('"plugin": "jev-router"', log)


class TestAnswerGuards(RouterCase):
    def test_full_answers_parse(self):
        v = jr.parse_answers(CANNED_ANSWERS, available_targets=("none", "subagent"))
        self.assertEqual(v["difficulty"], "medium")
        self.assertEqual(v["difficulty_score"], 2.0)
        self.assertEqual(v["coding_scope"], "implement_or_fix")
        self.assertEqual(v["completion_evidence"], "in_progress")
        self.assertEqual(v["delegate_target"], "subagent")
        self.assertEqual(v["needs_execution"], 0.9)

    def test_unknown_choice_values_guarded(self):
        answers = dict(CANNED_ANSWERS)
        answers["coding_scope"] = {"type": "choice", "choice": "banana"}
        answers["completion_evidence"] = {"type": "choice", "choice": "banana"}
        answers["delegate_target"] = {"type": "choice", "choice": "banana"}
        v = jr.parse_answers(answers, available_targets=jr.DELEGATE_TARGETS)
        self.assertEqual(v["coding_scope"], "non_coding")
        self.assertEqual(v["completion_evidence"], "not_started")
        self.assertEqual(v["delegate_target"], "none")

    def test_missing_keys_default_safely(self):
        v = jr.parse_answers({}, available_targets=jr.DELEGATE_TARGETS)
        self.assertEqual(v["difficulty"], "trivial")
        self.assertEqual(v["coding_scope"], "non_coding")
        self.assertEqual(v["completion_evidence"], "not_started")
        self.assertEqual(v["delegate_target"], "none")
        for key in ("needs_execution", "missing_critical_context", "delegation_value",
                    "independent_workstream", "parallel_edit_risk", "needs_deep_iteration"):
            self.assertEqual(v[key], 0.0, key)

    def test_non_numeric_values_guarded(self):
        v = jr.parse_answers({"difficulty": {"score": "high"},
                              "needs_execution": {"noul": "oops"}},
                             available_targets=jr.DELEGATE_TARGETS)
        self.assertEqual(v["difficulty"], "trivial")
        self.assertEqual(v["needs_execution"], 0.0)

    def test_score_clamped_to_0_to_4(self):
        hi = jr.parse_answers({"difficulty": {"score": 99}}, available_targets=jr.DELEGATE_TARGETS)
        lo = jr.parse_answers({"difficulty": {"score": -3}}, available_targets=jr.DELEGATE_TARGETS)
        self.assertEqual(hi["difficulty"], "very_hard")
        self.assertEqual(lo["difficulty"], "trivial")

    def test_parse_rejects_non_map(self):
        with self.assertRaises(ValueError):
            jr.parse_answers("not a map")


class TestRouteContract(RouterCase):
    def _run_route(self, response_payload, message="fix the login bug"):
        self._key_patched()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return _Resp(response_payload)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            verdict = jr.route(message)
        return verdict, captured["req"]

    def test_request_and_response_contract(self):
        verdict, req = self._run_route({"model": "jev-1.13.0", "answers": CANNED_ANSWERS})
        self.assertTrue(req.full_url.startswith("https://"))
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-key-123456")
        payload = json.loads(req.data.decode())
        self.assertEqual(payload["model"], "jev-latest")
        self.assertEqual(set(payload["questions"]), ANSWER_KEYS)
        self.assertEqual(set(payload["state"]), {"user_message"})
        self.assertIsInstance(verdict, dict)
        self.assertEqual(set(verdict), ANSWER_KEYS | {"difficulty_score"})

    def test_answers_map_is_the_source_of_truth(self):
        answers = dict(CANNED_ANSWERS)
        answers["delegate_target"] = {"type": "choice", "choice": "claude_code"}
        with mock.patch.object(jr, "delegate_target_criteria",
                               return_value={"none": "", "subagent": "",
                                             "agy": "", "claude_code": ""}):
            verdict, _ = self._run_route({"answers": answers})
        self.assertEqual(verdict["delegate_target"], "claude_code")

    def test_non_https_endpoint_rejected(self):
        self._key_patched()
        with mock.patch.object(jr, "TYPESAFE_ENDPOINT", "http://api.typesafe.ai/v1/systemone"):
            with self.assertRaises(RuntimeError):
                jr.route("hello")

    def test_empty_message_never_hits_network(self):
        self._key_patched()
        with mock.patch("urllib.request.urlopen") as up:
            self.assertIsNone(jr.route("   "))
        up.assert_not_called()


class TestInvalidApiResults(RouterCase):
    def _run(self, payload):
        self._key_patched()
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: _Resp(payload)):
            return jr.route("fix the bug")

    def test_missing_answers_map(self):
        self.assertIsNone(self._run({"model": "jev-1.13.0"}))

    def test_answers_not_a_map(self):
        self.assertIsNone(self._run({"answers": ["nope"]}))

    def test_response_not_a_dict(self):
        self.assertIsNone(self._run(None))

    def test_malformed_json_raises_and_hook_fails_open(self):
        self._key_patched()

        def fake_urlopen(req, timeout=None):
            class Bad:
                def read(self):
                    return b"not json"

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return Bad()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(Exception):
                jr.route("fix the bug")


class TestNetworkFailOpen(RouterCase):
    def setUp(self):
        super().setUp()
        self.ctx = mock.Mock()
        pkg.register(self.ctx)
        self.hook = self.ctx.register_hook.call_args[0][1]

    def test_urlerror_hook_returns_empty(self):
        self._key_patched()
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("dns down")):
            self.assertEqual(self.hook(user_message="fix the bug"), "")

    def test_timeout_hook_returns_empty(self):
        self._key_patched()
        with mock.patch("urllib.request.urlopen",
                        side_effect=socket.timeout("slow")):
            self.assertEqual(self.hook(user_message="fix the bug"), "")

    def test_http_error_hook_returns_empty(self):
        self._key_patched()
        err = urllib.error.HTTPError("https://api.typesafe.ai/v1/systemone", 529,
                                     "overloaded", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(self.hook(user_message="fix the bug"), "")

    def test_no_api_key_hook_returns_empty_without_network(self):
        with mock.patch.object(jr, "_read_api_key", return_value=None):
            with mock.patch("urllib.request.urlopen") as up:
                self.assertEqual(self.hook(user_message="fix the bug"), "")
        up.assert_not_called()


class TestHookRegistration(RouterCase):
    def setUp(self):
        super().setUp()
        self.ctx = mock.Mock()
        pkg.register(self.ctx)

    def test_registers_only_pre_llm_call(self):
        names = [c[0][0] for c in self.ctx.register_hook.call_args_list]
        self.assertEqual(names, ["pre_llm_call"])
        self.assertTrue(callable(self.ctx.register_hook.call_args[0][1]))

    def test_hook_accepts_real_pre_llm_call_payload(self):
        # Field names mirror agent/turn_context._collect_pre_llm_call_context().
        hook = self.ctx.register_hook.call_args[0][1]
        self._key_patched()
        with mock.patch.object(jr, "_post",
                               return_value={"answers": CANNED_ANSWERS}):
            out = hook(session_id="s", task_id="t", turn_id="u",
                       user_message="fix the login bug",
                       conversation_history=[{"role": "user"}],
                       is_first_turn=True, model="gpt-5", platform="cli",
                       parent_session_id="", sender_id="me")
        self.assertIsInstance(out, str)
        self.assertIn("advisory only", out)

    def test_hook_injects_str_advisory_block(self):
        hook = self.ctx.register_hook.call_args[0][1]
        self._key_patched()
        with mock.patch.object(jr, "_post",
                               return_value={"answers": CANNED_ANSWERS}):
            out = hook(user_message="fix the bug")
        self.assertIsInstance(out, str)
        self.assertIn(jr.HEADER, out)
        self.assertIn("difficulty=medium", out)
        self.assertIn("scope=implement_or_fix", out)
        self.assertIn("target=subagent", out)
        self.assertIn("completion_evidence=in_progress", out)
        self.assertIn(jr.NO_RECURSIVE, out)  # target != none

    def test_hook_no_recursion_note_omitted_for_none(self):
        hook = self.ctx.register_hook.call_args[0][1]
        answers = dict(CANNED_ANSWERS)
        answers["delegate_target"] = {"type": "choice", "choice": "none"}
        self._key_patched()
        with mock.patch.object(jr, "_post", return_value={"answers": answers}):
            out = hook(user_message="fix the bug")
        self.assertNotIn(jr.NO_RECURSIVE, out)
        self.assertIn("target=none", out)

    def test_hook_header_preserves_user_directive(self):
        self.assertIn("user's explicit directive", jr.HEADER)
        self.assertIn("approvals", jr.HEADER)

    def test_hook_empty_on_invalid_result(self):
        hook = self.ctx.register_hook.call_args[0][1]
        self._key_patched()
        with mock.patch.object(jr, "_post", return_value={"answers": "bad"}):
            self.assertEqual(hook(user_message="fix the bug"), "")

    def test_hook_empty_on_internal_exception(self):
        hook = self.ctx.register_hook.call_args[0][1]
        self._key_patched()
        with mock.patch.object(jr, "_post", side_effect=RuntimeError("boom")):
            self.assertEqual(hook(user_message="fix the bug"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
