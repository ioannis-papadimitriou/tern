"""Smoke helpers behind ``tests/smoke/adapter.py`` (plan 05 step 8).

The smoke contracts' module-scoped fixtures run before the autouse ``TERN_HOME`` fixture of
``tests/conftest.py``, so nothing here reads the environment: no ``paths.resolve()`` without an
explicit env and no ``config.load()``. :class:`SmokeConfig` carries the ``TernPaths`` resolved from
``{"TERN_HOME": <temp dir>}`` and a ``TernConfig`` built in code; every index lives under that
temp dir or the ``out_dir`` the caller passes.

Everything is real tern code (``Backfill`` over ``FakeBridge``, ``EmbedQueue`` with the
``StubEmbedder``, ``SearchService`` in hybrid mode) on the synthetic mailbox only. Failures raise
(:class:`SmokeError` or the underlying error); nothing is defaulted.

:func:`api_client` (plan 07 step 4) is the MCP contract: the real MCP server
(``build_server(RegistryBackend(...))``) over the real ``Registry`` + permission ``Engine`` + a
temp ``AuditLog`` + ``FakeBridge``, driven by the SDK's in-memory ``Client``, on its own fresh
temp index. Every request checks that exactly one new audit row was written for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from mcp.client import Client
from mcp.types import CallToolResult, Implementation

import tern
from tern import paths
from tern.agent.citations import CONFLICTED_MARKER, REMOVED_MARKER, find_citations
from tern.agent.loop import AgentSession, AgentTurn
from tern.config import TernConfig
from tern.index.ingest import STATUS_DONE, Backfill, BatchReport, IngestReport
from tern.index.source import IngestSource
from tern.index.store import INDEX_NAME, Store, index_path
from tern.llm.client import LLMClient
from tern.mcp.local import RegistryBackend
from tern.mcp.server import build_server
from tern.paths import TernPaths
from tern.perms.audit import AuditLog, audit_path
from tern.perms.engine import Engine
from tern.perms.recipients import DenyAllRecipients
from tern.search import vec
from tern.search.embed_queue import EmbedQueue
from tern.search.embedder import Embedder
from tern.search.service import SearchService
from tern.testing.fake_bridge import FakeBridge
from tern.testing.fake_llm import FakeLLM, FakeLLMScriptError, FakeScript, load_script
from tern.testing.stub_embedder import StubEmbedder
from tern.tools.registry import Registry, ToolRuntime, builtin_tools

#: The three fixed smoke queries (adapter ``smoke_inputs``). The German one has no fixture mail
#: (fixture languages en/el, A-22 placeholder), so its hits are vector-only by design.
SMOKE_QUERIES: tuple[str, ...] = (
    "Acme invoice March",
    "Rechnung Hausverwaltung",
    "συνάντηση Πέμπτη",
)
#: Hits per query returned by :func:`predict`.
PREDICT_K = 5
INGEST_BATCH_SIZE = 50
STUB_DIM = 64
#: The smoke MCP session's caller id and client name (``client_info.name`` -> ``caller_label``).
API_CALLER = "mcp:smoke"
API_CLIENT_NAME = "tern-smoke"
#: The only ``api_client`` request method: call the tool named by ``path``.
API_METHOD = "TOOL"
#: Tool-error codes :func:`api_status` maps to 403 (refused by the permission engine or the
#: user); ``invalid_params`` is 422, success 200, every other code 500.
API_FORBIDDEN_CODES: frozenset[str] = frozenset(
    {"denied", "expired", "rate_limited", "tier_not_enabled", "targets_changed", "state_changed"}
)


class SmokeError(RuntimeError):
    """A smoke step produced an inconsistent index (loud; never a defaulted value)."""


@dataclass(frozen=True)
class SmokeConfig:
    """Everything the smoke run needs, without reading the environment."""

    mailbox: Path
    tern_home: Path
    paths: TernPaths
    config: TernConfig
    embedder: StubEmbedder
    ingest_batch_size: int = INGEST_BATCH_SIZE
    seed: int = 0
    #: Keeps a self-made temp dir alive as long as the config (removed when it is collected or
    #: at interpreter exit); None when the caller passed the root.
    _tmp: tempfile.TemporaryDirectory[str] | None = field(default=None, repr=False, compare=False)

    @property
    def distinct_messages(self) -> int:
        """The fixture manifest's ``distinct_messages``."""
        manifest = json.loads((self.mailbox / "MANIFEST.json").read_text(encoding="utf-8"))
        n = manifest["distinct_messages"]
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise SmokeError("MANIFEST.json distinct_messages is not a positive int")
        return n


