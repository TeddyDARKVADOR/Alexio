"""
core/ai/errors.py — what can go wrong, named.

Before the gateway, every module invented its own handling: some caught bare
Exception and returned a sentence, some let the SDK's error escape into the
receive loop. A caller could not tell "the key is wrong" from "you are over
quota" from "the model said nothing", which is why quota cooldowns had to be
re-implemented in actions/web_search.py instead of living in one place.
"""

from __future__ import annotations


class AIError(RuntimeError):
    """Base class. Catching this catches everything the gateway raises."""


class ProviderUnavailable(AIError):
    """The provider could not be reached, or refused the credentials.

    Distinct from a bad request: retrying later may work, retrying now will not.
    """


class QuotaExceeded(AIError):
    """Rate-limited or out of quota. The caller should degrade, not retry hard."""


class EmptyResponse(AIError):
    """The call succeeded and the model returned nothing usable.

    Its own class because it is the one failure that is not an error anywhere in
    the stack — no exception, no bad status — and so was routinely mistaken for
    a successful empty answer and spoken aloud as silence.
    """


class CapabilityUnavailable(AIError):
    """The request needs something this provider cannot do — looking at an
    image, or grounding an answer in a live web search."""
