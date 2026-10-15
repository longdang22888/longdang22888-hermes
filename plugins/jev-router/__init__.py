"""Hermes plugin: inject a Jev advisory routing hint into each turn.

Wires jev_router.route() to the ``pre_llm_call`` hook: one TypeSafe fan-out per
turn over a bounded, redacted user_message, folded into a single advisory block
injected as context. Advisory only — the user's directive and approvals always
win, delegation never recurses, and every failure path is fail-open (empty
string, turn unaffected).
"""
import logging

from . import jev_router

logger = logging.getLogger(__name__)


def register(ctx):
    def on_pre_llm_call(user_message=None, **kwargs):
        jev_router._metric("hook_called")
        if not jev_router._read_api_key():
            jev_router._metric("skipped", reason="no_api_key")
            return ""
        try:
            verdict = jev_router.route(user_message)
        except Exception as exc:  # fail open: never break the turn
            logger.warning("jev-router failed: %s", exc)
            return ""
        if not verdict:
            return ""
        block = jev_router.advisory_block(verdict)
        jev_router._metric("injected", injected_chars=len(block),
                           delegate_target=verdict["delegate_target"])
        logger.debug("jev-router: scope=%s target=%s",
                     verdict["coding_scope"], verdict["delegate_target"])
        return block

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
