"""Hermes plugin: score each finished turn with Jev and record the reward.

Wires post_llm_call (observer — return ignored, so it never blocks or alters
the user's response) to jev_scorer.record_turn(), appending one JSONL line per
turn to ~/.hermes/logs/jev_rewards.jsonl.
"""
import logging

from . import jev_scorer

logger = logging.getLogger(__name__)


def register(ctx):
    def on_post_llm_call(session_id=None, user_message=None, assistant_response=None,
                        conversation_history=None, **kwargs):
        if not jev_scorer._read_api_key():
            return
        try:
            result = jev_scorer.record_turn(
                session_id, user_message, assistant_response, conversation_history)
        except Exception as exc:
            logger.warning("jev-response-scorer failed: %s", exc)
            return
        if result and "error" not in result:
            logger.debug("jev-response-scorer: satisfied=%.2f research=%.2f decision=%s session=%s",
                         result["satisfied_intent"], result["needs_research"],
                         result["decision"], session_id)
        else:
            logger.warning("jev-response-scorer: %s", result.get("error") if result else "no result")

    ctx.register_hook("post_llm_call", on_post_llm_call)

    # Steering: tell the agent how it is scored. Cache-safe — rendered once per
    # session at position after_memory, so it never breaks prompt caching.
    ctx.register_system_prompt_section(
        jev_scorer.CRITERIA_SECTION_ID,
        jev_scorer.criteria_text,
        position="after_memory",
        max_chars=2000,
    )
