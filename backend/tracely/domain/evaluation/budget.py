"""Context-window budget for judge calls — pure, no I/O.

Every evaluation column has a model, and every model has an input budget (`provider.model_info`:
a decision model's is fixed — Jev reads at most 32k tokens of `state` + question — an LLM's comes
from OpenRouter's catalog). Before spending a call the judge asks `fits()`; when the item does not
fit, the column's `fallback_model` grades it instead, or the item is recorded as skipped. Nothing is
ever truncated to squeeze under the limit: a verdict on half a conversation is a wrong verdict
that looks like a right one.

There is no public tokenizer for every model (Jev's isn't published), and one bytes-per-token
ratio cannot be both safe and useful: measured live against Jev on 2026-09-27, English prose ran
~6 bytes/token while JSON tool payloads and code ran ~2.4. So the estimate is a LOWER BOUND
(8 bytes/token, below anything real text reaches): when even the lower bound is over the limit the
item certainly does not fit and no call is spent on it. Everything closer is sent, and the
provider's own rejection — fast, and reported as `max_tokens_exceeded` / "maximum context length"
(`provider.ContextOverflowError`) — is the exact check, taking the same fallback-or-skip path. A
single high estimate would have routed ordinary prose to the fallback at a third of Jev's real
capacity; a single low one would have under-counted JSON.
"""

from __future__ import annotations

import math
from typing import Any

_BYTES_PER_TOKEN_FLOOR = 8


def estimate_tokens(*parts: str | None) -> int:
    """A lower bound on the tokens in the concatenation of `parts` — see the module docstring."""
    n = sum(len((p or "").encode("utf-8")) for p in parts)
    return math.ceil(n / _BYTES_PER_TOKEN_FLOOR)


def fits(info: dict[str, Any], prompt_tokens: int, reserved_output: int = 0) -> bool:
    """Whether a prompt of `prompt_tokens` fits the model described by `info`
    (`{kind, context_tokens}`, from `provider.model_info`).

    A decision model's budget covers input only (it emits no text). An LLM's context holds the
    prompt AND the completion, so the output cap the provider reserves is subtracted. Unknown
    context never blocks — the provider's own rejection is the backstop."""
    limit = info.get("context_tokens")
    if not limit:
        return True
    if info.get("kind") == "decision":
        return prompt_tokens <= limit
    return prompt_tokens + max(reserved_output, 0) <= limit


def overflow_note(model_id: str, prompt_tokens: int, info: dict[str, Any]) -> str:
    """The human sentence a skipped cell shows."""
    limit = info.get("context_tokens") or 0
    return (
        f"Input is over {_k(prompt_tokens)} tokens, past the {_k(limit)} context of {model_id}. "
        "Set a fallback model on this column to grade long items."
    )


def k(n: int) -> str:
    """`32000` → `32k`, for the sentences cells show."""
    return _k(n)


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)
