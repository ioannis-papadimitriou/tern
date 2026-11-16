"""Known-recipient lookups for recipient escalation (#7, architecture §6, plan 06b).

A tool that declares ``recipients(input)`` (``tern-tool/1``) is escalated to at least W2 when any
recipient is unknown. The engine asks a :class:`KnownRecipients` provider about each address
(lower-cased). Plan 10 supplies the real provider (personal address book and sent recipients) and
injects it at daemon start; until then ternd uses :class:`DenyAllRecipients`.

Error policy (plan 10's "never W1 on error"): a provider that cannot answer raises
:class:`RecipientsUnavailable`; the engine then treats every recipient of the call as unknown
(at least W2) and logs a WARNING with the count. Any other exception from a provider propagates
(``Registry.call`` reports it as ``internal``): only this declared failure type escalates.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class KnownRecipients(Protocol):
    """Answers whether an address counts as known. Async, because plan 10's provider asks the
    bridge (``contacts.lookup``). ``addr`` is lower-cased; the answer must be a ``bool``."""

    async def is_known(self, addr: str) -> bool: ...


class RecipientsUnavailable(Exception):
    """A provider cannot answer right now (plan 10: the bridge ``contacts.lookup`` failed or no
    extension is attached). The engine treats every recipient of the call as unknown. The message
    is never logged or audited (it may name an address)."""


class DenyAllRecipients:
    """The stub provider: every address is unknown, so every call with a declared recipient is at
    least W2. ternd uses it until plan 10 injects the real provider."""

    async def is_known(self, addr: str) -> bool:
        return False
