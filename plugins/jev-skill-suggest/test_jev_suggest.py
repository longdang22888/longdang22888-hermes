"""Pure-logic tests for the expanded jev-skill-suggest routing (no API, no I/O)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_suggest as js


def _routing(answers):
    return js._parse_routing(answers)


def test_no_delegate_produces_no_directive():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 1},
        "which_toolset": {"choice": "terminal"},
        "which_mcp": {"choice": "none"},
        "delegate_plan": {"choice": "no_delegate"},
    }
    r = _routing(a)
    assert r["delegate_hint"] is None
    assert r["delegate_plan"] == "no_delegate"
    assert js.routing_block(r) == "[Routing: needs_code=y; difficulty=easy; toolset=terminal]"


def test_delegate_parallel_directive():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 3},
        "which_toolset": {"choice": "none"},
        "which_mcp": {"choice": "none"},
        "delegate_plan": {"choice": "delegate_parallel"},
    }
    r = _routing(a)
    assert r["delegate_hint"] is not None
    assert "SPLIT & PARALLELIZE" in js.routing_block(r)


def test_delegate_whole_directive():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 1},
        "which_toolset": {"choice": "none"},
        "which_mcp": {"choice": "none"},
        "delegate_plan": {"choice": "delegate_whole"},
    }
    r = _routing(a)
    assert "DELEGATE WHOLE TASK" in js.routing_block(r)


def test_delegate_single_directive():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 2},
        "which_toolset": {"choice": "none"},
        "which_mcp": {"choice": "none"},
        "delegate_plan": {"choice": "delegate_single"},
    }
    r = _routing(a)
    assert "OFFLOAD ONE SUBTASK" in js.routing_block(r)


def test_toolset_none_omitted():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 1},
        "which_toolset": {"choice": "none"},
        "which_mcp": {"choice": "none"},
        "delegate_plan": {"choice": "no_delegate"},
    }
    block = js.routing_block(_routing(a))
    assert "toolset=" not in block
    assert block == "[Routing: needs_code=y; difficulty=easy]"


def test_mcp_none_omitted():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 1},
        "which_toolset": {"choice": "none"},
        "which_mcp": {"choice": "none"},
        "delegate_plan": {"choice": "no_delegate"},
    }
    block = js.routing_block(_routing(a))
    assert "mcp=" not in block


def test_mcp_shown_when_present():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 1},
        "which_toolset": {"choice": "none"},
        "which_mcp": {"choice": "apify"},
        "delegate_plan": {"choice": "no_delegate"},
    }
    block = js.routing_block(_routing(a))
    assert "mcp=apify" in block


def test_unknown_delegate_plan_defaults_none():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 1},
        "which_toolset": {"choice": "none"},
        "which_mcp": {"choice": "none"},
        "delegate_plan": {"choice": "bogus"},
    }
    assert _routing(a)["delegate_hint"] is None


def test_missing_new_keys_default_safely():
    # Backward-compatible: if a hypothetical caller omits the new keys entirely.
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 2},
    }
    r = _routing(a)
    assert r["toolset"] == "none"
    assert r["mcp"] == "none"
    assert r["delegate_plan"] == "no_delegate"
    assert r["delegate_hint"] is None


def test_load_mcp_servers():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "config.yaml").write_text(
            "# comment\n"
            "model:\n"
            "  default: gpt\n"
            "mcp_servers:\n"
            "  apify:\n"
            "    url: https://mcp.apify.com/\n"
            "  mem0-mcp:\n"
            "    url: https://mcp.mem0.ai/mcp\n"
            "  # disabled:\n"
            "  #   url: x\n"
            "platform_toolsets:\n"
            "  cli: [web]\n"
        )
        old = js._hermes_home
        js._hermes_home = lambda: Path(td)
        try:
            assert js.load_mcp_servers() == ["apify", "mem0-mcp"]
        finally:
            js._hermes_home = old


def test_load_mcp_servers_empty_when_absent():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "config.yaml").write_text("model:\n  default: gpt\n")
        old = js._hermes_home
        js._hermes_home = lambda: Path(td)
        try:
            assert js.load_mcp_servers() == []
        finally:
            js._hermes_home = old


def test_suggestion_block_ranked_list():
    block = js.suggestion_block([("obsidian", 0.87), ("notion", 0.61), ("pdf", 0.45)])
    assert "1. obsidian (0.87)" in block
    assert "2. notion (0.61)" in block
    assert "3. pdf (0.45)" in block


def test_suggestion_block_single_entry():
    block = js.suggestion_block([("obsidian", 0.87)])
    assert "1. obsidian (0.87)" in block


def test_suggestion_block_empty():
    assert js.suggestion_block([]) == ""
    assert js.suggestion_block(None) == ""


def test_relevance_block_ranked():
    block = js.relevance_block([("obsidian", 0.9)])
    assert "<skill_relevance>" in block
    assert "obsidian (0.90)" in block


def test_relevance_block_no_skill():
    block = js.relevance_block([])
    assert js.NO_SKILL_TEXT in block


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{ 'OK' if failures == 0 else 'FAILED: ' + str(failures) }")
    sys.exit(1 if failures else 0)
