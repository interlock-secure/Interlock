"""The adapter contract, and what "round-trip" is taken to mean here.

Every rail adapter implements the same two operations: parse a native message
into a :class:`~interlock.schema.case.RecallCase`, and emit a native message
from one.

What round-trip means, precisely
--------------------------------
The obvious reading - "any input we parse, we can re-emit byte for byte" - is
not achievable against real XML and it is dishonest to claim it. Two messages
can be semantically identical and differ in whitespace, element ordering among
optional siblings, namespace prefix choice, or the presence of optional elements
the sender happened to include.

So the guarantee is split in two, and both halves are tested:

**Canonical round-trip.** For a message already in Interlock's canonical form,
``emit(parse(m)) == m`` byte for byte. The golden fixtures are canonical, and
this is what stops an emitter drifting.

**Semantic round-trip.** For a message that is *not* canonical - extra
whitespace, a different namespace prefix, optional elements in another order -
``parse(messy)`` produces the same case as ``parse(canonical)``. This is what
stops a parser being brittle against a real counterparty.

Claiming only the first would be a system that works against its own output.
Claiming only the second would let the emitter rot. Both together mean an
adapter can be trusted in either direction.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from interlock.schema.case import Channel, RecallCase
from interlock.schema.common import Rail


class AdapterError(Exception):
    """Base class for every failure to translate a rail message."""


class MalformedMessageError(AdapterError):
    """The input is not a valid message of the expected type.

    Distinct from :class:`UnsupportedMessageError`: this one means we recognised
    what it was trying to be and it was broken.
    """


class UnsupportedMessageError(AdapterError):
    """A well-formed message this adapter does not handle.

    Its own class because the operational response differs. A malformed message
    is a counterparty bug worth reporting back; an unsupported one is usually a
    routing mistake on our side, and the case should go to another adapter
    rather than to an error queue.
    """


class LossyRoundTripError(AdapterError):
    """Emitting would silently drop something the input carried.

    Raised by an emitter that finds it cannot reproduce a field it was given.
    Loud by design: the alternative is a counterparty receiving a case reference
    we quietly truncated, and discovering it during a recovery dispute.
    """


@runtime_checkable
class RailAdapter(Protocol):
    """What every rail adapter provides.

    A Protocol rather than an abstract base class, so an adapter is anything
    with the right shape. That keeps the free-text adapter - which is a stub in
    M3 and an LLM call in M7 - from having to inherit machinery designed for
    XML.
    """

    rail: Rail
    channel: Channel

    def parse(self, raw: bytes) -> RecallCase:
        """Translate a native message into a canonical case.

        Raises:
            MalformedMessageError: the message is broken.
            UnsupportedMessageError: this adapter does not handle it.
        """
        ...

    def emit(self, case: RecallCase) -> bytes:
        """Render a canonical case back into a native message.

        Raises:
            LossyRoundTripError: the case carries something this format cannot
                express.
        """
        ...
