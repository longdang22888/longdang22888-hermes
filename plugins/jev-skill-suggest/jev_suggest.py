"""Jev skill-suggestion + turn-router pipeline (stdlib only, no third-party deps).

Pre-side, one TypeSafe fan-out request per turn, following the typesafe-ai
cookbook: a Choice ranks every skill (its ``probabilities`` are the fit scores),
three Nouls gate whether the turn needs a skill at all, and routing questions
(needs_code / difficulty / which_toolset / which_mcp / delegate_plan) admit the
turn. Parallel questions share state, so this costs ONE request.

Returns a ranked list of the skills whose choice-probability clears
FITS_THRESHOLD (best first, each with its score), plus a routing hint (toolset /
MCP / delegate plan) that the plugin caller injects as a single cache-safe
per-turn block via pre_llm_call. Jev only answers; all thresholds live here in
code.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TYPESAFE_ENDPOINT = os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai/v1/systemone")
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-1.13.0")  # pinned: thresholds are validated per model version

TOP_N = 5
GATE_THRESHOLD = 0.30
FITS_THRESHOLD = 0.30

# Routing thresholds (Jev answers, code decides). Difficulty is a 0-indexed score.
DIFFICULTY_LEVELS = ["trivial", "easy", "medium", "hard", "very_hard"]

# Tool/delegate routing — answered inside the call-1 fan-out (zero extra requests).
TOOLSET_INDEX = [
    ("web", "Web research / search / extract content"),
    ("terminal", "Run shell commands, builds, git, processes"),
    ("file", "Read/write/patch/search files"),
    ("code_execution", "Run Python that calls Hermes tools programmatically"),
    ("skills", "Load/create/edit skills"),
    ("memory", "Persistent memory / user profile"),
    ("delegation", "Spawn subagents (delegate_task)"),
    ("browser", "Browser automation (navigate/click/type)"),
    ("cronjob", "Scheduled tasks"),
    ("clarify", "Ask user clarifying questions"),
    ("vision", "Image analysis"),
    ("tts", "Text-to-speech"),
    ("todo", "Task planning"),
    ("session_search", "Recall past conversations"),
    ("none", "No tool needed"),
]
_DELEGATE_DIRECTIVES = {
    "no_delegate": "",
    "delegate_whole": (
        "DELEGATE WHOLE TASK: call delegate_task with a single task entry "
        "(goal + self-contained context). Do it inline if trivial."
    ),
    "delegate_parallel": (
        "SPLIT & PARALLELIZE: decompose into 2-5 independent subtasks and spawn them "
        "in ONE delegate_task call (parallel children); merge results after."
    ),
    "delegate_single": (
        "OFFLOAD ONE SUBTASK: delegate exactly one self-contained piece via delegate_task; "
        "keep the main reasoning and synthesis local."
    ),
}

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _read_api_key() -> str | None:
    """Read TYPESAFE_API_KEY from env or the profile .env file (secrets only)."""
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


def _parse_frontmatter(text: str):
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm, body = m.group(1), text[m.end():]
    meta = {}
    for line in fm.splitlines():
        if not line or line.lstrip().startswith(("#", "-", "!")) or line[:1].isspace():
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("\"", "'"):
            v = v[1:-1]
        meta[k.strip()] = v
    return meta, body


def load_roster() -> list[dict]:
    """Discover skills from the profile skills dir (mirrors build_roster.py)."""
    root = _hermes_home() / "skills"
    if not root.is_dir():
        return []
    skills = []
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        if "SKILL.md" not in filenames:
            continue
        p = Path(dirpath) / "SKILL.md"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, _body = _parse_frontmatter(text)
        rel = Path(dirpath).relative_to(root)
        category = str(rel) if str(rel) != "." else "root"
        skills.append({
            "name": meta.get("name") or Path(dirpath).name,
            "category": category,
            "description": meta.get("description", "").strip(),
        })
    by_name = {}
    for s in skills:
        by_name.setdefault(s["name"], s)
    return sorted(by_name.values(), key=lambda s: (s["category"], s["name"]))


def _render_index(skills: list[dict]) -> str:
    by_cat: dict[str, list[dict]] = {}
    for s in skills:
        by_cat.setdefault(s["category"], []).append(s)
    lines = []
    for cat in sorted(by_cat):
        lines.append(f"{cat}:")
        for s in sorted(by_cat[cat], key=lambda x: x["name"]):
            lines.append(f"  {s['name']}: {s['description']}")
    return "\n".join(lines)


def load_mcp_servers() -> list[str]:
    """Enabled MCP server names from config.yaml -> mcp_servers (stdlib-only YAML-lite scan).

    Only reads the top-level ``mcp_servers:`` block and returns the 2-space-indented
    server keys; skips comments and stops at the next top-level key. No yaml module.
    """
    try:
        lines = (_hermes_home() / "config.yaml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    names, in_block = [], False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not in_block:
            if re.match(r"^mcp_servers\s*:", line):
                in_block = True
            continue
        if line[0] not in (" ", "\t"):  # back to a top-level key → done
            break
        indent = len(line) - len(line.lstrip(" "))
        if indent == 2 and line.rstrip().endswith(":"):
            names.append(line.strip()[:-1])
    return names


_SYSTEM_TURN_PREFIXES = ("[Background process", "[IMPORTANT: Background", "[ASYNC DELEGATION",
                         "[Continuing toward your standing goal]")
_SECRET_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_\-]{16,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]{16,}"
    r"|(?i:[A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)\s*[=:]\s*[^\s'\"]{6,})")
_env_secrets = {"mtime": None, "values": []}


def _is_system_turn(user_message) -> bool:
    """Automated notices (background process, delegation, goal loop) carry no user intent."""
    return (user_message or "").lstrip().startswith(_SYSTEM_TURN_PREFIXES)


def _clean_user_message(user_message, limit: int = 1200, head: int = 300, tail: int = 700) -> str:
    """Trim command/skill boilerplate: keep the opening and the tail, where the real ask sits."""
    msg = user_message or ""
    if len(msg) > limit and msg.lstrip().startswith("["):
        return msg[:head] + "\n[...]\n" + msg[-tail:]
    return msg[-4000:]


def _known_secrets() -> list:
    """Secret-looking values from ~/.hermes/.env, so exact known secrets never leave the machine."""
    try:
        p = _hermes_home() / ".env"
        mtime = p.stat().st_mtime
        if _env_secrets["mtime"] != mtime:
            vals = set()
            for line in p.read_text(encoding="utf-8").splitlines():
                k, _, v = line.partition("=")
                v = v.strip().strip('"').strip("'")
                if re.search(r"KEY|TOKEN|SECRET|PASSWORD|PASS", k.upper()) and len(v) >= 12:
                    vals.add(v)
            _env_secrets.update(mtime=mtime, values=sorted(vals, key=len, reverse=True))
        return _env_secrets["values"]
    except Exception:
        return []


def _redact_text(text) -> str:
    s = text if isinstance(text, str) else str(text)
    for v in _known_secrets():
        s = s.replace(v, "[REDACTED]")
    return _SECRET_RE.sub("[REDACTED]", s)


def _redact_obj(o):
    if isinstance(o, str):
        return _redact_text(o)
    if isinstance(o, dict):
        return {k: _redact_obj(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_redact_obj(v) for v in o]
    return o


PLUGIN = "jev-skill-suggest"
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


REQUEST_TIMEOUT_S = 8     # Jev answers in ~0.1-0.7s; a hung call should fail fast (fail-open)
MAX_ATTEMPTS = 1           # retries only transient network/5xx errors


def _post(questions: dict, state) -> dict:
    """Timed wrapper around _post_raw: records latency, attempts and ok/error per request."""
    t0 = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            r = _post_raw(questions, state)
            break
        except Exception as exc:
            transient = (isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError))
                         or (isinstance(exc, RuntimeError) and "HTTP 5" in str(exc)))
            if transient and attempt < MAX_ATTEMPTS:
                continue
            _metric("request", ok=False, error=type(exc).__name__, attempts=attempt,
                    n_questions=len(questions), latency_ms=int((time.monotonic() - t0) * 1000))
            raise
    _metric("request", ok=True, attempts=attempt, n_questions=len(questions),
            latency_ms=int((time.monotonic() - t0) * 1000))
    return r


def _post_raw(questions: dict, state) -> dict:
    before = json.dumps(state, ensure_ascii=False, default=str)
    state = _redact_obj(state)   # never send secrets to the vendor
    if json.dumps(state, ensure_ascii=False, default=str) != before:
        _metric("redaction", n_questions=len(questions))
    api_key = _read_api_key()
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY not set")
    body = json.dumps({"state": state, "model": TYPESAFE_MODEL, "questions": questions}).encode()
    req = urllib.request.Request(
        TYPESAFE_ENDPOINT, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"TypeSafe HTTP {e.code}: {e.read().decode()[:200]}") from e


def _parse_routing(answers: dict) -> dict:
    """Fold the routing answers into a typed routing dict + delegate directive."""
    needs_code = answers["needs_code"]["noul"] >= 0.5
    diff = answers["difficulty"]
    idx = int(diff.get("score", 0))
    idx = max(0, min(idx, len(DIFFICULTY_LEVELS) - 1))
    label = DIFFICULTY_LEVELS[idx]
    toolset = answers.get("which_toolset", {}).get("choice", "none")
    mcp = answers.get("which_mcp", {}).get("choice", "none")
    plan = answers.get("delegate_plan", {}).get("choice", "no_delegate")
    hint = _delegate_directive(plan)
    return {
        "needs_code": needs_code,
        "difficulty": label,
        "difficulty_index": idx,
        "toolset": toolset,
        "mcp": mcp,
        "delegate_plan": plan,
        "delegate_hint": hint,
    }


def _delegate_directive(plan: str) -> str | None:
    return _DELEGATE_DIRECTIVES.get(plan) or None


def suggest(request: str, roster: list[dict] | None = None) -> dict:
    skills = roster if roster is not None else load_roster()
    if not skills:
        return {"suggestions": [], "reason": "empty roster", "gate_mean": 0.0, "routing": None}

    # ---- Call 1: skim all skills + admit the turn (one fan-out) ----
    criteria = {s["name"]: s["description"] for s in skills}
    criteria["none"] = "No skill fits the request"
    mcp_servers = load_mcp_servers()
    fanout = {
        "which_skill": {
            "type": "choice",
            "instructions": "Which skill best fits the user's request? Choose the "
                            "single most relevant one, or 'none' if nothing fits.",
            "criteria": criteria,
        },
        "act_on_stuff": {
            "type": "noul",
            "instructions": "Does the request ask to act on the user's files, "
                            "data, or systems (not just talk)?",
        },
        "follow_steps": {
            "type": "noul",
            "instructions": "Does the request need a written multi-step procedure "
                            "or specialist workflow?",
        },
        "just_talk": {
            "type": "noul",
            "instructions": "Is the request purely conversational, with no task "
                            "to execute?",
        },
        "needs_code": {
            "type": "noul",
            "instructions": "Does this request require writing or editing code or "
                            "creating/modifying files, or does it only need an answer?",
            "criteria": {
                "true": "It requires code, file creation/editing, or running commands",
                "false": "It only needs a text answer or explanation",
            },
        },
        "difficulty": {
            "type": "score",
            "instructions": "How difficult is the user's request to complete correctly?",
            "criteria": [
                "Trivial — a one-line answer",
                "Easy — simple, self-contained task",
                "Medium — moderate, needs a few steps or some care",
                "Hard — complex, multi-step, or needs specialist knowledge",
                "Very hard — large, open-ended, or high-risk",
            ],
        },
        "which_toolset": {
            "type": "choice",
            "instructions": "Which single toolset best serves this request? "
                            "Choose 'none' if no tool is needed.",
            "criteria": {name: desc for name, desc in TOOLSET_INDEX},
        },
        "delegate_plan": {
            "type": "choice",
            "instructions": "How should this task be delegated to subagents, if at all?",
            "criteria": {
                "no_delegate": "Do it inline; no subagent",
                "delegate_whole": "Whole task → one subagent",
                "delegate_parallel": "Split into independent subtasks → run in parallel",
                "delegate_single": "Offload one self-contained subtask; keep main work local",
            },
        },
    }
    if mcp_servers:
        fanout["which_mcp"] = {
            "type": "choice",
            "instructions": "Which MCP server (if any) provides this capability?",
            "criteria": {**{s: s for s in mcp_servers}, "none": "No MCP needed"},
        }
    r1 = _post(fanout, {"request": request, "skills_index": _render_index(skills)})

    answers = r1["answers"]
    probs = answers["which_skill"]["probabilities"]
    ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
    gate_mean = sum(answers[k]["noul"] for k in ("act_on_stuff", "follow_steps", "just_talk")) / 3

    result = {"gate_mean": round(gate_mean, 3), "suggestions": [],
              "reason": "", "routing": _parse_routing(answers)}

    if gate_mean < GATE_THRESHOLD:
        result["reason"] = f"gate_mean {gate_mean:.3f} < {GATE_THRESHOLD}"
        return result

    # Ranked skills come straight from which_skill's choice-probabilities (0..1);
    # these already reflect the request and are the fit scores we hand the model.
    above = [(name, round(float(p), 3)) for name, p in ranked
             if name != "none" and float(p) >= FITS_THRESHOLD][:TOP_N]
    if not above:
        result["reason"] = f"no candidate scored >= {FITS_THRESHOLD}"
        return result

    result["suggestions"] = above
    result["reason"] = (f"ranked {len(above)} skill(s): "
                        + ", ".join(f"{n}={s}" for n, s in above))
    return result


def suggestion_block(suggestions) -> str:
    """Render a ranked list of (name, score) tuples; '' when empty."""
    suggestions = [s for s in (suggestions or []) if s and s[0]]
    if not suggestions:
        return ""
    lines = []
    for i, (name, score) in enumerate(suggestions, 1):
        score_str = f" ({score:.2f})" if isinstance(score, (int, float)) else ""
        lines.append(f"{i}. {name}{score_str}")
    numbered = "\n".join(lines)
    return (f"Relevant skills, ranked by fit:\n{numbered}\n"
            f"Ignore any that do not fit what the user actually asked for.")


NO_SKILL_TEXT = "No skill in the roster appears relevant to this request."


def relevance_block(suggestions) -> str:
    """Always say something: silence leaves the roster's 'err on the side of loading' unopposed."""
    body = suggestion_block(suggestions) or NO_SKILL_TEXT
    return f"<skill_relevance>\n{body}\n</skill_relevance>"


def routing_block(routing: dict | None) -> str:
    """Render the routing verdict as a compact advisory hint ('' when none)."""
    if not routing:
        return ""
    parts = [f"needs_code={'y' if routing['needs_code'] else 'n'}",
             f"difficulty={routing['difficulty']}"]
    if routing.get("toolset") and routing["toolset"] != "none":
        parts.append(f"toolset={routing['toolset']}")
    if routing.get("mcp") and routing["mcp"] != "none":
        parts.append(f"mcp={routing['mcp']}")
    header = f"[Routing: {'; '.join(parts)}]"
    hint = routing.get("delegate_hint")
    return f"{header} {hint}".strip() if hint else header
