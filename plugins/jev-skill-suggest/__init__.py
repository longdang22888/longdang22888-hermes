"""Hermes plugin: inject a Jev skill suggestion + routing hint into each turn.

Wires jev_suggest.suggest() to the pre_llm_call hook so every turn gets a
single cache-safe hint naming at most one relevant skill, plus a routing hint
(delegate to a light model when the turn is short and self-contained), per the
TypeSafe skill-suggestion cookbook.
"""
import logging

from . import jev_suggest

logger = logging.getLogger(__name__)


def register(ctx):
    def on_pre_llm_call(user_message, **kwargs):
        if not jev_suggest._read_api_key():
            return ""
        try:
            result = jev_suggest.suggest(user_message or "")
        except Exception as exc:  # never break the turn on a suggestion failure
            logger.warning("jev-skill-suggest failed: %s", exc)
            return ""
        block = jev_suggest.suggestion_block(result.get("suggestion"))
        routing = jev_suggest.routing_block(result.get("routing"))
        parts = [p for p in (block, routing) if p]
        if parts:
            logger.debug("jev-skill-suggest: skill=%s routing=%s",
                         result.get("suggestion"), result.get("routing"))
        return "\n".join(parts)

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
