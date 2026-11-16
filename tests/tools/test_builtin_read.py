"""The R0/R1 built-in tools (plan 06a step 5), called only through ``Registry.call``.

Most cases run on the module's fixture index (``conftest.fixture_index``: the synthetic mailbox,
real ingest, stub-embedded). Expected values come from MANIFEST.json keys or from the index's own
rows read through the Store, never from a tool run. Small inline indexes (made-up words,
``example.com`` addresses) cover what the fixture lacks: NULL date/subject/sender, cleared
content, a failed parse, a conflicted thread member, a thread rooted at an unindexed message.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_fixture_manifest as tfm
from pydantic import BaseModel

from tern.bridge.protocol import BridgeError, Empty, Flags, FoundList, FoundLocation, Raw
from tern.index.mid import mid_of
from tern.index.store import LocationState, MessageRecord, Store
from tern.search import vec
from tern.search.embedder import EmbedderError, EmbedderUnavailable
from tern.search.service import SearchFilters, SearchStateError
from tern.testing.fake_bridge import FakeBridge
from tern.testing.stub_embedder import StubEmbedder
from tern.tools import ToolError
from tern.tools.builtin import BUILTINS
from tern.tools.builtin.read import READ_TOOLS, sender
from tern.tools.ids import short_id
from tern.tools.registry import ToolResult

from . import test_attachments
from .conftest import FIXTURE, STUB, EnvFactory, ToolEnv

MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
MID = {m["key"]: m["mid"] for m in MANIFEST["messages"]}
CONFLICTED = MID["inv-0077"]
CALLER = "agent:t1"


async def call(env: ToolEnv, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """The tool's output as the model sees it (aliases, nulls kept)."""
    res = await env.registry.call(CALLER, tool, args)
    assert isinstance(res, ToolResult)
    return res.output.model_dump(mode="json")


async def fails(env: ToolEnv, tool: str, args: dict[str, Any]) -> ToolError:
    with pytest.raises(ToolError) as info:
        await env.registry.call(CALLER, tool, args)
    return info.value


@pytest.fixture
def env(tool_env: EnvFactory, fixture_index: Path) -> ToolEnv:
    return tool_env(fixture_index, bridge=FakeBridge(FIXTURE))


# ---- inline indexes -----------------------------------------------------------------------------

INBOX = "acct1:/INBOX"
ARCHIVE = "acct1:/Archive"
OTHER = "acct1:/Other"


def mid_for(label: str) -> str:
    return mid_of(f"{label}@inline.example.com") or ""


def h(c: str) -> str:
    return c * 64


def rec(label: str, content: str, **kw: Any) -> MessageRecord:
    base: dict[str, Any] = {
        "parse_status": "ok",
        "header_message_id": f"{label}@inline.example.com",
    }
    base.update(kw)
    return MessageRecord(mid=mid_for(label), content_hash=h(content), **base)


def put(store: Store, r: MessageRecord, folder: str = INBOX, *, read: bool = False) -> None:
    with store.transaction():
        store.upsert_message(r)
        store.add_location(
            r.mid, folder, r.content_hash, LocationState(read=read), 0, source="backfill"
        )


@pytest.fixture
def inline(tmp_path: Path) -> Callable[[Callable[[Store], None]], Path]:
    def build(fill: Callable[[Store], None]) -> Path:
        path = tmp_path / "inline" / "index.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with Store.open(path) as store:
            fill(store)
        return path

    return build


# ---- registry wiring ----------------------------------------------------------------------------


def test_the_builtins_are_the_eight_read_tools() -> None:
    # BUILTINS also holds the thunderbird-mcp tools (tests/tools/test_builtin_tb.py)
    assert BUILTINS[: len(READ_TOOLS)] == READ_TOOLS
    assert sorted(s.name for s in READ_TOOLS) == [
        "find_contacts",
        "get_message",
        "get_thread",
        "index_status",
        "list_folders",
        "open_in_thunderbird",
        "read_attachment",
        "search_messages",
    ]


@pytest.mark.parametrize(
    ("name", "props"),
    [
        (
            "search_messages",
            ["after", "before", "folder", "from", "has_attachment", "limit", "query"],
        ),
        ("get_message", ["max_chars", "mid"]),
        ("get_thread", ["mid"]),
        ("list_folders", []),
        ("find_contacts", ["limit", "query"]),
        ("index_status", []),
        ("open_in_thunderbird", ["mid"]),
        ("read_attachment", ["max_chars", "mid", "name"]),
    ],
)
def test_inputs_follow_the_s3_stubs(name: str, props: list[str]) -> None:
    [spec] = [s for s in BUILTINS if s.name == name]
    schema = spec.input_model.model_json_schema()
    assert sorted(schema.get("properties", {})) == props


def test_input_limits_follow_the_s3_stubs() -> None:
    specs = {s.name: s for s in BUILTINS}
    search = specs["search_messages"].input_model.model_json_schema()["properties"]["limit"]
    assert (search["default"], search["maximum"]) == (10, 50)
    get = specs["get_message"].input_model.model_json_schema()["properties"]["max_chars"]
    assert (get["default"], get["maximum"]) == (4000, 20000)
    contacts = specs["find_contacts"].input_model.model_json_schema()["properties"]["limit"]
    assert (contacts["default"], contacts["maximum"]) == (20, 50)


