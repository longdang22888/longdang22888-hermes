"""Hermes plugin: force a coding turn to continue when Jev QC finds the change short."""
import logging

from . import jev_verify

logger = logging.getLogger(__name__)


def register(ctx):
    ctx.register_hook("pre_llm_call", jev_verify.on_pre_llm_call)
    ctx.register_hook("post_tool_call", jev_verify.on_post_tool_call)
    ctx.register_hook("on_session_end", jev_verify.on_session_end)

    def on_pre_verify(session_id=None, final_response=None, changed_paths=None, **kwargs):
        try:
            nudge = jev_verify.evaluate(session_id, final_response, changed_paths)
        except Exception as exc:  # fail open: never trap the turn
            logger.warning("jev-verify-gate failed: %s", exc)
            return None
        if nudge:
            logger.debug("jev-verify-gate: continue session=%s nudge=%r", session_id, nudge)
            return {"action": "continue", "message": nudge}
        logger.debug("jev-verify-gate: accept session=%s", session_id)
        return None

    ctx.register_hook("pre_verify", on_pre_verify)
