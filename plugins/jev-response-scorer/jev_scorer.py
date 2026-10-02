"""Jev response scorer (stdlib only).

One TypeSafe request per finished turn asks two questions:
  satisfied_intent — has the agent satisfied the user's intent?
  needs_research   — does the answer need further research (tavily/exa/parallel)?

The decision (stop / continue-research / continue-other) is derived in code from
the rule: if the intent is satisfied, stop and do not research further. Results
are appended to a JSONL ledger as RL labels for later training/analysis.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TYPESAFE_ENDPOINT = os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai/v1/systemone")
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")

# Decision thresholds (Jev answers, code decides).
SATISFIED_MIN = 0.50     # satisfied_intent >= this → satisfied
NEEDS_RESEARCH_MIN = 0.50

# Steering: the criteria are also injected into the system prompt (once per
# session, cache-safe) so the agent knows how it is scored. Keep the wording
# here beside the questions so both describe the same thing and stay in sync.
CRITERIA_SECTION_ID = "jev-response-scorer.criteria"


def criteria_text(session_info=None) -> str:
    return (
        "Your response is judged on two things each turn:\\n"
        "1. Did you satisfy the user's intent? — answer what they asked, or make concrete progress.\\n"
        "2. Is more research needed? — if you have already answered fully, do NOT keep researching; "
        "stop. Only research further (tavily/exa/parallel) when the answer is genuinely incomplete."
    )

# Ledger rotation: keep the active file small, roll it aside, and prune old ones.
LEDGER_MAX_BYTES = 5 * 1024 * 1024      # rotate when the active ledger exceeds 5 MB
LEDGER_KEEP_ROTATED = 3                 # keep the 3 most recent rotated files


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


def _questions() -> dict:
    return {
        "satisfied_intent": {
            "type": "noul",
            "instructions": "Has the response satisfied the user's intent — answered "
                            "what they asked, or made concrete progress on it?",
            "criteria": {
                "true": "The intent is satisfied; nothing essential is missing",
                "false": "The response is incomplete, off-target, or deflecting",
            },
        },
        "needs_research": {
            "type": "noul",
            "instructions": "Does the response need further research (tavily/exa/parallel "
                            "search or retrieval) to be complete?",
            "criteria": {
                "true": "More research is required to answer correctly",
                "false": "The answer is already grounded and complete",
            },
        },
    }


def decide(satisfied: float, needs_research: float) -> str:
    """Derive the stop/continue decision from the two answers (code owns policy)."""
    if satisfied >= SATISFIED_MIN:
        # If intent is satisfied, stop and do not research further.
        return "stop"
    if needs_research >= NEEDS_RESEARCH_MIN:
        return "continue_research"
    return "continue_other"


def score_response(user_message: str, assistant_response: str, history="") -> dict:
    state = {"user_message": user_message, "assistant_response": assistant_response}
    if history:
        text = history if isinstance(history, str) else json.dumps(history, ensure_ascii=False)
        state["conversation_history"] = text[-8000:]  # tail only; keep the request bounded
    r = _post(_questions(), state)
    a = r["answers"]
    satisfied = a["satisfied_intent"]["noul"]
    needs_research = a["needs_research"]["noul"]
    decision = decide(satisfied, needs_research)
    return {
        "satisfied_intent": round(satisfied, 3),
        "needs_research": round(needs_research, 3),
        "decision": decision,
        "model": r.get("model"),
    }


def _append_ledger(entry: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _rotate_if_needed(path: Path) -> None:
    """Roll the ledger aside when it exceeds LEDGER_MAX_BYTES, pruning old files."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < LEDGER_MAX_BYTES:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    rotated = path.with_name(f"{path.stem}.{stamp}.jsonl")
    path.rename(rotated)
    # prune: keep only the most recent LEDGER_KEEP_ROTATED rotated files
    siblings = sorted(path.parent.glob(f"{path.stem}.*.jsonl"))
    for old in siblings[:-LEDGER_KEEP_ROTATED]:
        try:
            old.unlink()
        except OSError:
            pass


def record_turn(session_id, user_message, assistant_response, history="") -> dict | None:
    try:
        scores = score_response(user_message or "", assistant_response or "", history or "")
    except Exception as exc:  # scoring is best-effort; never break the turn
        return {"error": str(exc)}
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "user_message": user_message or "",
        "assistant_response": assistant_response or "",
        **scores,
    }
    ledger = _hermes_home() / "logs" / "jev_rewards.jsonl"
    _append_ledger(entry, ledger)
    return entry