class TinyDataset(NamedTuple):
    """The handle :func:`build_dataset` returns: ``(index_path, ingest_report)``."""

    index_path: Path
    report: IngestReport


def smoke_config(mailbox: Path, root: Path | None = None, *, seed: int = 0) -> SmokeConfig:
    """A :class:`SmokeConfig` for ``mailbox`` with ``TERN_HOME`` = ``root`` (a fresh temp dir
    when None). ``root`` must be absolute (as ``TERN_HOME`` must be)."""
    if not (mailbox / "MANIFEST.json").is_file():
        raise SmokeError("mailbox has no MANIFEST.json")
    tmp: tempfile.TemporaryDirectory[str] | None = None
    if root is None:
        tmp = tempfile.TemporaryDirectory(prefix="tern-smoke-")
        root = Path(tmp.name)
    tern_paths = paths.resolve({"TERN_HOME": str(root)})
    embedder = StubEmbedder(dim=STUB_DIM, seed=seed)
    # Embedding on, with the stub's id (EmbedQueue refuses a mismatch); nothing else differs
    # from the defaults. No network: the stub is the only embedder the helpers use.
    config = TernConfig(embed_model=embedder.model_id)
    return SmokeConfig(
        mailbox=mailbox,
        tern_home=root,
        paths=tern_paths,
        config=config,
        embedder=embedder,
        seed=seed,
        _tmp=tmp,
    )


def _backfill(
    cfg: SmokeConfig,
    store: Store,
    on_batch: Callable[[BatchReport], None] | None = None,
    *,
    bridge: FakeBridge | None = None,
) -> IngestReport:
    backfill = Backfill(
        store,
        IngestSource(FakeBridge(cfg.mailbox) if bridge is None else bridge),
        cfg.config,
        batch_size=cfg.ingest_batch_size,
    )
    return asyncio.run(backfill.run(on_batch=on_batch))


def _message_count(store: Store) -> int:
    return int(store.connection.execute("SELECT count(*) FROM messages").fetchone()[0])


def _check_ingest_done(store: Store, report: IngestReport) -> None:
    rows = store.connection.execute("SELECT status FROM ingest_state").fetchall()
    statuses = [r[0] for r in rows]
    if (
        report.folders_total < 1
        or report.folders_done != report.folders_total
        or len(statuses) != report.folders_total
        or any(s != STATUS_DONE for s in statuses)
    ):
        raise SmokeError(
            f"backfill not complete: folders_done={report.folders_done} "
            f"folders_total={report.folders_total} ingest_state_rows={len(statuses)} "
            f"not_done={sum(s != STATUS_DONE for s in statuses)}"
        )


def build_dataset(cfg: SmokeConfig, *, bridge: FakeBridge | None = None) -> TinyDataset:
    """The real ingest of ``cfg.mailbox`` into ``<tern_home>/data/index.db`` (a fresh index).
    ``bridge``: the ``FakeBridge`` to ingest from (default: a fresh one over ``cfg.mailbox``),
    e.g. one with a planted read/flagged state (``scripts/chat_probe.py``)."""
    path = index_path(cfg.paths)
    if path.exists():
        raise SmokeError("the smoke index already exists; use a fresh tern_home")
    store = Store.open(path)
    try:
        report = _backfill(cfg, store, bridge=bridge)
        _check_ingest_done(store, report)
    finally:
        store.close()
    return TinyDataset(path, report)


def dataset_size(ds: TinyDataset) -> int:
    """Distinct mids in the index :func:`build_dataset` built (read-only)."""
    store = Store.open(ds.index_path, readonly=True)
    try:
        return _message_count(store)
    finally:
        store.close()


class _Curve:
    """``on_batch`` callback: 1 − count(messages) / distinct_messages, read from the persisted
    index through a separate read-only connection after every batch."""

    def __init__(self, reader: Store, distinct: int) -> None:
        self.reader = reader
        self.distinct = distinct
        self.points: list[float] = []
        self.counts: list[int] = []

    def measure(self) -> None:
        n = _message_count(self.reader)
        self.counts.append(n)
        self.points.append(1.0 - n / self.distinct)

    def __call__(self, _report: BatchReport) -> None:
        self.measure()


