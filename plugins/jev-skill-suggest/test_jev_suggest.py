"""Pure-logic tests for the expanded jev-skill-suggest routing (no API, no I/O)."""
import sys
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
        "delegate_plan": {"choice": "delegate_whole"},
    }
    r = _routing(a)
    assert "DELEGATE WHOLE TASK" in js.routing_block(r)


def test_delegate_single_directive():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 2},
        "which_toolset": {"choice": "none"},
        "delegate_plan": {"choice": "delegate_single"},
    }
    r = _routing(a)
    assert "OFFLOAD ONE SUBTASK" in js.routing_block(r)


def test_toolset_none_omitted():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 1},
        "which_toolset": {"choice": "none"},
        "delegate_plan": {"choice": "no_delegate"},
    }
    block = js.routing_block(_routing(a))
    assert "toolset=" not in block
    assert block == "[Routing: needs_code=y; difficulty=easy]"


def test_unknown_delegate_plan_defaults_none():
    a = {
        "needs_code": {"noul": 0.9},
        "difficulty": {"score": 1},
        "which_toolset": {"choice": "none"},
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
    assert r["delegate_plan"] == "no_delegate"
    assert r["delegate_hint"] is None


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
