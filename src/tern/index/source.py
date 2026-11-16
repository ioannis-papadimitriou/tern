"""``IngestSource``: the ingest jobs' view of the extension (plan 04 step 7, #16).

A thin typed wrapper over any ``ExtensionRequester`` (``ExtensionLink`` in ternd, ``FakeBridge``
in tests): one method per bridge request the ingest needs, each returning the validated 1.0
model. Nothing is cached and no TB id or cursor is kept here (#5): ``tb_id``s and cursors are
session-scoped and live only as long as the caller holds them.

Errors are the bridge's own ``BridgeError``s, passed through unchanged (``raw`` raises
``BridgeError('too_large')`` above ``RAW_MAX``; any call may raise ``stale_id``); a result of the
wrong type, or raw bytes that are not base64, raise ``ProtocolViolation``.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from tern.bridge.client import ExtensionRequester, ProtocolViolation
from tern.bridge.protocol import (
    Account,
    AccountList,
    Empty,
    FindParams,
    Folder,
    FolderList,
    FoldersListParams,
    FoundList,
    FoundLocation,
    FullMessage,
    Page,
    PageParams,
    Raw,
    TbIdParams,
)


def _expect[M: BaseModel](result: Any, model: type[M], method: str) -> M:
    if not isinstance(result, model):
        raise ProtocolViolation(f"{method} returned {type(result).__name__}, not {model.__name__}")
    return result


class IngestSource:
    """Typed bridge calls for the ingest jobs."""

    def __init__(self, requester: ExtensionRequester) -> None:
        self.requester = requester

    async def _call[M: BaseModel](self, method: str, params: BaseModel, model: type[M]) -> M:
        return _expect(await self.requester.request(method, params), model, method)

    async def accounts(self) -> list[Account]:
        return list((await self._call("mail.accounts.list", Empty(), AccountList)).root)

    async def folders(self, account_id: str) -> list[Folder]:
        params = FoldersListParams(account_id=account_id)
        return list((await self._call("mail.folders.list", params, FolderList)).root)

    async def page(
        self,
        folder_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        cursor: str | None = None,
    ) -> Page:
        """One page of ``folder_id`` filtered by the inclusive ``[from_date, to_date]``; with a
        ``cursor`` the dates are not sent (the cursor carries its query)."""
        if cursor is not None:
            params = PageParams(folder_id=folder_id, cursor=cursor)
        else:
            params = PageParams(folder_id=folder_id, from_date=from_date, to_date=to_date)
        return await self._call("mail.messages.page", params, Page)

    async def raw(self, tb_id: int) -> bytes:
        """The message's getRaw bytes; ``BridgeError('too_large')`` above ``RAW_MAX``."""
        result = await self._call("mail.messages.raw", TbIdParams(tb_id=tb_id), Raw)
        try:
            return base64.b64decode(result.raw_b64, validate=True)
        except binascii.Error as exc:
            raise ProtocolViolation("mail.messages.raw returned bytes that are not base64") from exc

    async def full(self, tb_id: int) -> FullMessage:
        return await self._call("mail.messages.full", TbIdParams(tb_id=tb_id), FullMessage)

    async def find(self, header_message_id: str) -> list[FoundLocation]:
        """Every current location of TB's ``headerMessageId`` (fresh ``tb_id``s)."""
        params = FindParams(header_message_id=header_message_id)
        return list((await self._call("mail.messages.find", params, FoundList)).root)