@pytest.mark.asyncio
async def test_each_call_is_audited_with_its_target(env: ToolEnv) -> None:
    await call(env, "get_message", {"mid": MID["inv-0042"]})
    [row] = env.audit.list(limit=None)
    assert (row.tool, row.tier, row.decision, row.status) == ("get_message", "R0", "auto", "ok")
    assert row.target_mids == (MID["inv-0042"],)


# ---- search_messages ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_matches_the_service_without_conflicted(env: ToolEnv) -> None:
    out = await call(env, "search_messages", {"query": "Acme invoice March", "limit": 7})
    direct = await env.search.search(
        "Acme invoice March", SearchFilters(include_conflicted=False), 7
    )
    assert [hit["mid"] for hit in out["hits"]] == [hit.mid for hit in direct.hits]
    assert out["hits"], "the query must find fixture mail"
    assert (out["mode"], out["embed_pending"]) == ("hybrid", 0)
    first, hit0 = out["hits"][0], direct.hits[0]
    assert first["from"] == sender(hit0.from_name, hit0.from_addr)
    assert first["subject"] == hit0.subject
    assert first["folders"] == list(hit0.folder_ids)
    assert set(first) == {"mid", "date", "from", "subject", "snippet", "score", "folders"}


@pytest.mark.asyncio
async def test_search_drops_the_conflicted_inv_0077(env: ToolEnv) -> None:
    query = "invoice 0077"
    with_conflicted = await env.search.search(query, limit=50)
    assert CONFLICTED in {hit.mid for hit in with_conflicted.hits}  # the query does reach it
    out = await call(env, "search_messages", {"query": query, "limit": 50})
    assert out["hits"] and CONFLICTED not in {hit["mid"] for hit in out["hits"]}


@pytest.mark.asyncio
async def test_search_filters_reach_the_service(env: ToolEnv) -> None:
    args = {
        "query": "invoice",
        "folder": None,
        "from": "acme",
        "after": "2020-01-01",
        "before": "2100-01-01T00:00:00+02:00",
        "has_attachment": None,
        "limit": 50,
    }
    out = await call(env, "search_messages", args)
    filters = SearchFilters(
        from_="acme",
        after=datetime(2020, 1, 1, tzinfo=UTC),
        before=datetime(2099, 12, 31, 22, tzinfo=UTC),
        include_conflicted=False,
    )
    direct = await env.search.search("invoice", filters, 50)
    assert [hit["mid"] for hit in out["hits"]] == [hit.mid for hit in direct.hits]
    assert out["hits"]
    unfiltered = await call(env, "search_messages", {"query": "invoice", "limit": 50})
    assert len(unfiltered["hits"]) > len(out["hits"])  # the filters did restrict


@pytest.mark.asyncio
async def test_search_folder_and_attachment_filters(env: ToolEnv) -> None:
    [loc] = env.store.locations(MID["inv-0042"])
    out = await call(
        env, "search_messages", {"query": "invoice", "folder": loc.folder_id, "limit": 50}
    )
    assert out["hits"] and all(loc.folder_id in hit["folders"] for hit in out["hits"])
    for flag in (True, False):
        got = await call(
            env, "search_messages", {"query": "invoice", "has_attachment": flag, "limit": 50}
        )
        for hit in got["hits"]:
            row = env.store.get_message(hit["mid"])
            assert row is not None and row.has_attachments is flag


@pytest.mark.asyncio
async def test_search_accepts_from_by_name_too(env: ToolEnv) -> None:
    a = await call(env, "search_messages", {"query": "invoice", "from": "acme"})
    b = await call(env, "search_messages", {"query": "invoice", "from_": "acme"})
    assert a == b


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["after", "before"])
@pytest.mark.parametrize(
    "value",
    [
        "PLANTED-not-a-date",
        # parses, but the offset takes it out of datetime's range when made UTC (OverflowError)
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ],
)
async def test_search_bad_date_is_invalid_params(env: ToolEnv, field: str, value: str) -> None:
    err = await fails(env, "search_messages", {"query": "invoice", field: value})
    assert (err.code, err.reason) == ("invalid_params", "bad_date")
    assert value not in str(err) and field in str(err)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"query": "?!  --"}, "empty_query"),
        ({"query": "invoice", "from": " ́ "}, "bad_filter"),
        ({"query": "invoice", "folder": ""}, "bad_filter"),
    ],
)
async def test_search_refusals_are_invalid_params(
    env: ToolEnv, args: dict[str, Any], reason: str
) -> None:
    err = await fails(env, "search_messages", args)
    assert (err.code, err.reason) == ("invalid_params", reason)


@pytest.mark.asyncio
async def test_an_fts_service_is_labelled_fts(tool_env: EnvFactory, fixture_index: Path) -> None:
    env = tool_env(fixture_index, mode="fts")
    out = await call(env, "search_messages", {"query": "Acme invoice March"})
    assert out["mode"] == "fts" and out["hits"]


class _FailingEmbedder(StubEmbedder):
    def __init__(self, exc: Exception) -> None:
        super().__init__(dim=STUB.dim, seed=STUB.seed)
        self.exc = exc

    async def embed_query(self, text: str) -> list[float]:
        raise self.exc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (EmbedderUnavailable("connect_failed (model=stub)"), "embedder_unavailable"),
        (EmbedderError("bad_response (model=stub)"), "embedder_error"),
    ],
)
async def test_an_unreachable_embedder_is_unavailable(
    tool_env: EnvFactory, fixture_index: Path, exc: Exception, reason: str
) -> None:
    env = tool_env(fixture_index, embedder=_FailingEmbedder(exc))
    err = await fails(env, "search_messages", {"query": "invoice"})
    assert (err.code, err.reason) == ("unavailable", reason)
    [row] = env.audit.list(limit=None)
    assert row.status == "error"