def train(cfg: SmokeConfig, ds: TinyDataset, out_dir: Path) -> list[float]:
    """Build the full index (real Backfill, then ``EmbedQueue.drain()`` with the stub) into
    ``out_dir/index.db`` and return the ingest-progress curve (first point before the first
    batch, one per ``BatchReport``). Raises :class:`SmokeError` unless the curve ends at 0.0,
    ``ingest_state`` is all ``done``, the index holds as many messages as ``ds``, and every chunk
    has a vector of the stub's model (its id and dim)."""
    path = out_dir / INDEX_NAME
    if path.exists():
        raise SmokeError("out_dir already holds an index")
    distinct = cfg.distinct_messages
    store = Store.open(path)
    try:
        reader = Store.open(path, readonly=True)
        try:
            curve = _Curve(reader, distinct)
            curve.measure()
            report = _backfill(cfg, store, curve)
        finally:
            reader.close()
        _check_ingest_done(store, report)
        if curve.counts[-1] != distinct:
            raise SmokeError(
                f"index holds {curve.counts[-1]} messages, the manifest {distinct} distinct ones"
            )
        if curve.counts[-1] != dataset_size(ds):
            raise SmokeError("the trained index and the dataset index differ in message count")
        _embed_all(cfg, store)
    finally:
        store.close()
    return curve.points


async def embed_all(store: Store, embedder: Embedder, config: TernConfig) -> vec.ModelInfo:
    """``EmbedQueue.drain()`` with ``embedder`` on a writable ``store``; returns the registered
    model. Raises :class:`SmokeError` unless every chunk then has a vector of ``embedder``'s
    model id; embedder failures (``EmbedderUnavailable``, ``EmbedderError``) propagate."""
    await EmbedQueue(store, embedder, config).drain()
    model = vec.current(store.connection)
    pending = vec.pending_count(store.connection)
    if model is None:
        raise SmokeError(f"embedding incomplete: no model registered, pending={pending}")
    if model.model_id != embedder.model_id:
        raise SmokeError(
            f"embedding model mismatch: registered {model.model_id!r}, "
            f"the embedder {embedder.model_id!r}"
        )
    if pending != 0:
        raise SmokeError(f"embedding incomplete: pending={pending}")
    return model


def _embed_all(cfg: SmokeConfig, store: Store) -> None:
    """:func:`embed_all` with the stub; raises :class:`SmokeError` unless every chunk then has
    a vector of the stub's model (its id and dim)."""
    model = asyncio.run(embed_all(store, cfg.embedder, cfg.config))
    if model.model_id != cfg.embedder.model_id or model.dim != cfg.embedder.dim:
        raise SmokeError(
            f"embedding model mismatch: registered ({model.model_id}, dim {model.dim}), "
            f"the stub ({cfg.embedder.model_id}, dim {cfg.embedder.dim})"
        )


def predict(
    cfg: SmokeConfig, checkpoint_dir: Path, queries: Sequence[str], k: int = PREDICT_K
) -> list[tuple[str, ...]]:
    """Per query, the top ``k`` mids of a hybrid search over ``checkpoint_dir/index.db``, opened
    with fresh objects (a new read-only Store and SearchService)."""
    if isinstance(queries, str):
        raise TypeError("queries must be a sequence of strings, not a string")
    store = Store.open(checkpoint_dir / INDEX_NAME, readonly=True)
    try:
        service = SearchService(store, cfg.embedder, mode="hybrid")

        async def run() -> list[tuple[str, ...]]:
            out: list[tuple[str, ...]] = []
            for query in queries:
                result = await service.search(query, limit=k)
                if result.mode != "hybrid" or result.embed_pending != 0:
                    raise SmokeError(
                        f"search not fully hybrid: mode={result.mode} "
                        f"embed_pending={result.embed_pending}"
                    )
                out.append(tuple(hit.mid for hit in result.hits))
            return out

        return asyncio.run(run())
    finally:
        store.close()


