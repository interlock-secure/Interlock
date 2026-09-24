"""One narrow interface to a language model, and the Claude implementation of it.

Everything in :mod:`interlock.ai` codes against :class:`StructuredModel`: give
it a system prompt, untrusted user content and a Pydantic schema, get back an
instance of that schema or :class:`AIUnavailableError`. Tests use a fake that
returns recorded outputs, so the whole AI layer is exercised without a network
call or an API key.

Why structured output rather than free text
-------------------------------------------
The Claude API can constrain a response to a JSON schema
(``client.messages.parse(..., output_format=Model)``). That removes a whole
class of failure - unparseable replies - but not the one that matters: a
well-formed answer that is wrong. The deterministic checks in each feature
module exist for that second class, and they run on every response.

Configuration
-------------
``ANTHROPIC_API_KEY`` switches the AI on. ``INTERLOCK_AI_MODEL`` picks the
model (default ``claude-sonnet-5``). With no key, :func:`default_model` returns
None and every feature says the AI is not configured rather than pretending.
"""

from __future__ import annotations

import os
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-sonnet-5"
REQUEST_TIMEOUT_SECONDS = 25.0


class AIUnavailableError(RuntimeError):
    """The model could not produce a usable answer: no key, network, refusal."""


class StructuredModel(Protocol):
    """What every AI feature needs from a model."""

    name: str

    def parse(self, *, system: str, user: str, schema: type[T], max_tokens: int = 1024) -> T: ...


class ClaudeModel:
    """:class:`StructuredModel` backed by the Anthropic API."""

    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL) -> None:
        import anthropic

        self.name = model
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=1
        )

    def parse(self, *, system: str, user: str, schema: type[T], max_tokens: int = 1024) -> T:
        try:
            message = self._client.messages.parse(
                model=self.name,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except self._anthropic.APIError as failed:
            raise AIUnavailableError(f"model call failed: {type(failed).__name__}") from failed

        parsed = getattr(message, "parsed_output", None)
        if parsed is None:
            # A refusal or a truncated reply. Reported, never guessed at.
            raise AIUnavailableError(
                f"model returned no structured output (stop reason: "
                f"{getattr(message, 'stop_reason', 'unknown')})"
            )
        return parsed


def default_model() -> StructuredModel | None:
    """The configured model, or None when no API key is set."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return ClaudeModel(api_key=key, model=os.environ.get("INTERLOCK_AI_MODEL", DEFAULT_MODEL))


def fence_untrusted(text: str, *, label: str = "message") -> str:
    """Wrap text written outside the institution so the model treats it as data.

    The system prompts say that anything inside these tags is evidence, never
    instruction. A closing tag inside the text itself would let a sender break
    out of the fence, so it is neutralised first.
    """
    cleaned = text.replace(f"</{label}>", f"</ {label}>").replace(f"<{label}>", f"< {label}>")
    return f"<{label}>\n{cleaned}\n</{label}>"
