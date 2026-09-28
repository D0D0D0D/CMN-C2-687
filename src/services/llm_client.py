"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# LLM injection seam (docs/02_design.md §Framework Utilization). The template does NOT
# construct a concrete client or hard-code a model id — the LLM is injected via
# config["llm"] as a `shared.services.llm.base_llm.BaseLLM` implementation
# (`complete(messages: list[dict]) -> dict`, result["content"] carries the text).
#
# An un-injected seam (config["llm"] is None) is a dev/CI condition -> deterministic
# degradation (status=success + degradation_reason), NOT a production mode (production
# requires a real LLM — server.py / docs/07).

from __future__ import annotations

from typing import Any


class LlmCallError(RuntimeError):
    """Normalized LLM failure — the caller degrades gracefully."""


def call_llm(llm: Any, prompt: str) -> str:
    """Call the injected BaseLLM seam once and return the response text (str).

    - BaseLLM contract: ``complete(messages: list[dict]) -> dict`` with ``result["content"]``.
    - Absorbs implementations that return a str directly or expose a ``content`` attribute.
    - Any call failure (incl. a missing seam) is normalized to ``LlmCallError``.
    """
    if llm is None:
        raise LlmCallError("llm is not injected")
    messages = [{"role": "user", "content": prompt}]
    try:
        result = llm.complete(messages)
    except Exception as exc:  # noqa: BLE001 — normalize any LLM-impl exception to degrade
        raise LlmCallError(str(exc)) from exc

    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content", "")
        return content if isinstance(content, str) else str(content)
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return content
    return str(result)