def _registry(
    cfg: SmokeConfig,
    store: Store,
    audit: AuditLog,
    *,
    search: SearchService | None = None,
    bridge: FakeBridge | None = None,
) -> Registry:
    """The built-ins over a real ``Engine`` (no approval notifier: W2/W3 are ``unavailable``, as
    in ternd before plan 12) and ``FakeBridge``; hybrid search with the stub embedder. Shared by
    the MCP contract, the agent reference case and ``scripts/chat_probe.py``, which passes its
    own ``search`` (FTS-only or the real embedder) and ``bridge`` (its planted flag state)."""
    if bridge is None:
        bridge = FakeBridge(cfg.mailbox)
    engine = Engine(
        audit=audit,
        index=store,
        bridge=bridge,
        clock=time.time,
        known_recipients=DenyAllRecipients(),
    )
    runtime = ToolRuntime(
        audit=audit,
        index=store,
        search=SearchService(store, cfg.embedder, mode="hybrid") if search is None else search,
        bridge=bridge,
        config=cfg.config,
        log=logging.getLogger("tern.tools"),
        clock=time.time,
        engine=engine,
        state_dir=cfg.paths.state,
    )
    return Registry(builtin_tools(), runtime=runtime)


# ---- plan 07 step 4: the MCP contract (adapter api_client / api_smoke_request) -------------


@dataclass(frozen=True)
class ApiResponse:
    """One :meth:`ApiClient.request` result: an HTTP-like status and the result's text."""

    status_code: int
    text: str


def _result_text(result: CallToolResult) -> str:
    texts: list[str] = []
    for item in result.content:
        if item.type != "text":
            raise SmokeError(f"MCP result holds a {item.type!r} content block, not text")
        texts.append(item.text)
    return "".join(texts)


def api_status(result: CallToolResult) -> int:
    """The status of one MCP tool result: success 200; an ``is_error`` result by its
    ``{"error": {"code"}}`` text: ``invalid_params`` 422, a :data:`API_FORBIDDEN_CODES` code
    403, any other code 500. An error result without that body is a :class:`SmokeError` (the
    server's error shape broke), never a guessed status."""
    if not result.is_error:
        return 200
    try:
        body = json.loads(_result_text(result))
    except json.JSONDecodeError:
        raise SmokeError("MCP error result is not JSON") from None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str):
        raise SmokeError('MCP error result has no {"error": {"code": str}} body')
    if code == "invalid_params":
        return 422
    if code in API_FORBIDDEN_CODES:
        return 403
    return 500


class ApiClient:
    """The adapter's ``api_client`` wrapper: ``request("TOOL", name, args)`` runs one MCP
    session (the SDK's in-memory ``Client`` against ``build_server(RegistryBackend(registry,
    API_CALLER))``) that calls tool ``name``. Per request the index is opened read-only and
    the audit log at ``<tern_home>/state/audit.db`` is opened, both closed again afterwards
    (the log persists across requests, so the engine's rate windows see earlier calls)."""

    def __init__(self, cfg: SmokeConfig, index: Path) -> None:
        if not index.is_file():
            raise SmokeError("the api_client index does not exist")
        self.cfg = cfg
        self.index = index

    @property
    def audit_path(self) -> Path:
        return audit_path(self.cfg.paths)

    def request(self, method: str, path: str, json: Mapping[str, Any] | None = None) -> ApiResponse:
        """Call tool ``path`` with arguments ``json`` (None: no arguments) and map the result
        with :func:`api_status`. Raises :class:`SmokeError` unless exactly one new audit row,
        with caller :data:`API_CALLER`, tool ``path`` and caller label
        :data:`API_CLIENT_NAME`, was written for the call (proves it went through
        ``Registry.call``)."""
        if method != API_METHOD:
            raise SmokeError(f"api_client supports only method {API_METHOD!r}")
        args = None if json is None else dict(json)
        return asyncio.run(self._request(path, args))

    async def _request(self, tool: str, args: dict[str, Any] | None) -> ApiResponse:
        store = Store.open(self.index, readonly=True)
        try:
            with AuditLog.open(self.audit_path) as audit:
                server = build_server(
                    RegistryBackend(_registry(self.cfg, store, audit), API_CALLER)
                )
                before = audit.count()
                info = Implementation(name=API_CLIENT_NAME, version=tern.__version__)
                async with Client(server, client_info=info) as client:
                    result = await client.call_tool(tool, args)
                written = audit.count() - before
                if written != 1:
                    raise SmokeError(f"{written} audit rows written for one call, expected 1")
                [row] = audit.list(limit=1)
                if (row.caller, row.tool, row.caller_label) != (API_CALLER, tool, API_CLIENT_NAME):
                    raise SmokeError(
                        "the call's audit row does not name the smoke caller, tool and client"
                    )
        finally:
            store.close()
        return ApiResponse(api_status(result), _result_text(result))