@pytest.mark.asyncio
async def test_a_model_mismatch_is_unavailable(tool_env: EnvFactory, fixture_index: Path) -> None:
    env = tool_env(fixture_index, embedder=StubEmbedder(dim=64, seed=1))
    err = await fails(env, "search_messages", {"query": "invoice"})
    assert (err.code, err.reason) == ("unavailable", "model_mismatch")


@pytest.mark.asyncio
async def test_no_embedding_model_is_unavailable(
    tool_env: EnvFactory, inline: Callable[[Callable[[Store], None]], Path]
) -> None:
    path = inline(lambda s: put(s, rec("plain", "1", body_text="zebra words")))
    env = tool_env(path)
    err = await fails(env, "search_messages", {"query": "zebra"})
    assert (err.code, err.reason) == ("unavailable", "no_embedding_model")
    fts = await call(tool_env(path, mode="fts"), "search_messages", {"query": "zebra"})
    assert [hit["mid"] for hit in fts["hits"]] == [mid_for("plain")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "code", "reason"),
    [
        (SearchStateError("a chunk has no message row"), "internal", "index_state"),
        (vec.VecStateError("chunks_vec triggers are missing"), "internal", "index_state"),
        (sqlite3.OperationalError("database is locked"), "internal", "index_error"),
        # No bare ValueError is taken for a caller mistake (plan 05 note).
        (ValueError("PLANTED out of range"), "internal", "tool_failed"),
    ],
)
async def test_index_errors_in_search_are_internal(
    env: ToolEnv, monkeypatch: pytest.MonkeyPatch, exc: Exception, code: str, reason: str
) -> None:
    async def broken(*args: object, **kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(env.search, "search", broken)
    err = await fails(env, "search_messages", {"query": "invoice"})
    assert (err.code, err.reason) == (code, reason)
    assert "PLANTED" not in str(err)


class _FailingReads:
    """The index connection with every statement but the transaction ones raising ``exc``."""

    def __init__(self, conn: sqlite3.Connection, exc: Exception) -> None:
        self.conn, self.exc = conn, exc

    @property
    def in_transaction(self) -> bool:
        return self.conn.in_transaction

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if sql in ("BEGIN", "COMMIT", "ROLLBACK"):
            return self.conn.execute(sql)
        raise self.exc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "broken"),
    [
        ("get_message", {"mid": MID["inv-0042"]}, "get_message"),
        ("get_thread", {"mid": MID["inv-0042"]}, "get_message"),
        ("list_folders", {}, "folders"),
        ("find_contacts", {"query": "example"}, "connection"),
        ("index_status", {}, "status"),
        ("open_in_thunderbird", {"mid": MID["inv-0042"]}, "get_message"),
    ],
)
@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (sqlite3.OperationalError("PLANTED database is locked"), "index_error"),
        (SearchStateError("PLANTED a chunk has no message row"), "index_state"),
        (vec.VecStateError("PLANTED chunks_vec triggers are missing"), "index_state"),
    ],
)
async def test_index_errors_in_every_tool_are_internal(
    tool_env: EnvFactory,
    fixture_index: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    args: dict[str, Any],
    broken: str,
    exc: Exception,
    reason: str,
) -> None:
    fake = FakeBridge(FIXTURE)
    env = tool_env(fixture_index, bridge=fake)
    real = env.store.connection
    if broken == "connection":  # find_contacts reads through the snapshot's connection
        failing = _FailingReads(real, exc)
        monkeypatch.setattr(Store, "connection", property(lambda self: failing))
    else:

        def raising(*a: object, **kw: object) -> object:
            raise exc

        monkeypatch.setattr(env.store, broken, raising)
    err = await fails(env, tool, args)
    assert (err.code, err.reason) == ("internal", reason)
    assert "PLANTED" not in str(err)
    assert fake.calls == [] and not real.in_transaction
    [row] = env.audit.list(limit=None)
    assert row.status == "error"


