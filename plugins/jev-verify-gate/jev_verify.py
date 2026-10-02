"""Jev pre_verify control gate (stdlib only).

Accumulates per-session evidence across hooks; at pre_verify (fired only after
the agent edited files and is about to finish) asks ONE TypeSafe request with a
fixed fan-out of independent QC questions. Thresholds live in code; the result
is either None (finish) or a continue nudge. Fail-open: any error returns None.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

TYPESAFE_ENDPOINT = os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai/v1/systemone")
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")

# ---- static evaluation policy: questions + thresholds in ONE place ----
QUESTIONS = {
    "turn_requires_code": {
        "type": "noul",
        "instructions": "Does the user's task in this turn require writing or editing "
                        "code or files, rather than only answering a question?",
        "criteria": {
            "true": "The turn is a coding/file-editing task",
            "false": "The turn is a question/status/chat turn that only incidentally touched files",
        },
    },
    "requirements_met": {
        "type": "noul",
        "instructions": "Does the implementation (as described by the final response "
                        "and the changed files) fully satisfy the task requirements?",
        "criteria": {
            "true": "The change satisfies the task, with no missing requirement",
            "false": "A stated requirement is unmet, unhandled, or only partially done",
        },
    },
    "scope_creep": {
        "type": "noul",
        "instructions": "Does the implementation change behavior or files outside "
                        "the requested scope?",
        "criteria": {
            "true": "It touches things beyond what the task asked for",
            "false": "The change stays within the requested scope",
        },
    },
    "regression_risk": {
        "type": "score",
        "instructions": "Risk that these changes introduce regressions or break existing behavior.",
        "criteria": [
            "Very low risk",
            "Low risk",
            "Moderate risk",
            "High risk",
            "Very high risk",
        ],
    },
    "suspicious_change": {
        "type": "noul",
        "instructions": "Does the change contain suspicious, unjustified, or clearly wrong code?",
        "criteria": {
            "true": "There is suspicious or unjustified code in the change",
            "false": "The change looks reasonable and justified",
        },
    },
    "needs_review": {
        "type": "noul",
        "instructions": "Does this implementation require deeper review by another agent or a human?",
        "criteria": {
            "true": "It needs deeper expert review before being considered done",
            "false": "It can be accepted without deeper review",
        },
    },
    "missing": {
        "type": "choice",
        "instructions": "What is the main thing still missing or incomplete in this change?",
        "criteria": {
            "none": "Nothing is missing — the change is complete",
            "tests": "Missing or insufficient unit tests / verification",
            "core_functionality": "Missing core functionality or a stated requirement",
            "error_handling": "Missing error handling or edge cases",
            "documentation": "Missing documentation or comments",
            "other": "Something else is incomplete",
        },
    },
}

# All thresholds are 0..1 (regression_risk is normalized to 0..1 by legend length).
TURN_REQUIRES_CODE_MIN = 0.50   # (A) only gate turns that actually need code
REQUIREMENTS_MET_MIN = 0.50     # (C) was 0.80 — too eager on chat/meta turns
SCOPE_CREEP_MAX = 0.70
REGRESSION_RISK_MAX = 0.70
SUSPICIOUS_MAX = 0.70
NEEDS_REVIEW_MAX = 0.65

MAX_EVIDENCE = 20        # last N tool calls kept in state
MAX_RESULT_CHARS = 400   # per tool result
MAX_ARGS_CHARS = 400     # per tool args

_session_state: dict[str, dict] = {}


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _read_api_key() -> str | None:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    env_file = _hermes_home() / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _post(questions: dict, state) -> dict:
    api_key = _read_api_key()
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY not set")
    body = json.dumps({"state": state, "model": TYPESAFE_MODEL, "questions": questions}).encode()
    req = urllib.request.Request(
        TYPESAFE_ENDPOINT, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"TypeSafe HTTP {e.code}: {e.read().decode()[:200]}") from e


def _redact_args(args) -> str:
    """Strip obvious secrets from tool args before they enter state (best-effort)."""
    if not isinstance(args, dict):
        return json.dumps(args, ensure_ascii=False)[:MAX_ARGS_CHARS]
    out = {}
    for k, v in args.items():
        s = str(v)
        if any(t in k.lower() for t in ("key", "token", "password", "secret", "auth")):
            s = "[REDACTED]"
        out[k] = s
    return json.dumps(out, ensure_ascii=False)[:MAX_ARGS_CHARS]


def on_pre_llm_call(session_id=None, user_message=None, **kwargs):
    if not user_message:
        return
    st = _session_state.setdefault(session_id, {"task": "", "evidence": []})
    st["task"] = user_message  # latest user ask is the task under verification


def on_post_tool_call(session_id=None, tool_name=None, args=None, result=None,
                      duration_ms=None, **kwargs):
    st = _session_state.setdefault(session_id, {"task": "", "evidence": []})
    res = str(result) if result is not None else ""
    entry = {
        "tool": tool_name,
        "args": _redact_args(args or {}),
        "result": res[:MAX_RESULT_CHARS],
        "duration_ms": duration_ms,
    }
    st["evidence"] = (st["evidence"] + [entry])[-MAX_EVIDENCE:]


def on_session_end(session_id=None, **kwargs):
    _session_state.pop(session_id, None)


def _build_state(session_id, final_response, changed_paths) -> dict:
    st = _session_state.get(session_id, {"task": "", "evidence": []})
    return {
        "task": st.get("task", ""),
        "final_response": final_response or "",
        "changed_paths": list(changed_paths or []),
        "recent_tool_activity": st.get("evidence", []),
    }


def _missing_nudge(missing: str) -> str:
    """Map Jev's 'missing' choice to a specific, actionable nudge."""
    return {
        "tests": "Thiếu unit test hoặc chưa chạy xác nhận. Thêm test cho hành vi vừa đổi "
                 "và chạy xanh trước khi kết thúc.",
        "core_functionality": "Còn thiếu chức năng/yêu cầu đã nêu trong task. Làm nốt phần "
                 "chức năng còn thiếu rồi chạy test.",
        "error_handling": "Còn thiếu xử lý lỗi hoặc edge case. Bổ sung trước khi kết thúc.",
        "documentation": "Còn thiếu tài liệu/chú thích. Bổ sung trước khi kết thúc.",
        "other": "Còn thiếu sót chưa rõ. Đọc lại task, tự xác định phần còn thiếu và hoàn tất.",
        "none": "Jev thấy chưa đạt yêu cầu nhưng không chỉ ra được chỗ thiếu. Tự audit: liệt kê "
                "từng yêu cầu của task, đánh dấu đã/xong chưa, tìm yêu cầu còn thiếu, làm nốt và "
                "chạy test.",
    }.get(missing, "Đọc lại task, tìm phần còn thiếu và hoàn tất rồi chạy test.")


