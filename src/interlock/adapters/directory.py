"""Suggesting which bank sent a request, without trusting the request.

The intake agent never fills in a bank from message text, because a sender
can write any bank's name. What an operator can use is a *suggestion* from a
directory the institution maintains: the ``From:`` domain, or a bank name the
message mentions, matched against known counterparties.

Two kinds of match, labelled differently on screen:

- **Sender domain** - the ``From:`` address matches a counterparty's known
  domain. Stronger, but a pasted email's header is still text the sender
  controls, so it is a suggestion to confirm against the channel it arrived on.
- **Name mentioned** - the text names a known bank. Weaker, and shown as such.

Nothing is filled in silently. The operator types or confirms the identifier.

The demo directory below is synthetic, like everything else in this
repository. A deployment would load its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Counterparty:
    institution_id: str
    name: str
    domains: tuple[str, ...]


DEMO_DIRECTORY: tuple[Counterparty, ...] = (
    Counterparty("inst-northbay-cu", "Northbay Credit Union", ("northbay-cu.example",)),
    Counterparty("inst-pinebrook-bank", "Pinebrook Bank", ("pinebrook.example",)),
    Counterparty(
        "inst-first-national-trust", "First National Trust", ("first-national-trust.example",)
    ),
    Counterparty("inst-cedar-trust", "Cedar Trust", ("cedartrust.example",)),
    Counterparty("inst-harbor-national", "Harbor National", ("harbor-national.example",)),
)


@dataclass(frozen=True, slots=True)
class BankSuggestion:
    institution_id: str
    name: str
    basis: str
    """"sender domain" or "name mentioned"."""
    evidence: str


_FROM = re.compile(r"^\s*from:\s*.*?@([a-z0-9.-]+)", re.I | re.M)


def suggest_requesting_bank(
    text: str,
    directory: tuple[Counterparty, ...] = DEMO_DIRECTORY,
    *,
    exclude: str | None = None,
) -> BankSuggestion | None:
    """The best directory match, or None. Never a guess outside the directory.

    Args:
        exclude: our own institution id. A message addressed to us names us,
            and suggesting ourselves as the requester would be wrong.
    """
    directory = tuple(p for p in directory if p.institution_id != exclude)
    sender = _FROM.search(text)
    if sender:
        domain = sender.group(1).lower().rstrip(".")
        for party in directory:
            if domain in party.domains:
                return BankSuggestion(
                    party.institution_id, party.name, "sender domain", sender.group(0).strip()
                )

    lowered = text.lower()
    named = [p for p in directory if p.name.lower() in lowered]
    if len(named) == 1:
        only = named[0]
        return BankSuggestion(only.institution_id, only.name, "name mentioned", only.name)
    return None