def api_client(mailbox: Path, root: Path | None = None) -> ApiClient:
    """An :class:`ApiClient` over a fresh index of ``mailbox`` under ``root`` (a fresh temp dir
    when None): the real ingest (:func:`build_dataset`) plus every chunk embedded with the stub,
    so ``search_messages`` runs in hybrid mode."""
    cfg = smoke_config(mailbox, root)
    ds = build_dataset(cfg)
    store = Store.open(ds.index_path)
    try:
        _embed_all(cfg, store)
    finally:
        store.close()
    return ApiClient(cfg, ds.index_path)


# ---- plan 08a step 5: the agent reference case (adapter reference_case) --------------------

#: The reference case's agent session key; its tool calls are audited as ``agent:smoke``.
REFERENCE_SESSION_KEY = "smoke"
REFERENCE_CALLER = f"agent:{REFERENCE_SESSION_KEY}"


@dataclass(frozen=True)
class ReferenceIndex:
    """A fresh index of the synthetic mailbox for :func:`run_reference` (every chunk embedded
    with the stub, so ``search_messages`` runs in hybrid mode)."""

    cfg: SmokeConfig
    index: Path


@dataclass(frozen=True)
class ReferenceResult:
    """One reference turn: ``computed`` = ``|citations ∩ expected| / |expected|`` (after the
    agent's citation validation), the finished turn and the deltas it streamed."""

    computed: float
    turn: AgentTurn
    deltas: tuple[str, ...]


def reference_index(mailbox: Path, root: Path | None = None) -> ReferenceIndex:
    """The real ingest of ``mailbox`` plus the stub embeddings, under ``root`` (a fresh temp
    dir when None)."""
    cfg = smoke_config(mailbox, root)
    ds = build_dataset(cfg)
    store = Store.open(ds.index_path)
    try:
        _embed_all(cfg, store)
    finally:
        store.close()
    return ReferenceIndex(cfg, ds.index_path)


def _script_citations(script: FakeScript) -> list[str]:
    """Every citation (per occurrence) in the answer text the script streams, in script order:
    a tool-call step's ``lead_content`` and an answer step's ``content``. A step's ``think`` text
    is excluded on purpose: reasoning is only counted, never forwarded or citation-filtered, so a
    mid cited there is not a raw citation of the turn."""
    texts: list[str] = []
    for step in script.requests:
        if step.tool_calls is not None:
            texts.append(step.lead_content or "")
        elif step.content is not None:
            texts.append(step.content)
    return [mid for text in texts for mid in find_citations(text)]


def _script_tool_names(script: FakeScript) -> list[str]:
    """The tool names the script calls, in call order."""
    return [call.name for step in script.requests for call in step.tool_calls or ()]


def _check_reference_inputs(
    store: Store,
    cited: Sequence[str],
    expected: Sequence[str],
    must_strip: Mapping[str, str],
) -> None:
    """The fixture and the script must make the case meaningful: every expected mid indexed and
    not conflicted; the hallucinated mid not indexed; the conflicted mid indexed and conflicted;
    both cited by the script (else the stripping check would pass vacuously)."""
    if not expected or len(set(expected)) != len(expected):
        raise SmokeError("expected mids must be non-empty and distinct")
    for mid in expected:
        row = store.get_message(mid)
        if row is None or row.conflicted:
            raise SmokeError("an expected mid is not indexed, or is conflicted")
    hallucinated, conflicted = must_strip["hallucinated"], must_strip["conflicted"]
    if hallucinated in expected or conflicted in expected:
        raise SmokeError("a mid that must be stripped is among the expected ones")
    if store.get_message(hallucinated) is not None:
        raise SmokeError("the hallucinated mid is indexed")
    row = store.get_message(conflicted)
    if row is None or not row.conflicted:
        raise SmokeError("the conflicted mid is not indexed as conflicted")
    for what, mid in must_strip.items():
        if mid not in cited:
            raise SmokeError(f"the script never cites the {what} mid")


async def _reference_turn(session: AgentSession, question: str) -> tuple[AgentTurn, list[str]]:
    handle = session.start_turn(question)
    deltas = [delta async for delta in handle]
    return await handle.result(), deltas


