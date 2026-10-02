"""Jev skill-suggestion + turn-router pipeline (stdlib only, no third-party deps).

Pre-side, two TypeSafe requests per turn, following the typesafe-ai cookbook:
  Call 1 — one fan-out over the same state: a Choice ranks every skill, three
           Nouls gate whether the turn needs a skill at all, and three routing
           questions (needs_code / difficulty / delegatable) admit the turn.
           Parallel questions share state, so this costs ONE request.
  Call 2 — re-read the top-3 with full description + body excerpt; free to
           reject all.

Returns at most one skill name, plus a routing hint (delegate to a light/fast
model) that the plugin caller injects as a single cache-safe per-turn block via
pre_llm_call. Jev only answers; all thresholds live here in code.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

TYPESAFE_ENDPOINT = os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai/v1/systemone")
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")

SHORTLIST = 3
EXCERPT_CHARS = 700
GATE_THRESHOLD = 0.30
FITS_THRESHOLD = 0.30

# Routing thresholds (Jev answers, code decides). Difficulty is a 0-indexed score.
DIFFICULTY_LEVELS = ["trivial", "easy", "medium", "hard", "very_hard"]
DELEGATE_WHOLE_MAX = 2   # difficulty index <= 2 (trivial/easy/medium) → whole task
DELEGATE_PART_MIN = 3    # difficulty index >= 3 (hard/very_hard)      → offload a subtask

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
        meta, body = _parse_frontmatter(text)
        rel = Path(dirpath).relative_to(root)
        category = str(rel) if str(rel) != "." else "root"
        skills.append({
            "name": meta.get("name") or Path(dirpath).name,
            "category": category,
            "description": meta.get("description", "").strip(),
            "excerpt": body.strip()[:EXCERPT_CHARS],
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


def _post(questions: dict, state) -> dict:
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"TypeSafe HTTP {e.code}: {e.read().decode()[:200]}") from e


def _parse_routing(answers: dict) -> dict:
    """Fold the three routing answers into a typed routing dict + delegate hint."""
    needs_code = answers["needs_code"]["noul"] >= 0.5
    delegatable = answers["delegatable"]["noul"] >= 0.5
    diff = answers["difficulty"]
    idx = int(diff.get("score", 0))
    idx = max(0, min(idx, len(DIFFICULTY_LEVELS) - 1))
    label = DIFFICULTY_LEVELS[idx]
    hint = _delegate_hint(delegatable, idx)
    return {
        "needs_code": needs_code,
        "difficulty": label,
        "difficulty_index": idx,
        "delegatable": delegatable,
        "delegate_hint": hint,
    }


def _delegate_hint(delegatable: bool, idx: int) -> str | None:
    if not delegatable:
        return None
    if idx <= DELEGATE_WHOLE_MAX:
        return ("This task is short and self-contained; consider delegating it to a "
                "light/fast model (agy) via delegate_task, or just doing it directly.")
    if idx >= DELEGATE_PART_MIN:
        return ("This is a hard task; consider offloading a self-contained subtask to a "
                "light/fast model (agy) as a subagent to run in parallel.")
    return None


def suggest(request: str, roster: list[dict] | None = None) -> dict:
    skills = roster if roster is not None else load_roster()
    if not skills:
        return {"suggestion": None, "reason": "empty roster", "gate_mean": 0.0, "routing": None}

    # ---- Call 1: skim all skills + admit the turn (one fan-out) ----
    criteria = {s["name"]: s["description"] for s in skills}
    criteria["none"] = "No skill fits the request"
    r1 = _post({
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
        "delegatable": {
            "type": "noul",
            "instructions": "Is at least part of this task suitable to offload to a "
                            "fast/light model (short and self-contained)?",
            "criteria": {
                "true": "Part or all of it can be done by a light model",
                "false": "It needs the full capability of the main model",
            },
        },
    }, {"request": request, "skills_index": _render_index(skills)})

    answers = r1["answers"]
    probs = answers["which_skill"]["probabilities"]
    ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
    top3 = [name for name, _ in ranked if name != "none"][:SHORTLIST]
    gate_mean = sum(answers[k]["noul"] for k in ("act_on_stuff", "follow_steps", "just_talk")) / 3

    result = {"gate_mean": round(gate_mean, 3), "top3": top3,
              "suggestion": None, "reason": "",
              "routing": _parse_routing(answers)}

    if gate_mean < GATE_THRESHOLD:
        result["reason"] = f"gate_mean {gate_mean:.3f} < {GATE_THRESHOLD}"
        return result
    if not top3:
        result["reason"] = "no candidate ranked above 'none'"
        return result

    by_name = {s["name"]: s for s in skills}
    candidates = [by_name[n] for n in top3 if n in by_name]
    if not candidates:
        result["reason"] = "shortlist not in roster"
        return result

    # ---- Call 2: read the top-3 properly ----
    c2 = _post({
        "best": {
            "type": "choice",
            "instructions": "Which of these candidate skills actually fits the "
                            "request? Choose 'none' if none do.",
            "criteria": {c["name"]: c["description"] for c in candidates}
                        | {"none": "None of these fit"},
        },
        **{c["name"]: {
            "type": "noul",
            "instructions": f"Does the '{c['name']}' skill genuinely apply to "
                            f"this request?",
            "criteria": {
                "true": "It can perform the task described in the request",
                "false": "It does not apply to this request",
            },
        } for c in candidates},
    }, {"request": request,
        "candidates": [{"name": c["name"], "description": c["description"],
                        "excerpt": c["excerpt"]} for c in candidates]})

    best = c2["answers"]["best"]["choice"]
    fits = {n: c2["answers"][n]["noul"] for n in c2["answers"] if n != "best"}
    best_fit = max(fits.values()) if fits else 0.0
    result["best_choice"] = best
    result["shortlist_fits"] = {n: round(v, 3) for n, v in fits.items()}

    if best == "none":
        result["reason"] = f"call-2 choice was 'none' (best fit {best_fit:.3f})"
        return result
    if best_fit < FITS_THRESHOLD:
        result["reason"] = f"best='{best}' but best_fit {best_fit:.3f} < {FITS_THRESHOLD}"
        return result

    result["suggestion"] = best
    result["reason"] = f"chose '{best}' (fit {best_fit:.3f})"
    return result


def suggestion_block(skill: str | None) -> str:
    if not skill:
        return ""
    return (f"Relevant to the current request: {skill}. "
            f"Ignore this if it does not fit what the user actually asked for.")


def routing_block(routing: dict | None) -> str:
    """Render the routing verdict as a compact advisory hint ('' when none)."""
    if not routing or not routing.get("delegate_hint"):
        return ""
    return (
        f"[Routing: needs_code={'y' if routing['needs_code'] else 'n'}; "
        f"difficulty={routing['difficulty']}] {routing['delegate_hint']}"
    )
