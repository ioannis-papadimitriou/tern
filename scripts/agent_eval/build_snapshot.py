"""The agent eval's world: a tern index and a Thunderbird profile of the synthetic mailbox.

Everything lives under one work dir (a fresh temp dir by default):

- ``home/``: ``TERN_HOME`` (index ``home/data/index.db``, audit log, state dir; the tb tools
  copy their snapshots into ``home/state/tb-snapshot``);
- ``profile/``: a synthetic Thunderbird profile (``global-messages-db.sqlite`` + ``prefs.js``)
  written by ``tern.testing.gloda.build_profile`` from the same ``FakeBridge`` the index was
  ingested from, so every ``mid`` a tb tool shows is a mid of the index (citations validate).

Read state is planted before the ingest (every copy read and unflagged, then a few unread and
one starred), so unread/starred questions have a defined answer. No calendar or address book is
built (``tern.testing.gloda``): the calendar tools see no calendars.

Isolation (:func:`guard_real_data`): ``tb_profile_dir`` is set explicitly (profile discovery
never runs), ``tern.tbmcp.config._candidate_roots`` is emptied as a second line, and a
``sys.addaudithook`` hook refuses every ``open`` under the user's real Thunderbird profiles,
thunderbird-mcp's default cache, tern's real data/state/config dirs and the private eval sets.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tern.bridge.protocol import (
    AccountList,
    Empty,
    FolderList,
    FoldersListParams,
    Page,
    PageParams,
)
from tern.config import TernConfig
from tern.index.store import Store, index_path
from tern.tbmcp import config as tbconfig
from tern.testing import smoke
from tern.testing.fake_bridge import FakeBridge
from tern.testing.gloda import GlodaProfile, build_profile

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_mailbox"

#: Header Message-IDs (no brackets) left unread; every other copy is read.
UNREAD = frozenset(
    {
        "msg-010-1.0c5400a0@example.net",  # acct1/INBOX "Piano lessons"
        "msg-037-1.7e4904bb@example.org",  # acct2/INBOX "Weekend hike"
        "msg-055-1.d86528ea@example.test",  # acct3/INBOX "Επισκευή αυτοκινήτου"
        "news-weekly-14.69ee8870@weekly.example.net",  # acct1/Newsletters
    }
)
#: Header Message-IDs starred.
FLAGGED = frozenset({"inv-0042@acme.example.com"})


class IsolationError(RuntimeError):
    """Something tried to open the user's real mail, tern data or private eval sets."""


def forbidden_roots(home: Path | None = None) -> tuple[Path, ...]:
    """Directories the eval must never open anything under."""
    h = Path.home() if home is None else home
    return (
        h / ".thunderbird",
        h / ".mozilla-thunderbird",
        h / "snap" / "thunderbird",
        h / ".var" / "app" / "org.mozilla.Thunderbird",
        h / ".cache" / "thunderbird-mcp",
        h / ".local" / "share" / "tern",
        h / ".local" / "state" / "tern",
        h / ".config" / "tern",
    )


_GUARD_INSTALLED = False
_ACTIVE = False
_FORBIDDEN: tuple[str, ...] = ()
_REAL_ROOTS = tbconfig._candidate_roots  # noqa: SLF001


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not _ACTIVE or event != "open" or not args:
        return
    target = args[0]
    if isinstance(target, int):  # an fd
        return
    try:
        path = os.path.abspath(os.fsdecode(target))
    except TypeError:
        return
    for root in _FORBIDDEN:
        if path == root or path.startswith(root + os.sep):
            raise IsolationError(f"agent eval refused to open {path}")


def guard_real_data() -> None:
    """Install the isolation guards (idempotent; module docstring)."""
    global _GUARD_INSTALLED, _ACTIVE, _FORBIDDEN
    tbconfig._candidate_roots = lambda: []  # noqa: SLF001 - discovery must never find a profile
    _FORBIDDEN = tuple(str(p) for p in forbidden_roots())
    _ACTIVE = True
    if not _GUARD_INSTALLED:
        sys.addaudithook(_audit)  # cannot be removed; release_guard() makes it inert
        _GUARD_INSTALLED = True


def release_guard() -> None:
    """Make the open hook inert again (tests: the hook outlives the eval in the process)."""
    global _ACTIVE
    _ACTIVE = False
    tbconfig._candidate_roots = _REAL_ROOTS  # noqa: SLF001


async def _plant(bridge: FakeBridge) -> None:
    """Every copy read and unflagged, then :data:`UNREAD` unread and :data:`FLAGGED` starred."""
    accts: AccountList = await bridge.request("mail.accounts.list", Empty())
    for account in accts.root:
        folders: FolderList = await bridge.request(
            "mail.folders.list", FoldersListParams(account_id=account.account_id)
        )
        for folder in folders.root:
            cursor: str | None = None
            while True:
                page: Page = await bridge.request(
                    "mail.messages.page", PageParams(folder_id=folder.folder_id, cursor=cursor)
                )
                for item in page.items:
                    hmid = item.header_message_id
                    await bridge.update(
                        item.tb_id, read=hmid not in UNREAD, flagged=hmid in FLAGGED
                    )
                cursor = page.next_cursor
                if cursor is None:
                    break


@dataclass(frozen=True)
class World:
    """The built eval world."""

    work: Path
    smoke_cfg: smoke.SmokeConfig
    index: Path
    profile: GlodaProfile
    bridge: FakeBridge

    def config(self, **overrides: Any) -> TernConfig:
        """The eval's ``TernConfig``: the smoke config (stub embedder id) with the synthetic
        profile, UTC, and ``overrides`` (chat URL, model, limits)."""
        base = self.smoke_cfg.config.model_dump()
        base.update(tb_profile_dir=self.profile.path, tb_timezone="UTC")
        base.update(overrides)
        return TernConfig.model_validate(base)

    def with_config(self, cfg: TernConfig) -> smoke.SmokeConfig:
        return dataclasses.replace(self.smoke_cfg, config=cfg)


def build_world(work: Path | None = None) -> World:
    """Ingest the synthetic mailbox (with stub embeddings) and write the synthetic profile under
    ``work`` (a fresh temp dir when None; must not exist yet or be empty)."""
    guard_real_data()
    if work is None:
        work = Path(tempfile.mkdtemp(prefix="tern-agent-eval-"))
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise RuntimeError(f"{work} is not empty")
    bridge = FakeBridge(FIXTURE)
    asyncio.run(_plant(bridge))
    cfg = smoke.smoke_config(FIXTURE, work / "home")
    ds = smoke.build_dataset(cfg, bridge=bridge)
    store = Store.open(ds.index_path)
    try:
        asyncio.run(smoke.embed_all(store, cfg.embedder, cfg.config))
    finally:
        store.close()
    store = Store.open(index_path(cfg.paths), readonly=True)
    try:
        profile = asyncio.run(build_profile(bridge, store, work / "profile"))
    finally:
        store.close()
    return World(work, cfg, ds.index_path, profile, bridge)


def main() -> int:
    """Build a world and print what it holds (a quick check of the fixture)."""
    world = build_world(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(
        json.dumps(
            {
                "work": str(world.work),
                "accounts": [dataclasses.asdict(a) for a in world.profile.accounts],
                "folders": [f.name for f in world.profile.folders],
                "messages": len(world.profile.messages),
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