def run_reference(
    ref: ReferenceIndex,
    script: Path | FakeScript,
    *,
    question: str,
    expected: Sequence[str],
    hallucinated: str,
    conflicted: str,
) -> ReferenceResult:
    """One agent turn for ``question`` over ``ref``'s index: the real ``AgentSession`` (caller
    :data:`REFERENCE_CALLER`) with the built-in tools through the real ``Registry`` + ``Engine``
    + ``AuditLog``, and the real ``LLMClient`` over ``FakeLLM(script)``.

    Raises :class:`SmokeError` unless the turn ends ``ok`` with every scripted request used, the
    deltas join to the turn text, every tool call wrote one audit row as the agent caller (the
    script's tools, in its order, each executed without an error status), and
    citation validation stripped the ``hallucinated`` and the ``conflicted`` mid: neither is in
    the citations or anywhere in the shown text, every script citation was counted as raw, both
    were counted as invalid (the conflicted one as conflicted too) and each invalid one shows the
    removed marker. Without this check the score would still be 1.0 with stripping switched off
    (plan 08a A9)."""
    loaded = load_script(script) if isinstance(script, Path) else script
    cited = _script_citations(loaded)
    must_strip = {"hallucinated": hallucinated, "conflicted": conflicted}
    fake = FakeLLM(loaded)
    config = TernConfig.model_validate({**ref.cfg.config.model_dump(), "chat_model": fake.model_id})
    store = Store.open(ref.index, readonly=True)
    try:
        _check_reference_inputs(store, cited, expected, must_strip)
        with AuditLog.open(audit_path(ref.cfg.paths)) as audit:
            before = audit.count()
            llm = LLMClient(config, transport=fake.transport)
            session = AgentSession(
                REFERENCE_SESSION_KEY,
                _registry(ref.cfg, store, audit),
                llm,
                store,
                config,
                clock=time.monotonic,
            )

            async def run() -> tuple[AgentTurn, list[str]]:
                try:
                    return await _reference_turn(session, question)
                finally:
                    await llm.aclose()

            turn, deltas = asyncio.run(run())
            written = audit.count() - before
            rows = audit.list(limit=written) if written > 0 else []
    finally:
        store.close()
    try:
        fake.assert_done()
    except FakeLLMScriptError as exc:
        raise SmokeError(f"the FakeLLM script was not followed: {exc}") from exc
    if turn.outcome != "ok":
        raise SmokeError(f"the reference turn ended {turn.outcome!r}, not 'ok'")
    if "".join(deltas) != turn.text:
        raise SmokeError("the streamed deltas do not join to the turn text")
    m = turn.meta
    if m.tool_calls < 1 or len(rows) != m.tool_calls:
        raise SmokeError(f"{len(rows)} audit rows for {m.tool_calls} tool calls (at least 1)")
    if any(row.caller != REFERENCE_CALLER for row in rows):
        raise SmokeError("a reference audit row does not name the agent caller")
    oldest_first = rows[::-1]
    if [row.tool for row in oldest_first] != _script_tool_names(loaded):
        raise SmokeError("the audited tool calls are not the ones the script makes")
    if any(row.initial_status is None or row.status == "error" for row in oldest_first):
        raise SmokeError("a reference tool call was not executed, or failed")
    for what, mid in must_strip.items():
        if mid in turn.citations or mid in turn.text:
            raise SmokeError(f"the {what} mid was not stripped")
    if m.raw_citations != len(cited):
        raise SmokeError(f"{m.raw_citations} raw citations counted, the script has {len(cited)}")
    n_hallucinated, n_conflicted = cited.count(hallucinated), cited.count(conflicted)
    if m.conflicted_citations != n_conflicted or m.invalid_citations < (
        n_hallucinated + n_conflicted
    ):
        raise SmokeError(
            f"citation counts: invalid={m.invalid_citations} conflicted="
            f"{m.conflicted_citations}; the script cites the hallucinated mid {n_hallucinated}x "
            f"and the conflicted one {n_conflicted}x"
        )
    if turn.text.count(REMOVED_MARKER) + turn.text.count(CONFLICTED_MARKER) != m.invalid_citations:
        raise SmokeError("the shown text does not mark every invalid citation as removed")
    computed = len(set(turn.citations) & set(expected)) / len(set(expected))
    return ReferenceResult(computed, turn, tuple(deltas))


def reference_case(
    mailbox: Path,
    script: Path | FakeScript,
    *,
    question: str,
    expected: Sequence[str],
    hallucinated: str,
    conflicted: str,
    root: Path | None = None,
) -> ReferenceResult:
    """:func:`run_reference` on a fresh :func:`reference_index` of ``mailbox`` (adapter
    ``reference_case``)."""
    return run_reference(
        reference_index(mailbox, root),
        script,
        question=question,
        expected=expected,
        hallucinated=hallucinated,
        conflicted=conflicted,
    )