# ---- get_message --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_message_reads_the_index_row(env: ToolEnv) -> None:
    mid = MID["inv-0042"]
    row = env.store.get_message(mid)
    assert row is not None and row.body_text
    out = await call(env, "get_message", {"mid": mid})
    assert out["mid"] == mid
    assert out["subject"] == row.subject and out["subject"] is not None
    assert out["from"] == sender(row.from_name, row.from_addr)
    assert (out["to"], out["cc"]) == (list(row.to_addrs), list(row.cc_addrs))
    assert out["date"] == datetime.fromtimestamp(row.date_utc or 0, UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert (out["body"], out["body_status"], out["truncated"]) == (row.body_text, "ok", False)
    assert out["folders"] == [loc.folder_id for loc in env.store.locations(mid)]
    assert out["thread_id"] == row.thread_id
    assert out["attachments"] == [
        {"name": a.filename, "mime": a.mime, "size": a.size} for a in row.attachments
    ]


@pytest.mark.asyncio
async def test_get_message_truncates_to_max_chars(env: ToolEnv) -> None:
    out = await call(env, "get_message", {"mid": MID["inv-0042"], "max_chars": 10})
    assert len(out["body"]) == 10 and out["truncated"] is True


@pytest.mark.asyncio
async def test_get_message_refuses_the_conflicted_inv_0077(env: ToolEnv) -> None:
    err = await fails(env, "get_message", {"mid": CONFLICTED})
    assert err.code == "mid_conflicted"


@pytest.mark.asyncio
async def test_get_message_unknown_and_malformed_mids(env: ToolEnv) -> None:
    err = await fails(env, "get_message", {"mid": "f" * 64})
    assert (err.code, err.reason) == ("unknown_mid", "not_indexed")
    err = await fails(env, "get_message", {"mid": "PLANTED-not-a-mid"})
    assert err.code == "invalid_params" and "PLANTED" not in str(err)
    err = await fails(env, "get_message", {"mid": MID["inv-0042"], "max_chars": 20001})
    assert err.code == "invalid_params"


def _null_fill(store: Store) -> None:
    put(store, rec("nulls", "1", body_text="first words"))
    put(store, rec("name-only", "2", from_name="Only Name", body_text="x"))
    put(store, rec("addr-only", "3", from_addr="only@example.com", body_text="x"))
    put(store, rec("both", "4", from_name="Both", from_addr="both@example.com", body_text="x"))
    put(store, rec("broken", "5", parse_status="failed", body_text=""))
    # Content cleared: the stored copy goes, the other copy differs (refill pending).
    put(store, rec("cleared", "6", subject="gone", body_text="old"), INBOX)
    put(store, rec("cleared", "7", subject="other", body_text="new"), ARCHIVE)
    store.remove_location(mid_for("cleared"), INBOX, source="event")


@pytest.mark.asyncio
async def test_nulls_stay_null_and_body_status_is_explicit(
    tool_env: EnvFactory, inline: Callable[[Callable[[Store], None]], Path]
) -> None:
    env = tool_env(inline(_null_fill), mode="fts")
    cleared = env.store.get_message(mid_for("cleared"))
    assert cleared is not None and cleared.content_hash is None and not cleared.conflicted

    out = await call(env, "get_message", {"mid": mid_for("nulls")})
    assert (out["date"], out["from"], out["subject"]) == (None, None, None)
    assert (out["body"], out["body_status"]) == ("first words", "ok")

    senders = {}
    for label in ("name-only", "addr-only", "both"):
        senders[label] = (await call(env, "get_message", {"mid": mid_for(label)}))["from"]
    assert senders == {
        "name-only": "Only Name",
        "addr-only": "only@example.com",
        "both": "Both <both@example.com>",
    }

    broken = await call(env, "get_message", {"mid": mid_for("broken")})
    assert (broken["body"], broken["body_status"], broken["truncated"]) == (
        None,
        "unparseable",
        False,
    )
    gone = await call(env, "get_message", {"mid": mid_for("cleared")})
    assert (gone["body"], gone["body_status"], gone["subject"]) == (None, "unavailable", None)


@pytest.mark.asyncio
async def test_an_out_of_range_stored_date_is_an_index_state_error(
    tool_env: EnvFactory, inline: Callable[[Callable[[Store], None]], Path]
) -> None:
    # ingest stores NULL for such dates; a stored one is index state, not the caller's mistake
    path = inline(lambda s: put(s, rec("far", "1", body_text="x", date_utc=10**15)))
    env = tool_env(path, mode="fts")
    for tool in ("get_message", "get_thread"):
        err = await fails(env, tool, {"mid": mid_for("far")})
        assert (err.code, err.reason) == ("internal", "index_state")


# ---- get_thread ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_lists_a_fixture_thread_oldest_first(env: ToolEnv) -> None:
    [thread] = [t for t in MANIFEST["threads"] if t["key"] == "t-filler-001"]
    members = {MID[k] for k in thread["members"]}
    out = await call(env, "get_thread", {"mid": MID[thread["root"]]})
    mids = [m["mid"] for m in out["messages"]]
    assert set(mids) == members
    assert mids == env.store.thread(MID[thread["root"]])  # oldest first, the Store's order
    dates = [m["date"] for m in out["messages"]]
    assert dates == sorted(dates)
    assert all(m["body_status"] == "ok" and m["snippet"] for m in out["messages"])


@pytest.mark.asyncio
async def test_get_thread_refuses_a_conflicted_anchor(env: ToolEnv) -> None:
    err = await fails(env, "get_thread", {"mid": CONFLICTED})
    assert err.code == "mid_conflicted"
    err = await fails(env, "get_thread", {"mid": "f" * 64})
    assert err.code == "unknown_mid"


def _thread_fill(store: Store) -> None:
    root_id = "root@inline.example.com"  # never stored: the thread's root is not indexed
    put(store, rec("reply-a", "1", in_reply_to=root_id, refs=(root_id,), date_utc=200))
    put(store, rec("reply-b", "2", in_reply_to=root_id, refs=(root_id,), date_utc=100))
    # A conflicted member: two differing copies in two folders.
    spoof = rec("reply-c", "3", in_reply_to=root_id, refs=(root_id,), date_utc=300)
    put(store, spoof, INBOX)
    put(store, rec("reply-c", "4", in_reply_to=root_id, refs=(root_id,), date_utc=300), OTHER)


@pytest.mark.asyncio
async def test_get_thread_rooted_at_an_unindexed_mid_drops_conflicted_members(
    tool_env: EnvFactory, inline: Callable[[Callable[[Store], None]], Path]
) -> None:
    env = tool_env(inline(_thread_fill), mode="fts")
    root = mid_of("root@inline.example.com")
    a, b, c = (env.store.get_message(mid_for(x)) for x in ("reply-a", "reply-b", "reply-c"))
    assert a is not None and b is not None and c is not None
    assert a.thread_id == b.thread_id == c.thread_id == root  # the unindexed root's mid
    assert env.store.get_message(root or "") is None and c.conflicted

    out = await call(env, "get_thread", {"mid": a.mid})
    assert [m["mid"] for m in out["messages"]] == [b.mid, a.mid]  # oldest first, c dropped


def _no_thread_fill(store: Store) -> None:
    put(store, rec("lone", "1", body_text="x"))
    with store.transaction():
        store.connection.execute("UPDATE messages SET thread_id = NULL")


@pytest.mark.asyncio
async def test_get_thread_of_an_anchor_without_thread_id_is_an_index_state_error(
    tool_env: EnvFactory, inline: Callable[[Callable[[Store], None]], Path]
) -> None:
    env = tool_env(inline(_no_thread_fill), mode="fts")
    row = env.store.get_message(mid_for("lone"))
    assert row is not None and row.thread_id is None
    assert env.store.thread(row.mid) == []  # a NULL thread_id matches nothing, not even itself
    err = await fails(env, "get_thread", {"mid": row.mid})
    assert (err.code, err.reason) == ("internal", "index_state")


# ---- list_folders -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_folders_from_the_index(env: ToolEnv) -> None:
    out = await call(env, "list_folders", {})
    folders = env.store.folders()
    assert [f["folder_id"] for f in out["folders"]] == [f.folder_id for f in folders]
    assert sum(f["total"] for f in out["folders"]) == env.store.status().locations
    names = {a["key"]: a["name"] for a in MANIFEST["accounts"]}
    by_account = {f["account_id"]: f["account"] for f in out["folders"]}
    assert set(by_account.values()) == set(names.values())
    for f in out["folders"]:
        locs = env.store.folder_locations(f["folder_id"])
        assert f["total"] == len(locs)
        assert f["unread"] == sum(1 for loc in locs if not loc.state.read)


@pytest.mark.asyncio
async def test_list_folders_needs_no_extension(tool_env: EnvFactory, fixture_index: Path) -> None:
    env = tool_env(fixture_index, bridge=None)
    out = await call(env, "list_folders", {})
    assert out["folders"]


# ---- find_contacts ------------------------------------------------------------------------------


def _contacts_fill(store: Store) -> None:
    put(store, rec("c1", "1", from_addr="maria.k@example.com", from_name="Maria Kara"))
    put(store, rec("c2", "2", from_addr="maria.k@example.com", from_name="Maria K"))
    put(store, rec("c3", "3", from_addr="maria.k@example.com", from_name="Maria Kara"))
    put(store, rec("c4", "4", from_addr="ops@example.com", to_addrs=("Maria.K@example.com",)))
    put(store, rec("c5", "5", from_addr="mario@example.com", from_name="Mario Kart"))
    # Only in a conflicted message: never counted.
    put(store, rec("c6", "6", from_addr="spoof@example.com", from_name="Maria Spoof"), INBOX)
    put(store, rec("c6", "7", from_addr="spoof@example.com", from_name="Maria Spoof"), OTHER)


@pytest.mark.asyncio
async def test_find_contacts_counts_non_conflicted_mail(
    tool_env: EnvFactory, inline: Callable[[Callable[[Store], None]], Path]
) -> None:
    env = tool_env(inline(_contacts_fill), mode="fts")
    out = await call(env, "find_contacts", {"query": "MARÍA"})
    assert out["contacts"] == [
        {"address": "maria.k@example.com", "name": "Maria Kara", "count": 4},
    ]
    both = await call(env, "find_contacts", {"query": "kar example"})
    assert [c["address"] for c in both["contacts"]] == [
        "maria.k@example.com",
        "mario@example.com",
    ]
    limited = await call(env, "find_contacts", {"query": "example", "limit": 2})
    assert [c["address"] for c in limited["contacts"]] == [
        "maria.k@example.com",
        "mario@example.com",
    ]
    every = await call(env, "find_contacts", {"query": "example", "limit": 50})
    assert "spoof@example.com" not in {c["address"] for c in every["contacts"]}
    assert {c["address"]: c["name"] for c in every["contacts"]}["ops@example.com"] is None


@pytest.mark.asyncio
async def test_find_contacts_on_the_fixture(env: ToolEnv) -> None:
    out = await call(env, "find_contacts", {"query": "example"})
    assert 0 < len(out["contacts"]) <= 20
    keys = [(-c["count"], c["address"]) for c in out["contacts"]]
    assert keys == sorted(keys)
    err = await fails(env, "find_contacts", {"query": "   "})
    assert (err.code, err.reason) == ("invalid_params", "empty_query")


def _tie_fill(store: Store) -> None:
    put(store, rec("t1", "1", from_addr="tie@example.com", from_name="Zora Tie"))
    put(store, rec("t2", "2", from_addr="tie@example.com", from_name="Abel Tie"))


@pytest.mark.asyncio
async def test_find_contacts_name_ties_go_to_the_smallest_name(
    tool_env: EnvFactory, inline: Callable[[Callable[[Store], None]], Path]
) -> None:
    env = tool_env(inline(_tie_fill), mode="fts")
    for query in ("zora", "abel", "tie"):  # either name finds the address; the name shown is one
        out = await call(env, "find_contacts", {"query": query})
        assert out["contacts"] == [{"address": "tie@example.com", "name": "Abel Tie", "count": 2}]


# ---- index_status -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_status_on_the_fixture(env: ToolEnv) -> None:
    out = await call(env, "index_status", {})
    st = env.store.status()
    assert (out["messages"], out["locations"], out["conflicted"]) == (
        st.messages,
        st.locations,
        st.conflicted,
    )
    assert out["conflicted"] >= 1  # inv-0077
    assert (out["embed_model"], out["embed_pending"], out["jobs_failed"]) == (STUB.model_id, 0, 0)


def _status_fill(store: Store) -> None:
    store.upsert_account("acct1", "someone@example.com", "imap")
    store.upsert_folder(INBOX, "acct1", "INBOX", ["inbox"])
    store.upsert_folder(ARCHIVE, "acct1", "Archive", [])
    put(store, rec("s1", "1", body_text="one"))
    store.set_folder_reconciled(INBOX, 1, 1_700_000_000)
    store.set_folder_reconciled(ARCHIVE, 0, 1_700_000_500)
    with store.transaction():
        store.connection.executemany(
            "INSERT INTO ingest_state(folder_id, status, updated_at) VALUES (?, ?, 0)",
            [(INBOX, "error:StoreError"), ("*", "error:BridgeError"), (ARCHIVE, "done")],
        )


@pytest.mark.asyncio
async def test_index_status_reports_failed_jobs_and_the_last_reconcile(
    tool_env: EnvFactory, inline: Callable[[Callable[[Store], None]], Path]
) -> None:
    env = tool_env(inline(_status_fill), mode="fts")
    out = await call(env, "index_status", {})
    assert out["jobs_failed"] == 2
    assert out["last_reconcile"] == "2023-11-14T22:21:40Z"
    assert (out["embed_model"], out["embed_pending"]) == (None, 1)  # no model: every chunk
    folders = await call(env, "list_folders", {})
    assert [
        (f["folder_id"], f["account"], f["total"], f["unread"]) for f in folders["folders"]
    ] == [
        (ARCHIVE, "someone@example.com", 0, 0),
        (INBOX, "someone@example.com", 1, 1),
    ]


# ---- open_in_thunderbird ------------------------------------------------------------------------


class Wrapped:
    """A ``FakeBridge`` whose ``mail.open`` answers ``stale_id`` the first ``stale`` times, and
    whose ``find`` answer can be replaced."""

    def __init__(
        self,
        fake: FakeBridge,
        *,
        stale: int = 0,
        found: BaseModel | None = None,
        opens: frozenset[int] = frozenset(),
    ) -> None:
        self.fake = fake
        self.stale = stale
        self.found = found
        #: tb_ids that ``mail.open`` accepts itself (planted ids FakeBridge does not know).
        self.opens = opens
        self.calls: list[str] = []
        self.opened: list[int] = []

    async def request(self, method: str, params: BaseModel) -> Any:
        self.calls.append(method)
        if method == "mail.open" and self.stale > 0:
            self.stale -= 1
            raise BridgeError("stale_id", "tb_id is not valid in this session")
        if method == "mail.messages.find" and self.found is not None:
            return self.found
        tb_id = getattr(params, "tb_id", None)
        if method == "mail.open" and tb_id in self.opens:
            self.opened.append(tb_id)
            return Empty()
        return await self.fake.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        raise AssertionError("tools never notify")


def _header_id(store: Store, mid: str) -> str:
    row = store.get_message(mid)
    assert row is not None and row.header_message_id is not None
    return row.header_message_id


@pytest.mark.asyncio
async def test_open_finds_then_opens_once_and_keeps_tb_id_out(
    tool_env: EnvFactory, fixture_index: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeBridge(FIXTURE)
    env = tool_env(fixture_index, bridge=fake)
    mid = MID["inv-0042"]
    [loc] = env.store.locations(mid)
    caplog.set_level(logging.DEBUG)
    res = await env.registry.call(CALLER, "open_in_thunderbird", {"mid": mid})
    assert res.output.model_dump(mode="json") == {"opened": True, "folder_id": loc.folder_id}
    assert res.untrusted_output is False
    assert [m for m, _ in fake.calls] == ["mail.messages.find", "mail.open"]
    assert fake.calls[0][1] == {"header_message_id": _header_id(env.store, mid)}
    tb_id = fake.calls[1][1]["tb_id"]
    assert tb_id in fake.tb_ids(loc.folder_id)
    # The tb_id is not in the audit row, nor in any log record.
    [row] = env.audit.list(limit=None)
    assert (row.args, row.target_mids, row.status) == ({"mid": mid}, (mid,), "ok")
    assert env.audit.events(row.call_id) == []
    assert all("tb_id" not in r.__dict__ for r in caplog.records)
    assert all("tb_id" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_open_picks_the_lowest_indexed_folder(
    tool_env: EnvFactory, fixture_index: Path
) -> None:
    fake = FakeBridge(FIXTURE)
    env = tool_env(fixture_index, bridge=fake)
    mid = MID["dup-1"]
    folders = [loc.folder_id for loc in env.store.locations(mid)]
    assert len(folders) == 2
    out = await call(env, "open_in_thunderbird", {"mid": mid})
    assert out["folder_id"] == min(folders)
    assert fake.calls[1][1]["tb_id"] in fake.tb_ids(min(folders))


@pytest.mark.asyncio
async def test_open_refuses_without_asking_the_extension(
    tool_env: EnvFactory, fixture_index: Path
) -> None:
    fake = FakeBridge(FIXTURE)
    env = tool_env(fixture_index, bridge=fake)
    err = await fails(env, "open_in_thunderbird", {"mid": CONFLICTED})
    assert err.code == "mid_conflicted"
    err = await fails(env, "open_in_thunderbird", {"mid": "f" * 64})
    assert (err.code, err.reason) == ("unknown_mid", "not_indexed")
    x_mid = env.store.connection.execute(
        "SELECT mid FROM messages WHERE mid LIKE 'x-%' AND conflicted = 0 ORDER BY mid LIMIT 1"
    ).fetchone()[0]
    err = await fails(env, "open_in_thunderbird", {"mid": x_mid})
    assert (err.code, err.reason) == ("unknown_mid", "no_message_id")
    assert fake.calls == []


@pytest.mark.asyncio
async def test_open_without_an_extension_is_unavailable(
    tool_env: EnvFactory, fixture_index: Path
) -> None:
    env = tool_env(fixture_index, bridge=None)
    err = await fails(env, "open_in_thunderbird", {"mid": MID["inv-0042"]})
    assert err.code == "unavailable"


@pytest.mark.asyncio
async def test_open_retries_a_stale_id_once(tool_env: EnvFactory, fixture_index: Path) -> None:
    once = Wrapped(FakeBridge(FIXTURE), stale=1)
    env = tool_env(fixture_index, bridge=once)
    out = await call(env, "open_in_thunderbird", {"mid": MID["inv-0042"]})
    assert out["opened"] is True
    assert once.calls == ["mail.messages.find", "mail.open"] * 2

    twice = Wrapped(FakeBridge(FIXTURE), stale=2)
    env = tool_env(fixture_index, bridge=twice)
    err = await fails(env, "open_in_thunderbird", {"mid": MID["inv-0042"]})
    assert (err.code, err.reason) == ("unavailable", "stale_id")
    assert twice.calls == ["mail.messages.find", "mail.open"] * 2


@pytest.mark.asyncio
async def test_open_when_tb_holds_it_only_in_an_unindexed_folder(
    tool_env: EnvFactory, fixture_index: Path
) -> None:
    elsewhere = FoundList(
        [
            FoundLocation(
                tb_id=5,
                folder_id="not-indexed",
                flags=Flags(read=True, flagged=False, junk=False),
                tags=[],
            )
        ]
    )
    bridge = Wrapped(FakeBridge(FIXTURE), found=elsewhere)
    env = tool_env(fixture_index, bridge=bridge)
    err = await fails(env, "open_in_thunderbird", {"mid": MID["inv-0042"]})
    assert (err.code, err.reason) == ("unknown_mid", "not_in_thunderbird")
    assert bridge.calls == ["mail.messages.find"]


#: A tb_id no log number would carry by chance (FakeBridge's own are small integers).
PLANTED_TB_ID = 730_419_861


@pytest.mark.asyncio
async def test_open_never_logs_or_audits_the_tb_id_value(
    tool_env: EnvFactory, fixture_index: Path, caplog: pytest.LogCaptureFixture
) -> None:
    probe = tool_env(fixture_index)
    mid = MID["inv-0042"]
    [loc] = probe.store.locations(mid)
    planted = FoundList(
        [
            FoundLocation(
                tb_id=PLANTED_TB_ID,
                folder_id=loc.folder_id,
                flags=Flags(read=True, flagged=False, junk=False),
                tags=[],
            )
        ]
    )
    bridge = Wrapped(FakeBridge(FIXTURE), found=planted, opens=frozenset({PLANTED_TB_ID}))
    env = tool_env(fixture_index, bridge=bridge)
    caplog.set_level(logging.DEBUG)
    out = await call(env, "open_in_thunderbird", {"mid": mid})
    assert out == {"opened": True, "folder_id": loc.folder_id}
    assert bridge.opened == [PLANTED_TB_ID]  # the planted id did reach mail.open
    needle = str(PLANTED_TB_ID)
    assert caplog.records
    for r in caplog.records:
        assert needle not in r.getMessage()
        assert all(needle not in repr(v) for v in r.__dict__.values())
    [row] = env.audit.list(limit=None)
    assert needle not in repr(row)
    assert needle not in repr(env.audit.events(row.call_id))


@pytest.mark.asyncio
async def test_open_with_a_find_result_that_is_not_a_list_is_internal(
    tool_env: EnvFactory, fixture_index: Path
) -> None:
    bridge = Wrapped(FakeBridge(FIXTURE), found=Empty())
    env = tool_env(fixture_index, bridge=bridge)
    err = await fails(env, "open_in_thunderbird", {"mid": MID["inv-0042"]})
    assert (err.code, err.reason) == ("internal", "bad_bridge_result")
    assert bridge.calls == ["mail.messages.find"]  # nothing was opened


# ---- reads never hold a transaction across an await ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_message", {"mid": MID["inv-0042"]}),
        ("get_thread", {"mid": MID["inv-0042"]}),
        ("list_folders", {}),
        ("find_contacts", {"query": "example"}),
        ("index_status", {}),
        ("open_in_thunderbird", {"mid": MID["inv-0042"]}),
        ("read_attachment", {"mid": MID["inv-0042"]}),
        ("search_messages", {"query": "invoice"}),
    ],
)
async def test_no_transaction_is_left_open(env: ToolEnv, tool: str, args: dict[str, Any]) -> None:
    await call(env, tool, args)
    assert not env.store.connection.in_transaction


# ---- read_attachment ----------------------------------------------------------------------------


class RawOverride:
    """A ``FakeBridge`` whose ``mail.messages.raw`` answers ``raw`` (a planted message), and
    ``stale_id`` the first ``stale`` times."""

    def __init__(self, fake: FakeBridge, raw: bytes | None = None, *, stale: int = 0) -> None:
        self.fake = fake
        self.raw = raw
        self.stale = stale
        self.calls: list[str] = []

    async def request(self, method: str, params: BaseModel) -> Any:
        self.calls.append(method)
        if method == "mail.messages.raw":
            if self.stale > 0:
                self.stale -= 1
                raise BridgeError("stale_id", "tb_id is not valid in this session")
            if self.raw is not None:
                return Raw(raw_b64=base64.b64encode(self.raw).decode())
        return await self.fake.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        raise AssertionError("tools never notify")


@pytest.mark.asyncio
async def test_read_attachment_lists_the_fixture_pdf_by_short_id(
    tool_env: EnvFactory, fixture_index: Path
) -> None:
    fake = FakeBridge(FIXTURE)
    env = tool_env(fixture_index, bridge=fake)
    mid = MID["inv-0042"]
    res = await env.registry.call(CALLER, "read_attachment", {"mid": short_id(mid)})
    out = res.output.model_dump(mode="json")
    assert res.untrusted_output is True
    assert [(a["name"], a["mime"]) for a in out["attachments"]] == [
        ("INV-0042.pdf", "application/pdf")
    ]
    assert out["name"] == "INV-0042.pdf"  # the only attachment: no name needed
    # the fixture's PDF is a placeholder with no pages: nothing invented
    assert out["status"] in {"empty", "unreadable"} and out["text"] is None
    assert [m for m, _ in fake.calls] == ["mail.messages.find", "mail.messages.raw"]
    [row] = env.audit.list(limit=None)
    # the id is audited as given (a short id is not a Mid-typed target field)
    assert row.args is not None
    assert (row.tool, row.args["mid"], row.status) == ("read_attachment", short_id(mid), "ok")


@pytest.mark.asyncio
async def test_read_attachment_reads_pdf_text(tool_env: EnvFactory, fixture_index: Path) -> None:
    raw = test_attachments.message(
        ("bill.pdf", "application/pdf", test_attachments.pdf("Lantern total 42 coins")),
        ("notes.txt", "text/plain", b"bring the ladder"),
    )
    env = tool_env(fixture_index, bridge=RawOverride(FakeBridge(FIXTURE), raw))
    out = await call(env, "read_attachment", {"mid": MID["inv-0042"], "name": "bill.pdf"})
    assert out["status"] == "ok" and "Lantern total 42 coins" in out["text"]
    assert [a["name"] for a in out["attachments"]] == ["bill.pdf", "notes.txt"]
    txt = await call(env, "read_attachment", {"mid": MID["inv-0042"], "name": "notes"})
    assert (txt["name"], txt["text"]) == ("notes.txt", "bring the ladder")


@pytest.mark.asyncio
async def test_read_attachment_without_a_matching_name_lists_them(
    tool_env: EnvFactory, fixture_index: Path
) -> None:
    raw = test_attachments.message(("a.txt", "text/plain", b"one"), ("b.txt", "text/plain", b"two"))
    env = tool_env(fixture_index, bridge=RawOverride(FakeBridge(FIXTURE), raw))
    for args in ({}, {"name": "missing.pdf"}):
        out = await call(env, "read_attachment", {"mid": MID["inv-0042"], **args})
        assert (out["status"], out["name"], out["text"]) == ("not_found", None, None)
        assert [a["name"] for a in out["attachments"]] == ["a.txt", "b.txt"]
        assert "give one of the listed names" in out["detail"]


@pytest.mark.asyncio
async def test_read_attachment_refusals(tool_env: EnvFactory, fixture_index: Path) -> None:
    fake = FakeBridge(FIXTURE)
    env = tool_env(fixture_index, bridge=fake)
    err = await fails(env, "read_attachment", {"mid": CONFLICTED})
    assert err.code == "mid_conflicted"
    err = await fails(env, "read_attachment", {"mid": "f" * 64})
    assert (err.code, err.reason) == ("unknown_mid", "not_indexed")
    err = await fails(env, "read_attachment", {"mid": "not-a-mid!"})
    assert (err.code, err.reason) == ("invalid_params", "bad_mid")
    assert fake.calls == []
    err = await fails(
        tool_env(fixture_index, bridge=None), "read_attachment", {"mid": MID["inv-0042"]}
    )
    assert err.code == "unavailable"


@pytest.mark.asyncio
async def test_read_attachment_retries_a_stale_id_once(
    tool_env: EnvFactory, fixture_index: Path
) -> None:
    raw = test_attachments.message(("a.txt", "text/plain", b"one"))
    once = RawOverride(FakeBridge(FIXTURE), raw, stale=1)
    out = await call(
        tool_env(fixture_index, bridge=once), "read_attachment", {"mid": MID["inv-0042"]}
    )
    assert out["text"] == "one"
    assert once.calls == ["mail.messages.find", "mail.messages.raw"] * 2
    twice = RawOverride(FakeBridge(FIXTURE), raw, stale=2)
    err = await fails(
        tool_env(fixture_index, bridge=twice), "read_attachment", {"mid": MID["inv-0042"]}
    )
    assert (err.code, err.reason) == ("unavailable", "stale_id")
