"""``tern`` command line (plan 02: ``install-host``, ``bridge ping``, ``bridge schema``; plan 04:
``index status``, ``index rebuild``; plan 05: ``search``, embed lines in ``index status``; plan 06a:
``tools describe``; plan 06b: ``audit``, ``undo``; plan 07: ``mcp``).

In a dev session every ``tern`` command except ``--help``, ``--version`` and ``tools describe``
runs only with ``TERN_HOME`` on synthetic data (decisions #22).

``index status`` opens the index read-only (no daemon needed; it reads beside a running ``ternd``)
and prints counts and #28 short hashes only: never account names, folder paths, subjects or
addresses (decisions #28, CLAUDE.md). ``index rebuild`` (only while no ``ternd`` holds its lock)
moves the index aside and creates an empty one that keeps the TB profile binding; ``--rebind``
leaves it unbound, the explicit re-bind of architecture §3.

``search`` (plan 05 step 6) prints mail content (mids, senders, subjects, snippets), so against
real data it is user-run. It is a direct read-only CLI over ``$TERN_HOME/data/index.db``
(read-only ``Store``, sqlite-vec loaded) and ``config.toml`` until 06a/07 add the daemon path.
``hybrid`` (the default) embeds the query with the configured embedder (``_make_embedder``) and
fails loudly when that is impossible (no model configured or registered, a model mismatch, an
unreachable embedder); ``--fts-only`` is the explicit FTS-only mode and its output says so.
Folders are printed and accepted (``--folder``) as #28 short hashes. C0/C1 control characters
(and DEL) in printed fields become U+FFFD, so a subject cannot drive the terminal. Exit codes: 0
ok (also with no hit); 2 no index, no embedding model configured or registered, unknown folder
hash, empty query, bad arguments or an invalid config; 1 embedder unreachable or malformed,
egress refused, model mismatch, DB, schema or index-state errors.

``tools describe`` (plan 06a step 2) prints the ``tern-tool/1`` metadata of the built-ins and the
entry-point plugins (JSON list of ``ToolMeta``, sorted by name); ``--json-schema`` prints
``ToolMeta``'s JSON Schema (``contracts/tern-tool-1.schema.json``). It is metadata only, which is
why the guard exempts it: it never resolves ``tern.paths``, never reads ``config.toml``, never
loads config-path plugins and never opens the index, audit DB or socket. Exit 1 when a plugin
cannot be loaded or a tool name is taken.

``audit --last N`` (plan 06b step 10) reads ``audit.db`` directly over a read-only SQLite
connection (``mode=ro``: ternd stays its only writer; the reader takes no write lock and never
waits on ternd). A missing log prints ``no audit log`` and exits 0, creating nothing; on an
existing WAL log SQLite may create its ``-wal``/``-shm`` side files (with ``audit.db``'s mode,
0600) and leave them behind. Per call it prints ts, tool, tier, caller, decision, reason, derived
status, ``undone``, target count and undo token (a capability, not content: ``tern undo`` needs
it), oldest of the last N first, then the ``stale writes (>24 h tb_accepted): N`` line. It
never prints args, mids, ``caller_label`` or event details. Exit 1 when the log cannot be read (a
schema version it does not know, a damaged file, an I/O error).

``undo <token>`` (plan 06b step 10) asks ternd over the bridge (role ``cli``, method 1.0
``audit.undo``); ternd undoes as caller ``ui``. Exit 0 undone; 1 refused or failed (the bridge
error name and message are printed: ``state_changed``, ``invalid_params``, ``unavailable``, ...),
no reply in time, or a reply that is not a valid ``UndoResult`` (locations and error types only);
2 no ternd listening. The redo token is the undo's own row in ``tern audit``.

``mcp`` (plan 07 step 9) serves MCP on stdin/stdout through the running ternd
(:mod:`tern.mcp.proxy`, bridge 1.1 role ``mcp-proxy``); it takes no options (stdio only, #9) and
imports the MCP SDK lazily. Exit 0 when the client closes stdin; 1 refused by ternd, ternd too
old, or ternd closed the connection (at once, even while the client keeps stdin open); 2 no ternd
listening (``unavailable`` on stderr; it never
starts one).

Known limitations / deviations (plan 04 step 10):

- The counts beyond ``Store.status()`` (per-account and per-folder rows, rates, run counters,
  failed jobs) are SQL in this module, not fields of ``StatusCounts``, because ``store.py`` was
  not in step 10's files. Plan 08b's status additions and the bridge ``index.status`` need them
  too: the plan that next has ``store.py`` in scope should move them into ``Store.status()``.
- An account with a NULL name makes status fail (exit 1) rather than hash it;
  ``tern.index.events.folder_infos`` still hashes such a name as the string ``"None"`` (outside
  step 10's files), so the two must be aligned there (raise ``StoreError`` too).
- ``rate``: ``msg_per_s`` = sum of ``n_msgs`` / sum of ``secs`` over the ``backfill`` rows of
  an account type; this is a wall-clock rate because backfill pages folders one after another
  (concurrency is within a folder). ``-`` when the summed seconds are 0.
- Reading the old binding in ``index rebuild`` (read-only) may leave an empty ``-wal``/``-shm``
  beside ``index.db`` on a refusal; a read-only SQLite connection cannot remove them.
- ``search`` reads the folder short hashes before the search, outside its snapshot: a hit in a
  folder created while the search ran is an error (exit 1, retry), never an unlabelled folder.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from pydantic import ValidationError

import tern
from tern import config, paths
from tern.bridge.client import Peer
from tern.bridge.peercred import installed_nmhost_path
from tern.bridge.protocol import MINOR, PROTOCOL, BridgeError, PingStats, UndoResult, export_schema
from tern.daemon.uds import InstanceLock, prepare_runtime_dir
from tern.index.ingest import STATUS_DONE, is_indexed
from tern.index.mid import X_PREFIX
from tern.index.schema import SchemaError
from tern.index.service import ALL_FOLDERS, ERROR_PREFIX
from tern.index.shorthash import ShortHashCollision, assign, folder_name
from tern.index.store import FolderRow, Store, StoreError, index_path
from tern.llm.egress import EgressDenied
from tern.perms.audit import BUSY_TIMEOUT_MS as AUDIT_BUSY_TIMEOUT_MS
from tern.perms.audit import SCHEMA_VERSION as AUDIT_SCHEMA_VERSION
from tern.perms.audit import AuditLog, AuditRecord, AuditSchemaError, audit_path
from tern.perms.write_status import stale_writes
from tern.search import vec
from tern.search.embedder import Embedder, EmbedderError, EmbedderUnavailable, EmbeddingClient
from tern.search.service import (
    MAX_LIMIT,
    MIN_LIMIT,
    EmptyQuery,
    Hit,
    NoEmbeddingModel,
    SearchError,
    SearchFilters,
    SearchResult,
    SearchService,
)
from tern.tools.contract import ToolMeta
from tern.tools.registry import PluginLoadError, describe_registry

HOST_NAME = "tern"  # runtime.connectNative("tern")
EXTENSION_ID = "tern@tern.invalid"  # decisions #26
# TB 153 (tarball, Linux) reads per-user host manifests here (S1, A-6, decisions #3 amendment).
DEFAULT_HOST_DIR = Path(".mozilla") / "native-messaging-hosts"

S1_RTT_P50_MS = 0.837  # spikes/s1/REPORT.md, phase `rtt`
RTT_P50_GATE_MS = 20.0  # S1 H1; plan 02 decision Q1

MISSED_WINDOW_S = 86_400  # `missed_24h`


def host_manifest(nmhost_path: Path) -> dict[str, Any]:
    """The native-messaging manifest (same content as ``packaging/nmhost/tern.json.in``)."""
    if not nmhost_path.is_absolute():
        raise ValueError("the host path must be absolute (TB requires it on Linux)")
    return {
        "name": HOST_NAME,
        "description": "tern native-messaging host (relays Thunderbird <-> ternd)",
        "path": str(nmhost_path),
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }


def install_host(target_dir: Path, nmhost_path: Path) -> tuple[Path, bool]:
    """Write ``<target_dir>/tern.json``; returns (path, whether a file was replaced)."""
    manifest = host_manifest(nmhost_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{HOST_NAME}.json"
    replaced = out.exists()
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out, replaced


def _cli_hello() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "minor": MINOR,
        "role": "cli",
        "version": tern.__version__,
        "capabilities": [],
    }


async def ping_extension(sock: Path, n: int) -> PingStats:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    peer = Peer(reader, writer)
    task = asyncio.create_task(peer.run())
    try:
        await peer.request("hello", _cli_hello(), timeout=10)
        # The daemon waits up to 30 s for an extension, then each ping is a round trip.
        raw = await peer.request("bridge.ping_extension", {"n": n}, timeout=40 + n * 0.5)
        return PingStats.model_validate(raw)
    finally:
        await peer.close()
        await task


def _cmd_install_host(args: argparse.Namespace) -> int:
    target = Path(args.dir) if args.dir else Path.home() / DEFAULT_HOST_DIR
    out, replaced = install_host(target, installed_nmhost_path())
    print(f"{'replaced' if replaced else 'wrote'} {out}")
    return 0


def _cmd_bridge_ping(args: argparse.Namespace) -> int:
    sock = paths.resolve().socket
    try:
        s = asyncio.run(ping_extension(sock, args.n))
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"bridge ping: no ternd listening at {sock}", file=sys.stderr)
        return 2
    except BridgeError as err:
        print(f"bridge ping: {err.name}: {err.message}", file=sys.stderr)
        return 2
    verdict = "PASS" if s.p50_ms < RTT_P50_GATE_MS else "FAIL"
    print(
        f"bridge ping: n={s.n} p50={s.p50_ms:.3f} ms p90={s.p90_ms:.3f} ms "
        f"p99={s.p99_ms:.3f} ms max={s.max_ms:.3f} ms | {verdict} (p50 < {RTT_P50_GATE_MS:g} ms) "
        f"| {s.p50_ms / S1_RTT_P50_MS:.2f}x S1 p50 {S1_RTT_P50_MS} ms"
    )
    return 0 if verdict == "PASS" else 1


def _cmd_bridge_schema(args: argparse.Namespace) -> int:
    try:
        doc = export_schema(args.version)
    except ValueError as exc:
        print(f"bridge schema: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


# ---- index status ---------------------------------------------------------------------------


def x_mid_digest(x_mids: Sequence[str]) -> str:
    """First 8 hex of sha256 over the sorted x- mids, one per line (C2's detector, #30)."""
    return hashlib.sha256("\n".join(sorted(x_mids)).encode("ascii")).hexdigest()[:8]


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _distinct(values: Sequence[float | None]) -> str:
    """The distinct values of a per-run setting, comma-separated (one value unless it changed)."""
    known = sorted({v for v in values if v is not None})
    unknown = ["-"] if None in values else []
    return ",".join([_num(v) for v in known] + unknown)


#: Bidi controls (category Cf): ALM, LRM, RLM, the embeddings/overrides LRE..RLO and the
#: isolates LRI..PDI. Left alone they reorder the rest of the printed line.
BIDI_CONTROLS = (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
#: LINE SEPARATOR and PARAGRAPH SEPARATOR (categories Zl, Zp): line breaks to some terminals.
LINE_SEPARATORS = (0x2028, 0x2029)
#: C0 controls, DEL and C1 controls (Unicode category Cc), bidi controls and the Unicode line
#: separators -> U+FFFD in printed fields.
_CONTROLS = {
    c: "\ufffd" for c in (*range(0x20), *range(0x7F, 0xA0), *BIDI_CONTROLS, *LINE_SEPARATORS)
}


def printable(text: str) -> str:
    """``text`` with every C0/C1 control character (incl. ESC, TAB, newlines), DEL, bidi control
    and U+2028/U+2029 replaced by U+FFFD, so a printed field cannot move the cursor, clear the
    screen, break a line or reorder the fields printed beside it."""
    return text.translate(_CONTROLS)


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Labels:
    """The index's accounts and folders with their #28 short hashes."""

    #: account_id -> (name, type)
    accounts: dict[str, tuple[str, str | None]]
    folders: list[FolderRow]
    #: account_id -> ``acct-…``
    acct: dict[str, str]
    #: folder_id -> ``fold-…``
    fold: dict[str, str]


def index_labels(store: Store) -> Labels:
    """Short hashes for every account and folder (the set widens together on a collision, #28).
    Raises StoreError for an unnamed account or a folder whose account is missing, and
    ShortHashCollision when distinct names still collide."""
    conn = store.connection
    rows = conn.execute("SELECT account_id, name, type FROM accounts").fetchall()
    unnamed = sum(1 for r in rows if r[1] is None)
    if unnamed:
        raise StoreError(f"{unnamed} account(s) without a name in the index")
    accounts: dict[str, tuple[str, str | None]] = {r[0]: (r[1], r[2]) for r in rows}
    folders = store.folders()
    missing = {f.account_id for f in folders} - accounts.keys()
    if missing:
        raise StoreError(f"{len(missing)} folder account(s) missing from the index")
    acct_by_name = assign((name for name, _ in accounts.values()), "acct")
    acct = {aid: acct_by_name[name] for aid, (name, _) in accounts.items()}
    fnames = {f.folder_id: folder_name(accounts[f.account_id][0], f.path) for f in folders}
    fold_by_name = assign(fnames.values(), "fold")
    fold = {fid: fold_by_name[name] for fid, name in fnames.items()}
    return Labels(accounts=accounts, folders=folders, acct=acct, fold=fold)


def embed_status_lines(conn: sqlite3.Connection, *, max_input_chars: int) -> list[str]:
    """The three ``embed`` lines of ``tern index status`` (model, progress, completion); no
    content. Raises ``VecStateError`` when the vector tables are inconsistent."""
    es = vec.status(conn, max_input_chars=max_input_chars)
    m = es.model
    model = (
        "embed model=none dim=- quant=-"
        if m is None
        else f"embed model={printable(m.model_id)} dim={m.dim} quant={printable(m.quant)}"
    )
    complete = "-" if es.embed_complete_at is None else _utc(es.embed_complete_at)
    return [
        model,
        f"embed done={es.done} pending={es.pending} truncated={es.truncated}",
        f"embed complete_at={complete}",
    ]


def index_status_lines(store: Store, now: float, *, max_input_chars: int) -> list[str]:
    """``tern index status`` output: counts and #28 short hashes only. All reads run in one read
    transaction, so the numbers are one consistent snapshot even beside a writing ternd.
    ``max_input_chars`` (``TernConfig.embed_max_input_chars``) defines ``truncated``."""
    conn = store.connection
    conn.execute("BEGIN")
    try:
        return _status_lines(store, conn, now, max_input_chars)
    finally:
        conn.execute("ROLLBACK")


def _status_lines(
    store: Store, conn: sqlite3.Connection, now: float, max_input_chars: int
) -> list[str]:
    st = store.status()
    xp = (len(X_PREFIX), X_PREFIX)
    labels = index_labels(store)
    accounts, folders, acct, fold = labels.accounts, labels.folders, labels.acct, labels.fold
    indexed = [f for f in folders if is_indexed(f.special_use)]

    def grouped(sql: str, *args: object) -> dict[str, int]:
        return {r[0]: int(r[1]) for r in conn.execute(sql, args)}

    locs = grouped("SELECT folder_id, count(*) FROM locations GROUP BY folder_id")
    x_locs = grouped(
        "SELECT folder_id, count(*) FROM locations WHERE substr(mid, 1, ?) = ? GROUP BY folder_id",
        *xp,
    )
    ho = grouped("SELECT folder_id, count(*) FROM headers_only_items GROUP BY folder_id")
    ho_noid = grouped(
        "SELECT folder_id, count(*) FROM headers_only_items WHERE mid IS NULL GROUP BY folder_id"
    )
    # tb_error rows may stand for Message-ID items: md5_ids counts the large_noid ones only
    skipped = grouped(
        "SELECT folder_id, count(*) FROM skipped_items WHERE reason = 'large_noid' "
        "GROUP BY folder_id"
    )
    acct_conflicted = grouped(
        "SELECT f.account_id, count(DISTINCT m.mid) FROM messages m "
        "JOIN locations l ON l.mid = m.mid JOIN folders f ON f.folder_id = l.folder_id "
        "WHERE m.conflicted = 1 GROUP BY f.account_id"
    )
    acct_x = grouped(
        "SELECT f.account_id, count(DISTINCT l.mid) FROM locations l "
        "JOIN folders f ON f.folder_id = l.folder_id WHERE substr(l.mid, 1, ?) = ? "
        "GROUP BY f.account_id",
        *xp,
    )
    classes = conn.execute(
        "SELECT f.account_id, m.conflict_class, count(DISTINCT m.mid) FROM messages m "
        "JOIN locations l ON l.mid = m.mid JOIN folders f ON f.folder_id = l.folder_id "
        "WHERE m.conflicted = 1 GROUP BY f.account_id, m.conflict_class"
    ).fetchall()
    x_mids = [
        r[0] for r in conn.execute("SELECT mid FROM messages WHERE substr(mid, 1, ?) = ?", xp)
    ]
    churn, mismatch, unresolved, stale = conn.execute(
        "SELECT coalesce(sum(x_mid_churn), 0), coalesce(sum(hmid_mismatch), 0), "
        "coalesce(sum(unresolved_events), 0), coalesce(sum(stale_retries), 0) FROM ingest_runs"
    ).fetchone()
    missed_24h = conn.execute(
        "SELECT coalesce(sum(missed), 0) FROM ingest_runs WHERE ts >= ?",
        (int(now) - MISSED_WINDOW_S,),
    ).fetchone()[0]
    rates = conn.execute(
        "SELECT account_type, sum(n_msgs), sum(secs) FROM ingest_runs WHERE kind = 'backfill' "
        "GROUP BY account_type ORDER BY account_type IS NULL, account_type"
    ).fetchall()
    settings: dict[str | None, tuple[list[float | None], list[float | None]]] = defaultdict(
        lambda: ([], [])
    )
    for account_type, concurrency, throttle in conn.execute(
        "SELECT DISTINCT account_type, concurrency, throttle FROM ingest_runs "
        "WHERE kind = 'backfill'"
    ):
        settings[account_type][0].append(concurrency)
        settings[account_type][1].append(throttle)
    state = {r[0]: r[1] for r in conn.execute("SELECT folder_id, status FROM ingest_state")}

    out = [
        f"schema_version {st.schema_version}",
        f"messages {st.messages}",
        f"locations {st.locations}",
        f"same_folder_copies {st.same_folder_copies}",
        f"conflicted {st.conflicted}",
        f"conflicts_unclassified {st.conflicts_unclassified}",
        f"refill_pending {st.refill_pending}",
        f"x_mids {st.x_mids}",
        f"x_mid_digest {x_mid_digest(x_mids)}",
        f"x_mid_churn {churn}",
        f"hmid_mismatch {mismatch}",
        f"headers_only {st.headers_only}",
        f"skipped_large_noid {st.skipped_large_noid}",
        f"skipped_tb_error {st.skipped_tb_error}",
        f"unresolved_events {unresolved}",
        f"parse_status ok={st.parse_ok} partial={st.parse_partial} failed={st.parse_failed}",
    ]
    out += sorted(
        f"conflict_class {acct[aid]} {cls if cls is not None else 'unclassified'} {n}"
        for aid, cls, n in classes
    )
    for aid in sorted(accounts, key=lambda a: acct[a]):
        ids = [f.folder_id for f in indexed if f.account_id == aid]
        md5_ids = sum(x_locs.get(i, 0) + ho_noid.get(i, 0) + skipped.get(i, 0) for i in ids)
        out.append(
            f"{acct[aid]} type={accounts[aid][1] or '-'} folders={len(ids)} "
            f"locations={sum(locs.get(i, 0) for i in ids)} "
            f"conflicted={acct_conflicted.get(aid, 0)} x_mids={acct_x.get(aid, 0)} "
            f"md5_ids={md5_ids} headers_only={sum(ho.get(i, 0) for i in ids)}"
        )
    for f in sorted(indexed, key=lambda f: (acct[f.account_id], fold[f.folder_id])):
        out.append(
            f"{fold[f.folder_id]} {acct[f.account_id]} locations={locs.get(f.folder_id, 0)} "
            f"tb_total={_num(f.tb_total)}"
        )
    for account_type, msgs, secs in rates:
        conc, throttle = settings[account_type]
        rate = f"{msgs / secs:.1f}" if secs > 0 else "-"
        out.append(
            f"rate {account_type or '-'} msgs={msgs} secs={secs:.1f} msg_per_s={rate} "
            f"concurrency={_distinct(conc)} throttle={_distinct(throttle)}"
        )
    out += embed_status_lines(conn, max_input_chars=max_input_chars)
    done = sum(1 for f in indexed if state.get(f.folder_id) == STATUS_DONE)
    failed = sorted(
        (fid, status)
        for fid, status in state.items()
        if status is not None and status.startswith(ERROR_PREFIX)
    )
    out += [
        f"missed_24h {missed_24h}",
        f"stale_retries {stale}",
        f"backfill folders_done {done}/{len(indexed)}",
        f"jobs_failed {len(failed)}",
    ]
    for fid, status in failed:
        label = ALL_FOLDERS if fid == ALL_FOLDERS else fold.get(fid, "fold-unlisted")
        out.append(f"  {label} {status}")
    return out


_SCHEMA_DAMAGE = ("no such table", "no such column")


def _db_error_advice(exc: sqlite3.DatabaseError) -> str | None:
    """What to tell the user about a database error reading the index, or None when the error
    comes from tern's own code (``ProgrammingError``, ``IntegrityError``, ``DataError``,
    ``InternalError``, ``NotSupportedError``): that is a bug, raised with its traceback, never
    answered with advice to throw a healthy index away.

    - ``OperationalError`` "no such table/column": the schema does not match its recorded
      version (damaged): rebuild. A typo in tern's own status SQL reads the same way and cannot
      be told apart here, so the advice adds that the same error after a rebuild is a tern bug;
    - any other ``OperationalError`` (locked, cannot open, I/O) may pass: retry;
    - plain ``DatabaseError`` (not a database, malformed image): the file is damaged: rebuild."""
    if isinstance(exc, sqlite3.OperationalError):
        if str(exc).startswith(_SCHEMA_DAMAGE):
            return (
                "its schema is damaged; recreate it with `tern index rebuild` (if the same error "
                "follows a rebuild, it is a tern bug: report it)"
            )
        return "retry; if it persists, check the index file and its directory"
    if type(exc) is sqlite3.DatabaseError:
        return "recreate it with `tern index rebuild`"
    return None


def _status_db_error(exc: sqlite3.DatabaseError, advice: str) -> int:
    print(
        f"index status: cannot read the index ({type(exc).__name__}: {exc}); {advice}",
        file=sys.stderr,
    )
    return 1


def _load_config(tp: paths.TernPaths, prefix: str) -> config.TernConfig | None:
    """``config.toml`` validated, or None after printing why it cannot be used."""
    try:
        return config.load(tp.config_file)
    except (config.ConfigError, ValidationError) as exc:
        print(f"{prefix}: invalid config: {exc}", file=sys.stderr)
        return None


def _cmd_index_status(args: argparse.Namespace) -> int:
    tp = paths.resolve()
    cfg = _load_config(tp, "index status")
    if cfg is None:
        return 2
    path = index_path(tp)
    try:
        store = Store.open(path, readonly=True)
    except FileNotFoundError:
        print("index status: no index (ternd creates it when it first starts)", file=sys.stderr)
        return 2
    except SchemaError as exc:
        print(f"index status: {exc}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as exc:
        if (advice := _db_error_advice(exc)) is None:
            raise
        return _status_db_error(exc, advice)
    try:
        with store:
            lines = index_status_lines(
                store, time.time(), max_input_chars=cfg.embed_max_input_chars
            )
    except (StoreError, vec.VecStateError) as exc:
        print(f"index status: inconsistent index: {exc}", file=sys.stderr)
        return 1
    except ShortHashCollision as exc:
        # Names are never printed (#28), so status cannot show these rows unambiguously.
        print(f"index status: cannot label accounts/folders: {exc}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as exc:
        if (advice := _db_error_advice(exc)) is None:
            raise
        return _status_db_error(exc, advice)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


# ---- index rebuild --------------------------------------------------------------------------


class RebuildRefused(RuntimeError):
    """``tern index rebuild`` cannot run as asked; nothing was changed."""


@dataclass(frozen=True)
class RebuildResult:
    #: (old file, its backup), in the order moved; empty when there was no index.
    moved: list[tuple[Path, Path]]
    #: The TB profile the new index is bound to (None: unbound).
    profile_id: UUID | None


_INDEX_SUFFIXES = ("", "-wal", "-shm")


def _bound_profile(path: Path) -> UUID | None:
    """``meta.tb_profile_id`` of the index at ``path``, read without the schema check (a newer
    schema is a reason to rebuild, so it must not stop the binding being carried over)."""
    uri = "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if has_meta is None:
            return None
        row = conn.execute("SELECT value FROM meta WHERE key = 'tb_profile_id'").fetchone()
        return None if row is None else UUID(row[0])
    finally:
        conn.close()


def rebuild_index(tp: paths.TernPaths, *, rebind: bool, now: int) -> RebuildResult:
    """Move ``index.db`` (and its ``-wal``/``-shm``) to ``index.db.bak-<now>`` (``-wal``/``-shm``
    keep their suffix after it, so the backup stays an openable pair) and create an empty index,
    bound to the old index's TB profile unless ``rebind``. Holds ternd's instance lock throughout,
    so no ternd starts meanwhile. Raises :class:`RebuildRefused`, with nothing changed, while a
    ternd holds the lock, when a backup name is taken, or when the old binding cannot be read."""
    prepare_runtime_dir(tp.run)
    lock = InstanceLock(tp.lock)
    if not lock.acquire():
        raise RebuildRefused("ternd is running (it holds its lock); stop it first")
    try:
        path = index_path(tp)
        profile: UUID | None = None
        if path.is_file() and not rebind:
            try:
                profile = _bound_profile(path)
            except (sqlite3.DatabaseError, ValueError) as exc:
                raise RebuildRefused(
                    f"cannot read the old index's TB profile binding ({type(exc).__name__}); "
                    "rerun with --rebind to rebuild it unbound"
                ) from exc
        moves = [
            (path.with_name(path.name + sfx), path.with_name(f"{path.name}.bak-{now}{sfx}"))
            for sfx in _INDEX_SUFFIXES
            if path.with_name(path.name + sfx).exists()
        ]
        taken = [dst.name for _, dst in moves if dst.exists()]
        if taken:
            raise RebuildRefused(f"backup name already taken: {', '.join(taken)}")
        for src, dst in moves:
            src.rename(dst)
        with Store.open(path) as store:
            if profile is not None:
                store.bind_profile(profile)
            bound = store.profile_id()
        return RebuildResult(moved=moves, profile_id=bound)
    finally:
        lock.release()


def _cmd_index_rebuild(args: argparse.Namespace) -> int:
    try:
        result = rebuild_index(paths.resolve(), rebind=args.rebind, now=int(time.time()))
    except RebuildRefused as exc:
        print(f"index rebuild: refused: {exc}", file=sys.stderr)
        return 1
    for src, dst in result.moved:
        print(f"index rebuild: moved {src.name} -> {dst.name}")
    if not result.moved:
        print("index rebuild: there was no index")
    if result.profile_id is not None:
        binding = "kept the TB profile binding"
    elif args.rebind:
        binding = "unbound (--rebind): the next extension to connect binds it"
    else:
        binding = "unbound (there was no binding to keep)"
    print(f"index rebuild: new empty index, {binding}; ternd backfills it when it next starts")
    return 0


# ---- search ---------------------------------------------------------------------------------


_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DIGITS = re.compile(r"[0-9]+")


def _day(text: str) -> datetime:
    """``YYYY-MM-DD`` as midnight UTC of that day (argparse type)."""
    if not _DAY.fullmatch(text):
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    try:
        d = date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a calendar date") from None
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _limit(text: str) -> int:
    """An int in MIN_LIMIT..MAX_LIMIT (argparse type)."""
    if not _DIGITS.fullmatch(text) or not MIN_LIMIT <= int(text) <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"expected an integer in {MIN_LIMIT}..{MAX_LIMIT}")
    return int(text)


def _make_embedder(cfg: config.TernConfig) -> Embedder:
    """The query embedder for hybrid search (tests replace it with a ``StubEmbedder``)."""
    return EmbeddingClient(cfg)


class SearchArgError(Exception):
    """A ``tern search`` argument this index cannot use (unknown ``--folder`` hash, a ``--from``
    that is blank after folding); exit 2."""


def _sender(hit: Hit) -> str:
    name, addr = hit.from_name, hit.from_addr
    if name and addr:
        return f"{name} <{addr}>"
    return name or addr or "-"


def search_lines(result: SearchResult, fold: dict[str, str]) -> list[str]:
    """``tern search`` output for ``result``; folders as short hashes (``fold``: folder_id ->
    hash). Every printed field passes :func:`printable`. Raises StoreError for a hit in a
    folder ``fold`` does not know (the index changed after the labels were read)."""
    if result.mode == "hybrid":
        if result.model_id is None:
            raise RuntimeError("bug: a hybrid SearchResult without a model_id")
        out = [
            f"mode: hybrid model={printable(result.model_id)} embed_pending={result.embed_pending}"
        ]
    else:
        out = ["mode: fts-only"]
    for rank, hit in enumerate(result.hits, start=1):
        unknown = [fid for fid in hit.folder_ids if fid not in fold]
        if unknown:
            raise StoreError("a hit's folder is not in the index (it changed meanwhile); retry")
        folders = ",".join(sorted(fold[fid] for fid in hit.folder_ids)) or "-"
        when = "-" if hit.date is None else hit.date.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        flag = " conflicted" if hit.conflicted else ""
        out += [
            f"{rank} {printable(hit.mid)} {when} {hit.score:.6f}{flag} folders={folders}",
            f"  from: {printable(_sender(hit))}",
            f"  subject: {printable(hit.subject) if hit.subject is not None else '-'}",
            f"  snippet: {printable(hit.snippet)}",
        ]
    return out


def _search_fail(message: str, code: int) -> int:
    print(f"search: {message}", file=sys.stderr)
    return code


async def _search(
    service: SearchService,
    embedder: Embedder | None,
    query: str,
    filters: SearchFilters,
    limit: int,
) -> SearchResult:
    try:
        return await service.search(query, filters, limit)
    finally:
        if isinstance(embedder, EmbeddingClient):
            await embedder.aclose()


def _search_in(store: Store, cfg: config.TernConfig, args: argparse.Namespace) -> list[str]:
    """Resolve ``--folder``, build the filters and the service, run the search, format it."""
    labels = index_labels(store)
    folder_id: str | None = None
    if args.folder is not None:
        by_hash = {h: fid for fid, h in labels.fold.items()}
        if args.folder not in by_hash:
            raise SearchArgError("--folder: unknown folder short hash (see `tern index status`)")
        folder_id = by_hash[args.folder]
    try:
        filters = SearchFilters(
            folder_id=folder_id,
            from_=args.from_,
            after=args.after,
            before=args.before,
            has_attachment=True if args.has_attachment else None,
        )
    except ValueError as exc:  # the only argument it can refuse here: a blank --from
        raise SearchArgError(f"--from: {exc}") from None
    embedder = None if args.fts_only else _make_embedder(cfg)
    if embedder is None:
        service = SearchService(store, None, mode="fts")
    else:
        service = SearchService(store, embedder, mode="hybrid")
    result = asyncio.run(_search(service, embedder, " ".join(args.query), filters, args.limit))
    return search_lines(result, labels.fold)


def _cmd_search(args: argparse.Namespace) -> int:
    tp = paths.resolve()
    cfg = _load_config(tp, "search")
    if cfg is None:
        return 2
    if not args.fts_only and cfg.embed_model is None:
        return _search_fail(
            "no embedding model configured (embed_model is empty); use --fts-only for an "
            "FTS-only search",
            2,
        )
    try:
        store = Store.open(index_path(tp), readonly=True)
    except FileNotFoundError:
        return _search_fail("no index (ternd creates it when it first starts)", 2)
    except SchemaError as exc:
        return _search_fail(str(exc), 1)
    except sqlite3.DatabaseError as exc:
        if (advice := _db_error_advice(exc)) is None:
            raise
        return _search_fail(f"cannot read the index ({type(exc).__name__}: {exc}); {advice}", 1)
    try:
        with store:
            lines = _search_in(store, cfg, args)
    except (EmptyQuery, SearchArgError) as exc:
        return _search_fail(str(exc), 2)
    except NoEmbeddingModel as exc:
        return _search_fail(f"{exc} (ternd registers it; use --fts-only meanwhile)", 2)
    except EmbedderUnavailable as exc:
        return _search_fail(f"embedder unreachable: {exc}", 1)
    except EmbedderError as exc:
        return _search_fail(f"embedder error: {exc}", 1)
    except EgressDenied as exc:
        return _search_fail(f"embedder URL refused: {exc}", 1)
    except SearchError as exc:  # ModelMismatch, SearchStateError
        return _search_fail(f"{type(exc).__name__}: {exc}", 1)
    except (StoreError, vec.VecStateError, ShortHashCollision) as exc:
        return _search_fail(f"inconsistent index: {exc}", 1)
    except sqlite3.DatabaseError as exc:
        if (advice := _db_error_advice(exc)) is None:
            raise
        return _search_fail(f"cannot read the index ({type(exc).__name__}: {exc}); {advice}", 1)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


# ---- audit / undo (plan 06b step 10) ---------------------------------------------------------

#: ``tern audit --last`` bounds.
AUDIT_LAST_MAX = 1000
AUDIT_LAST_DEFAULT = 20
#: How long ``tern undo`` waits for ternd's answer. ternd bounds the undo itself (30 s for an
#: extension to attach, 120 s per extension request); past this the outcome is unknown.
UNDO_REPLY_TIMEOUT_S = 600.0


def _last(text: str) -> int:
    """An int in 1..AUDIT_LAST_MAX (argparse type)."""
    if not _DIGITS.fullmatch(text) or not 1 <= int(text) <= AUDIT_LAST_MAX:
        raise argparse.ArgumentTypeError(f"expected an integer in 1..{AUDIT_LAST_MAX}")
    return int(text)


def open_audit_reader(path: Path) -> AuditLog:
    """``audit.db`` over a read-only connection (``mode=ro``): every write is refused, no write
    lock is taken (``AuditLog.open`` checks its schema in a ``BEGIN IMMEDIATE`` transaction and
    would wait on ternd). A missing log is not created; on an existing WAL log SQLite may create
    its ``-wal``/``-shm`` side files (with ``audit.db``'s mode) and leave them behind. Raises
    FileNotFoundError when there is no log, :class:`AuditSchemaError` for a schema version this
    tern does not read, and sqlite3 errors for a damaged or unreadable file."""
    if not path.is_file():
        raise FileNotFoundError(f"no audit log at {path}")
    uri = "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {AUDIT_BUSY_TIMEOUT_MS}")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != AUDIT_SCHEMA_VERSION:
            raise AuditSchemaError(
                f"audit.db schema version {version}; this tern reads version {AUDIT_SCHEMA_VERSION}"
            )
    except BaseException:  # close, then re-raise: never swallowed
        conn.close()
        raise
    return AuditLog(conn, path, time.time)


def audit_line(r: AuditRecord) -> str:
    """One call of ``tern audit``: no args, mids, ``caller_label`` or event detail. Every field
    is a validated code or id in ``audit.db``; each still passes :func:`printable`."""
    tier = "-" if r.tier is None else r.tier.value
    return (
        f"{_utc(int(r.ts))} tool={printable(r.tool)} tier={tier} caller={printable(r.caller)} "
        f"decision={r.decision} reason={printable(r.reason or '-')} status={r.status or '-'} "
        f"undone={'yes' if r.undone else 'no'} targets={r.target_count} "
        f"undo_token={printable(r.undo_token or '-')}"
    )


def audit_lines(audit: AuditLog, *, last: int, now: float) -> list[str]:
    """``tern audit`` output: the last ``last`` calls, oldest first, then the stale-writes
    line."""
    records = audit.list(limit=last)
    out = [audit_line(r) for r in reversed(records)] or ["no calls"]
    out.append(f"stale writes (>24 h tb_accepted): {stale_writes(audit, now)}")
    return out


def _audit_db_error(exc: sqlite3.DatabaseError) -> bool:
    """A database error the user can act on (locked, I/O, not a database), as opposed to a bug
    in tern's own SQL (``ProgrammingError``, ...), which is raised with its traceback."""
    return isinstance(exc, sqlite3.OperationalError) or type(exc) is sqlite3.DatabaseError


def _cmd_audit(args: argparse.Namespace) -> int:
    path = audit_path(paths.resolve())
    try:
        audit = open_audit_reader(path)
    except FileNotFoundError:
        print("no audit log")
        return 0
    except AuditSchemaError as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as exc:
        if not _audit_db_error(exc):
            raise
        print(f"audit: cannot read the audit log ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    try:
        with audit:
            conn = audit._conn  # pyright: ignore[reportPrivateUsage] -- one read snapshot for the calls and the stale count (audit.py is not to be touched)
            conn.execute("BEGIN")
            try:
                lines = audit_lines(audit, last=args.last, now=time.time())
            finally:
                # SQLite rolls back by itself on some errors (IOERR, FULL, NOMEM): a second
                # ROLLBACK would raise and hide the real error.
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
    except sqlite3.DatabaseError as exc:
        if not _audit_db_error(exc):
            raise
        print(f"audit: cannot read the audit log ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


class UndoNoReply(Exception):
    """ternd did not answer ``audit.undo`` within :data:`UNDO_REPLY_TIMEOUT_S`."""


async def undo_via_daemon(
    sock: Path, token: str, *, timeout: float = UNDO_REPLY_TIMEOUT_S
) -> UndoResult:
    """Send bridge 1.0 ``audit.undo`` to ternd as role ``cli``. Raises ``BridgeError`` with
    ternd's refusal, :class:`UndoNoReply` when no answer comes within ``timeout``."""
    reader, writer = await asyncio.open_unix_connection(str(sock))
    peer = Peer(reader, writer)
    task = asyncio.create_task(peer.run())
    try:
        await peer.request("hello", _cli_hello(), timeout=10)
        try:
            raw = await asyncio.wait_for(peer.request("audit.undo", {"undo_token": token}), timeout)
        except TimeoutError:
            raise UndoNoReply(f"no reply from ternd within {timeout:g} s") from None
        return UndoResult.model_validate(raw)
    finally:
        await peer.close()
        await task


def _cmd_undo(args: argparse.Namespace) -> int:
    sock = paths.resolve().socket
    try:
        result = asyncio.run(undo_via_daemon(sock, args.token, timeout=UNDO_REPLY_TIMEOUT_S))
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"undo: no ternd listening at {sock}", file=sys.stderr)
        return 2
    except BridgeError as err:
        print(f"undo: {err.name}: {printable(err.message)}", file=sys.stderr)
        return 1
    except UndoNoReply as exc:
        print(f"undo: {exc}; it may still complete: check `tern audit`", file=sys.stderr)
        return 1
    except ValidationError as exc:  # field locations and error types only, never the input
        errors = exc.errors(include_input=False, include_url=False)
        where = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['type']}" for e in errors
        )
        print(
            f"undo: invalid reply from ternd ({printable(where)}); it may have completed: "
            "check `tern audit`",
            file=sys.stderr,
        )
        return 1
    if not result.undone:
        print("undo: ternd reported nothing undone", file=sys.stderr)
        return 1
    print("undone (its redo token is on the undo's row in `tern audit`)")
    return 0


# ---- mcp ------------------------------------------------------------------------------------


def _cmd_mcp(args: argparse.Namespace) -> int:
    """``tern mcp``: serve MCP on stdin/stdout through the running ternd (plan 07 step 9). The SDK
    is imported here only, so no other ``tern`` command imports its web stack. stdout is the
    protocol: logs and errors go to stderr."""
    from tern import log as tern_log
    from tern.mcp.proxy import NoTernd, ProxyError, serve_proxy

    tern_log.setup(sys.stderr, logging.WARNING)
    sock = paths.resolve().socket
    try:
        asyncio.run(serve_proxy(sock))
    except NoTernd as exc:
        print(f"mcp: unavailable: {exc}", file=sys.stderr)
        return 2
    except BridgeError as err:
        print(f"mcp: {err.name}: {printable(err.message)}", file=sys.stderr)
        return 1
    except ProxyError as exc:
        print(f"mcp: {exc}", file=sys.stderr)
        return 1
    return 0


# ---- tools describe -------------------------------------------------------------------------


def _json_out(doc: object) -> None:
    sys.stdout.write(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _cmd_tools_describe(args: argparse.Namespace) -> int:
    if args.json_schema:
        _json_out(ToolMeta.model_json_schema())
        return 0
    try:
        registry = describe_registry()
    except PluginLoadError as exc:
        print(f"tools describe: {exc}", file=sys.stderr)
        return 1
    _json_out([m.model_dump(mode="json") for m in registry.describe()])
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tern", description="tern command line")
    ap.add_argument("--version", action="version", version=f"tern {tern.__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    ih = sub.add_parser("install-host", help="install the native-messaging host manifest")
    ih.add_argument("--dir", help=argparse.SUPPRESS)  # tests only
    ih.set_defaults(func=_cmd_install_host)

    br = sub.add_parser("bridge", help="bridge utilities")
    brs = br.add_subparsers(dest="bridge_command", required=True)
    ping = brs.add_parser("ping", help="daemon<->extension round-trip time")
    ping.add_argument("--n", type=int, default=1000)
    ping.set_defaults(func=_cmd_bridge_ping)
    schema = brs.add_parser("schema", help="print the tern-bridge JSON schema")
    schema.add_argument("--version", dest="version", required=True)
    schema.set_defaults(func=_cmd_bridge_schema)

    ix = sub.add_parser("index", help="the local index")
    ixs = ix.add_subparsers(dest="index_command", required=True)
    status = ixs.add_parser("status", help="index counts (short hashes only, read-only)")
    status.set_defaults(func=_cmd_index_status)
    rebuild = ixs.add_parser(
        "rebuild", help="move the index aside and start an empty one (ternd must be stopped)"
    )
    rebuild.add_argument(
        "--rebind",
        action="store_true",
        help="leave the new index unbound: the next extension to connect binds its TB profile",
    )
    rebuild.set_defaults(func=_cmd_index_rebuild)

    se = sub.add_parser(
        "search",
        help="search the index (prints mail content; read-only)",
        description="Hybrid search (FTS5 + vectors, RRF) over the local index, or --fts-only.",
    )
    se.add_argument("query", nargs="+", metavar="QUERY", help="words to search for")
    se.add_argument(
        "--limit", type=_limit, default=10, help=f"hits ({MIN_LIMIT}..{MAX_LIMIT}, default 10)"
    )
    se.add_argument("--folder", help="only this folder (its short hash, e.g. fold-1a2b)")
    se.add_argument("--from", dest="from_", metavar="S", help="sender name/address contains S")
    se.add_argument("--after", type=_day, help="dated on or after this day (YYYY-MM-DD, UTC)")
    se.add_argument("--before", type=_day, help="dated before this day (YYYY-MM-DD, UTC)")
    se.add_argument("--has-attachment", action="store_true", help="only mails with attachments")
    se.add_argument(
        "--fts-only", action="store_true", help="full-text search only (no embedder needed)"
    )
    se.set_defaults(func=_cmd_search)

    to = sub.add_parser("tools", help="tern-tool/1 tools")
    tos = to.add_subparsers(dest="tools_command", required=True)
    de = tos.add_parser(
        "describe",
        help="print tool metadata (built-ins + entry-point plugins; reads no config or data)",
    )
    de.add_argument(
        "--json-schema",
        action="store_true",
        help="print the JSON Schema of the tool metadata (tern-tool/1) instead",
    )
    de.set_defaults(func=_cmd_tools_describe)

    au = sub.add_parser(
        "audit", help="recent tool calls from the audit log (codes and counts only; read-only)"
    )
    au.add_argument(
        "--last",
        type=_last,
        default=AUDIT_LAST_DEFAULT,
        metavar="N",
        help=f"the last N calls (1..{AUDIT_LAST_MAX}, default {AUDIT_LAST_DEFAULT})",
    )
    au.set_defaults(func=_cmd_audit)

    un = sub.add_parser("undo", help="undo a write by its undo token (asks ternd)")
    un.add_argument("token", help="the undo token (see `tern audit`)")
    un.set_defaults(func=_cmd_undo)

    mc = sub.add_parser(
        "mcp",
        help="serve tern's tools to a local MCP client over stdin/stdout (needs a running ternd)",
        description="An MCP server over stdio only: an MCP client (e.g. LM Studio) starts this "
        "command. Every tool call goes through ternd's permission engine and audit log.",
    )
    mc.set_defaults(func=_cmd_mcp)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
