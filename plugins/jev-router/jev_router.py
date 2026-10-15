"""Jev turn-router: one TypeSafe fan-out per turn -> a bounded advisory routing hint.

Stdlib only. Exactly ONE request to the TypeSafe HTTPS endpoint
(``/v1/systemone``, model ``jev-latest``) carrying 12 independent questions over a
bounded, redacted ``user_message`` state. The conversation history and any source
code are NEVER sent. Jev only answers; every threshold and routing policy lives
here in code.

The verdict is advisory context injected via ``pre_llm_call``: it never overrides
the user's directive or the approval flow, and it never triggers recursive
delegation. Fail-open by design: missing key, network error, timeout, non-HTTPS
endpoint, or invalid API result -> no hint and the turn runs unchanged.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TYPESAFE_ENDPOINT = os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai/v1/systemone")
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")  # alias; see docs.typesafe.ai/models

# Limits (all enforced in code).
REQUEST_TIMEOUT_S = 6        # advisory hint: fail fast, never stall the turn
MAX_MESSAGE_CHARS = 1200     # hard bound on the sanitized user_message sent as state
MSG_HEAD_CHARS = 400         # truncation keeps the opening ask...
MSG_TAIL_CHARS = 700         # ...and the tail, where the real ask usually sits

# Difficulty is a Score over 5 concrete 0..4 levels.
DIFFICULTY_CRITERIA = [
    "Trivial — a one-line answer or a single lookup, nothing to break",
    "Easy — one self-contained step, obvious how to do it",
    "Medium — a few steps or files, some care needed to get right",
    "Hard — multi-step, cross-cutting, or needs specialist knowledge",
    "Very hard — large, ambiguous, or high-risk work",
]
DIFFICULTY_LABELS = ["trivial", "easy", "medium", "hard", "very_hard"]

# Choice option sets (guards fall back to the first / safest option).
CODING_SCOPES = ("non_coding", "inspect_or_review", "implement_or_fix", "mixed")
COMPLETION_EVIDENCE = ("not_started", "in_progress", "blocked",
                       "claimed_but_unverified", "verified_complete")
DELEGATE_TARGETS = ("none", "subagent", "agy", "claude_code")

# External delegate CLIs are offered only when installed (shutil.which).
_DELEGATE_CLI = {"agy": "agy", "claude_code": "claude"}
_DELEGATE_TARGET_DESC = {
    "none": "Do it inline; no delegation",
    "subagent": "Hermes subagent via delegate_task",
    "agy": "Antigravity CLI (agy)",
    "claude_code": "Claude Code CLI (claude)",
}

KANBAN_INTENTS = ("none", "track_new_work", "maintain_existing_work")

NOUL_CRITERIA = {
    "needs_task_breakdown": ("Does this request need decomposition into separately trackable, independently verifiable tasks?",
                             {"true": "Multiple distinct deliverables need tracking",
                              "false": "One small task or an answer is enough"}),
    "needs_execution": ("Does completing this request require actually executing "
                        "actions — running commands, editing files, calling tools — "
                        "rather than only answering?",
                        {"true": "Actions must be executed to satisfy the request",
                         "false": "A text answer alone satisfies the request"}),
    "missing_critical_context": ("Is critical context missing from the request — "
                                 "information without which the task cannot be done "
                                 "correctly (required files, credentials, constraints, "
                                 "environment details)?",
                                 {"true": "Critical context is missing",
                                  "false": "The request is self-contained enough to start"}),
    "delegation_value": ("Would delegating part of this work to a subagent add value "
                         "(parallelism, isolation, or offloading) over doing it inline?",
                         {"true": "Delegation would add value",
                          "false": "Inline work is better or equal"}),
    "independent_workstream": ("Does this request decompose into independent "
                               "workstreams that can proceed without sharing edits or "
                               "intermediate state?",
                               {"true": "Independent subtasks exist",
                                "false": "The work is one coupled stream"}),
    "parallel_edit_risk": ("Would running subtasks in parallel risk conflicting edits "
                           "to the same files or overlapping work?",
                           {"true": "Parallel edits would likely conflict",
                            "false": "Parallel edits are safe, or there are none"}),
    "needs_deep_iteration": ("Does this request need deep iteration — repeated "
                             "build/test/fix cycles or extended research before a "
                             "correct result?",
                             {"true": "Iterative cycles are needed",
                              "false": "One pass should be enough"}),
}


def delegate_target_criteria(which=None) -> dict:
    """The delegate_target options offered to Jev — unavailable CLIs suppressed.

    ``which`` is injectable for tests (defaults to ``shutil.which``).
    """
    which = which or shutil.which
    criteria = {"none": _DELEGATE_TARGET_DESC["none"],
                "subagent": _DELEGATE_TARGET_DESC["subagent"]}
    for name, binary in _DELEGATE_CLI.items():
        if which(binary):
            criteria[name] = _DELEGATE_TARGET_DESC[name]
    return criteria


def build_questions(which=None) -> dict:
    """The 12 independent routing questions — one shared state, one request."""
    questions = {
        "difficulty": {
            "type": "score",
            "instructions": "How difficult is the user's request to complete "
                            "correctly? Judge the current request only.",
            "criteria": DIFFICULTY_CRITERIA,
        },
    }
    for qid, (instructions, criteria) in NOUL_CRITERIA.items():
        questions[qid] = {"type": "noul", "instructions": instructions,
                          "criteria": criteria}
    questions["coding_scope"] = {
        "type": "choice",
        "instructions": "What coding scope does this request cover?",
        "criteria": {
            "non_coding": "No code or files involved",
            "inspect_or_review": "Read, inspect, review, or explain existing code "
                                 "without changing it",
            "implement_or_fix": "Create, edit, or fix code or files",
            "mixed": "Both reading/reviewing and implementing or fixing",
        },
    }
    questions["completion_evidence"] = {
        "type": "choice",
        "instructions": "Judging only the request text (no conversation history is "
                        "provided), what does it imply about the current state of "
                        "the work it describes?",
        "criteria": {
            "not_started": "The described work has not begun",
            "in_progress": "The work is underway but unfinished",
            "blocked": "The work is stuck on something",
            "claimed_but_unverified": "Completion is claimed but no verifiable "
                                      "evidence is given",
            "verified_complete": "Completion is claimed with verifiable evidence "
                                 "(tests, output, concrete results)",
        },
    }
    questions["delegate_target"] = {
        "type": "choice",
        "instructions": "If this work were delegated at all, which target fits best? "
                        "Choose 'none' when it should be done inline.",
        "criteria": delegate_target_criteria(which),
    }
    questions["kanban_intent"] = {
        "type": "choice",
        "instructions": "What task-tracking intent is expressed? Judge intent only, not whether tasks already exist. This answer does not limit a workflow that needs both new and existing tasks.",
        "criteria": {
            "none": "No task tracking is warranted",
            "track_new_work": "Track newly requested work, breaking it down first if needed",
            "maintain_existing_work": "Update progress, blockers, or completion of existing work",
        },
    }
    return questions


# --- Key + secret handling (safe line parsing; never any shell; never logged) ----

def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _read_env_file(path: Path) -> dict:
    """Parse KEY=value lines only — no shell, no eval. Missing file -> {}."""
    values: dict = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            key = k.strip().removeprefix("export ").strip()
            values[key] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _env_file_paths() -> tuple[Path, Path]:
    return (_hermes_home() / ".env", Path.home() / ".config" / "typesafe" / "env")


def _read_api_key() -> str | None:
    """TYPESAFE_API_KEY from env, active HERMES_HOME/.env, then ~/.config/typesafe/env.

    Falling back to these runtime sources is authorized for the current task; the
    key itself is used only in the Authorization header and never logged or echoed.
    """
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    for path in _env_file_paths():
        key = (_read_env_file(path).get("TYPESAFE_API_KEY") or "").strip()
        if key:
            return key
    return None


_SECRET_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_\-]{16,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]{16,}"
    r"|(?i:[A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)\s*[=:]\s*[^\s'\"]{6,})")
_env_secrets = {"stamp": None, "values": []}


def _known_secrets() -> list:
    """Secret-looking values from the env files, so known credentials never leave."""
    try:
        stamp = tuple((p, p.stat().st_mtime) for p in _env_file_paths() if p.exists())
        if _env_secrets["stamp"] != stamp:
            vals = set()
            for p in _env_file_paths():
                for k, v in _read_env_file(p).items():
                    if re.search(r"KEY|TOKEN|SECRET|PASSWORD|PASS", k.upper()) and len(v) >= 12:
                        vals.add(v)
            _env_secrets.update(stamp=stamp, values=sorted(vals, key=len, reverse=True))
        return _env_secrets["values"]
    except Exception:
        return []


def _redact_text(text) -> str:
    s = text if isinstance(text, str) else str(text)
    for v in _known_secrets():
        s = s.replace(v, "[REDACTED]")
    return _SECRET_RE.sub("[REDACTED]", s)


def _clean_user_message(user_message) -> str:
    """Redact credentials, then bound to MAX_MESSAGE_CHARS (head + tail kept).

    This is the ONLY content that leaves the machine: never conversation_history,
    never tool output, never source files.
    """
    s = _redact_text(user_message or "").strip()
    if len(s) > MAX_MESSAGE_CHARS:
        s = s[:MSG_HEAD_CHARS] + "\n[...]\n" + s[-MSG_TAIL_CHARS:]
    return s


# --- Metrics (metadata + results only: never request text, never the key) --------

PLUGIN = "jev-router"
METRICS_MAX_BYTES = 2 * 1024 * 1024


def _metric(event: str, **fields) -> None:
    """Best-effort counter: one JSONL line in logs/jev_metrics.jsonl. Never raises."""
    try:
        path = _hermes_home() / "logs" / "jev_metrics.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > METRICS_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        line = {"ts": datetime.now(timezone.utc).isoformat(), "plugin": PLUGIN,
                "event": event, **fields}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --- The single TypeSafe request -------------------------------------------------

def _post(questions: dict, state: dict) -> dict:
    """POST one fan-out to /v1/systemone. Raises on any transport/API error."""
    if urllib.parse.urlparse(TYPESAFE_ENDPOINT).scheme != "https":
        raise RuntimeError("TypeSafe endpoint must be https")
    api_key = _read_api_key()
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY not set")
    before = json.dumps(state, ensure_ascii=False, default=str)
    clean = _redact_text(before)
    if clean != before:
        _metric("redaction", n_questions=len(questions))
    body = json.dumps({"state": json.loads(clean), "model": TYPESAFE_MODEL,
                       "questions": questions}).encode()
    req = urllib.request.Request(
        TYPESAFE_ENDPOINT, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        _metric("request", ok=False, error=f"HTTP {e.code}", n_questions=len(questions),
                latency_ms=int((time.monotonic() - t0) * 1000))
        raise RuntimeError(f"TypeSafe HTTP {e.code}") from e
    except Exception as exc:
        _metric("request", ok=False, error=type(exc).__name__, n_questions=len(questions),
                latency_ms=int((time.monotonic() - t0) * 1000))
        raise
    _metric("request", ok=True, n_questions=len(questions),
            latency_ms=int((time.monotonic() - t0) * 1000))
    return result


# --- Answer parsing (choice guards: unknown values fall back to safe defaults) ----

def _noul(answers: dict, key: str) -> float:
    try:
        return max(0.0, min(float(answers.get(key, {}).get("noul", 0.0)), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _choice(answers: dict, key: str, allowed, default: str) -> str:
    value = answers.get(key, {}).get("choice", default)
    return value if value in allowed else default


def parse_answers(answers: dict, available_targets=None) -> dict:
    """Fold the answers map into a typed verdict; every field guarded to a default."""
    if not isinstance(answers, dict):
        raise ValueError("answers must be a map")
    allowed_targets = tuple(available_targets if available_targets is not None
                            else delegate_target_criteria())
    raw_score = answers.get("difficulty", {}).get("score", 0.0)
    try:
        score = max(0.0, min(float(raw_score), float(len(DIFFICULTY_CRITERIA) - 1)))
    except (TypeError, ValueError):
        score = 0.0
    target = _choice(answers, "delegate_target", allowed_targets, "none")
    return {
        "difficulty": DIFFICULTY_LABELS[round(score)],
        "difficulty_score": round(score, 3),
        "needs_task_breakdown": _noul(answers, "needs_task_breakdown"),
        "kanban_intent": _choice(answers, "kanban_intent", KANBAN_INTENTS, "none"),
        "needs_execution": _noul(answers, "needs_execution"),
        "missing_critical_context": _noul(answers, "missing_critical_context"),
        "coding_scope": _choice(answers, "coding_scope", CODING_SCOPES, "non_coding"),
        "delegation_value": _noul(answers, "delegation_value"),
        "independent_workstream": _noul(answers, "independent_workstream"),
        "parallel_edit_risk": _noul(answers, "parallel_edit_risk"),
        "needs_deep_iteration": _noul(answers, "needs_deep_iteration"),
        "completion_evidence": _choice(answers, "completion_evidence",
                                       COMPLETION_EVIDENCE, "not_started"),
        "delegate_target": target,
    }


def route(user_message, *, which=None) -> dict | None:
    """One request -> verdict dict, or None when there is nothing to route.

    Raises only on transport/API errors (the hook logs and fails open); an invalid
    API result shape returns None.
    """
    msg = _clean_user_message(user_message)
    if not msg:
        _metric("skipped", reason="empty_message")
        return None
    questions = build_questions(which=which)
    resp = _post(questions, {"user_message": msg})
    answers = resp.get("answers") if isinstance(resp, dict) else None
    if not isinstance(answers, dict):
        _metric("invalid_response", got=type(resp).__name__)
        return None
    available = tuple(delegate_target_criteria(which))
    verdict = parse_answers(answers, available_targets=available)
    _metric("route", **verdict)
    return verdict


# --- The advisory hint (routing advisory ONLY) -----------------------------------

HEADER = ("[Jev routing — advisory only: the user's explicit directive and any "
          "required approvals always take precedence over this hint]")
NO_RECURSIVE = ("Delegation is one level only: a delegate must never delegate "
                "further.")


def advisory_block(verdict: dict) -> str:
    """Render the verdict as one compact advisory block."""
    yn = lambda p: "y" if p >= 0.5 else "n"  # noqa: E731
    lines = [
        HEADER,
        (f"difficulty={verdict['difficulty']}; scope={verdict['coding_scope']}; "
         f"execute={yn(verdict['needs_execution'])}; "
         f"missing_context={yn(verdict['missing_critical_context'])}; "
         f"deep_iteration={yn(verdict['needs_deep_iteration'])}"),
        (f"delegation: value={yn(verdict['delegation_value'])}; "
         f"independent={yn(verdict['independent_workstream'])}; "
         f"parallel_edit_risk={yn(verdict['parallel_edit_risk'])}; "
         f"target={verdict['delegate_target']}"),
        f"completion_evidence={verdict['completion_evidence']}",
        f"kanban_intent={verdict['kanban_intent']}; needs_task_breakdown={yn(verdict['needs_task_breakdown'])}",
        "Before Kanban changes: inspect related tasks, break down if needed, deduplicate, then create/update. Verify acceptance before closing. This hint never authorizes changes itself.",
    ]
    if verdict["delegate_target"] != "none":
        lines.append(NO_RECURSIVE)
    return "\n".join(lines)