def decide(answers: dict) -> str | None:
    """Turn fan-out answers (all 0..1) into a continue nudge, or None to accept."""
    # (A) only gate turns that genuinely require code. A chat/meta turn that
    # incidentally edited a file (e.g. a status check mid-build) must never nudge.
    if answers.get("turn_requires_code", 0.0) < TURN_REQUIRES_CODE_MIN:
        return None
    missing = answers.get("missing", "none")
    if answers.get("requirements_met", 1.0) < REQUIREMENTS_MET_MIN:
        # (C) only nudge when Jev can name the gap. A low score with "nothing
        # missing" is a contradiction — accept rather than nag.
        if missing != "none":
            return _missing_nudge(missing)
        return None
    if answers.get("scope_creep", 0.0) > SCOPE_CREEP_MAX:
        return ("Thay đổi đang đụng tới file hoặc hành vi ngoài phạm vi task. Hoàn tác "
                "phần không liên quan và giữ diff tối thiểu.")
    if answers.get("regression_risk", 0.0) > REGRESSION_RISK_MAX:
        return ("Nguy cơ regression cao. Chạy bộ test hiện có và thêm test cho hành vi "
                "vừa đổi trước khi kết thúc.")
    if answers.get("suspicious_change", 0.0) > SUSPICIOUS_MAX:
        return "Diff chứa code đáng ngờ hoặc vô căn cứ. Rà soát và dọn lại."
    if answers.get("needs_review", 0.0) > NEEDS_REVIEW_MAX:
        return ("Thay đổi này cần rà soát kỹ hơn. Xem lại cách tiếp cận và đơn giản hoá "
                "trước khi kết thúc.")
    return None


def evaluate(session_id, final_response, changed_paths) -> str | None:
    """One Jev fan-out request over the accumulated state; returns a nudge or None."""
    if not _read_api_key():
        return None
    state = _build_state(session_id, final_response, changed_paths)
    r = _post(QUESTIONS, state)
    answers = r["answers"]
    norms = {
        "requirements_met": answers["requirements_met"]["noul"],
        "scope_creep": answers["scope_creep"]["noul"],
        "suspicious_change": answers["suspicious_change"]["noul"],
        "needs_review": answers["needs_review"]["noul"],
    }
    legend = answers["regression_risk"]["legend"]
    levels = len(legend) if isinstance(legend, dict) else 0
    score = answers["regression_risk"]["score"]
    norms["regression_risk"] = score / (levels - 1) if levels > 1 else 0.0
    norms["missing"] = answers.get("missing", {}).get("choice", "none")
    norms["turn_requires_code"] = answers.get("turn_requires_code", {}).get("noul", 0.0)
    return decide(norms)
