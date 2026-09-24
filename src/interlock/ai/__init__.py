"""The AI layer: a model that reads, suggests and drafts, and never decides.

Three jobs, one rule.

- :mod:`~interlock.ai.intake` reads a free-text recall request and proposes a
  case draft, every field tied to a quote from the message.
- :mod:`~interlock.ai.recommend` suggests an outcome for a case, citing only
  facts the system gave it.
- :mod:`~interlock.ai.drafting` writes the reply to the counterparty for an
  operator to review.

The rule: **the model never records anything.** A draft becomes a case only
through :meth:`~interlock.adapters.freetext.CaseDraft.confirm` with a named
operator; a recommendation becomes an outcome only when an operator applies a
transition; a drafted reply is text on a screen. Every model output passes
deterministic checks before it is shown, and a check that fails produces a
visible "rejected" or "unavailable" state rather than a quieter fallback that
looks like an answer - the second of the project's three rules.
"""
