"""Hermes plugin: inject a Jev skill-suggestion + routing hint into each turn.

Wires jev_suggest.suggest() to the pre_llm_call hook so every turn gets a
single cache-safe hint listing the top relevant skills (ranked by fit score),
plus a routing hint (toolset / MCP / delegate plan), per the TypeSafe
skill-suggestion cookbook.
"""
import logging

from . import jev_suggest

logger = logging.getLogger(__name__)


def register(ctx):
    def on_pre_llm_call(user_message, **kwargs):
        jev_suggest._metric("hook_called")
        if not jev_suggest._read_api_key():
            jev_suggest._metric("skipped", reason="no_api_key")
            return ""
        if jev_suggest._is_system_turn(user_message):
            jev_suggest._metric("skipped", reason="system_turn")
            return ""
        try:
            result = jev_suggest.suggest(jev_suggest._clean_user_message(user_message))
        except Exception as exc:  # never break the turn on a suggestion failure
            logger.warning("jev-skill-suggest failed: %s", exc)
            return ""
        block = ("" if result.get("reason") == "empty roster"
                 else jev_suggest.relevance_block(result.get("suggestions")))
        routing = jev_suggest.routing_block(result.get("routing"))
        parts = [p for p in (block, routing) if p]
        jev_suggest._metric(
            "suggest", suggestions=result.get("suggestions"),
            gate_mean=result.get("gate_mean"), routing=result.get("routing"),
            injected=bool(parts), injected_chars=sum(len(p) for p in parts))
        if parts:
            logger.debug("jev-skill-suggest: skills=%s routing=%s",
                         result.get("suggestions"), result.get("routing"))
        return "\n".join(parts)

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
