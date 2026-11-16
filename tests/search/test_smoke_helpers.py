"""Plan 05 step 8: ``tern.testing.smoke``, the helpers behind ``tests/smoke/adapter.py``.

The smoke contracts' module-scoped fixtures run before the autouse ``TERN_HOME`` fixture, so the
helpers are exercised here the same way: in a module-scoped fixture, with ``TERN_HOME`` unset,
the ``XDG_*`` dirs pointed at trap paths that must stay absent, ``platformdirs.user_*_dir``,
``tern.config.load`` and an env-less ``tern.paths.resolve`` raising, and every
``sqlite3.connect`` path recorded. Expected mids come from MANIFEST.json keys, never from a
search run.
"""

from __future__ import annotations

import dataclasses
import gc
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

import platformdirs
import pytest
import test_fixture_manifest as tfm

import tern.config
import tern.paths
from tern.index.store import Store
from tern.search import vec
from tern.search.service import SearchService
from tern.testing import smoke
from tern.testing.stub_embedder import StubEmbedder

FIXTURE = tfm.FIXTURE
MANIFEST: dict[str, Any] = tfm.load_manifest(FIXTURE)
MID = {m["key"]: m["mid"] for m in MANIFEST["messages"]}
XDG_VARS = (
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_CACHE_HOME",
)
PLATFORMDIRS_FNS = (
    "user_data_dir",
    "user_state_dir",
    "user_config_dir",
    "user_runtime_dir",
    "user_cache_dir",
    "user_log_dir",
)


class Count(NamedTuple):
    """One ``smoke._message_count`` call: the Store it read and what it saw."""

    path: Path
    readonly: bool
    n: int
    #: ``len(connects)`` when the count was taken (every connection opened before it).
    connects_before: int


@dataclass
class Run:
    cfg: smoke.SmokeConfig
    ds: smoke.TinyDataset
    size: int
    curve: list[float]
    out: Path
    first: list[tuple[str, ...]]
    second: list[tuple[str, ...]]
    traps: list[Path]
    roots: list[Path]
    connects: list[str]
    counts: list[Count]


def _trapped(mp: pytest.MonkeyPatch, trap_root: Path) -> tuple[list[Path], list[str]]:
    """Make every implicit environment read raise; return the trap dirs and the connect log."""
    mp.delenv("TERN_HOME", raising=False)
    traps: list[Path] = []
    for var in XDG_VARS:
        trap = trap_root / var.lower()
        mp.setenv(var, str(trap))
        traps.append(trap)

    def boom(*_a: object, **_k: object) -> str:
        raise AssertionError("platformdirs used by a smoke helper")

    for name in PLATFORMDIRS_FNS:
        mp.setattr(platformdirs, name, boom)

    def no_load(*_a: object, **_k: object) -> None:
        raise AssertionError("config.load used by a smoke helper")

    mp.setattr(tern.config, "load", no_load)
    real_resolve = tern.paths.resolve

    def explicit_resolve(env: dict[str, str] | None = None) -> tern.paths.TernPaths:
        if env is None:
            raise AssertionError("paths.resolve() without an env used by a smoke helper")
        return real_resolve(env)

    mp.setattr(tern.paths, "resolve", explicit_resolve)
    connects: list[str] = []
    real_connect = sqlite3.connect

    def spy(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        connects.append(str(database))
        return real_connect(database, *args, **kwargs)

    mp.setattr(sqlite3, "connect", spy)
    return traps, connects


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Run]:
    base = tmp_path_factory.mktemp("smk")
    home = base / "home"
    out = base / "ckpt"
    out.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        traps, connects = _trapped(mp, base / "trap")
        counts: list[Count] = []
        real_count = smoke._message_count

        def count_spy(store: Store) -> int:
            n = real_count(store)
            counts.append(Count(store.path, store.readonly, n, len(connects)))
            return n

        mp.setattr(smoke, "_message_count", count_spy)
        cfg = smoke.smoke_config(FIXTURE, home)
        ds = smoke.build_dataset(cfg)
        size = smoke.dataset_size(ds)
        curve = smoke.train(cfg, ds, out)
        queries = list(smoke.SMOKE_QUERIES)
        first = smoke.predict(cfg, out, queries)
        second = smoke.predict(cfg, out, queries)
    yield Run(cfg, ds, size, curve, out, first, second, traps, [home, out], connects, counts)


# ---- environment ------------------------------------------------------------------------------


def test_the_helpers_never_touch_an_xdg_dir(run: Run) -> None:
    assert run.traps
    for trap in run.traps:
        assert not trap.exists(), trap


def test_every_database_the_helpers_open_is_under_their_roots(run: Run) -> None:
    assert run.connects
    for database in run.connects:
        path = Path(database.removeprefix("file:").split("?", 1)[0])
        assert any(path.is_relative_to(root) for root in run.roots), database


def test_the_config_is_built_in_code_with_tern_home_as_given(run: Run) -> None:
    home = run.cfg.tern_home
    assert run.cfg.paths == tern.paths.resolve({"TERN_HOME": str(home)})
    assert run.ds.index_path == home / "data" / "index.db"
    assert run.cfg.config.embed_model == run.cfg.embedder.model_id == "stub-fh-64-0"
    assert run.cfg.ingest_batch_size == 50
    assert run.cfg.seed == 0


def test_without_a_root_the_config_owns_a_temp_dir_removed_with_it() -> None:
    cfg = smoke.smoke_config(FIXTURE)
    home = cfg.tern_home
    assert home.is_dir()
    assert home.is_relative_to(Path(tempfile.gettempdir()))
    assert cfg.paths.data == home / "data"
    del cfg
    gc.collect()
    assert not home.exists()


def test_a_mailbox_without_a_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(smoke.SmokeError, match="MANIFEST"):
        smoke.smoke_config(tmp_path, tmp_path / "home")


# ---- dataset and train ------------------------------------------------------------------------


def test_dataset_size_is_the_manifests_distinct_messages(run: Run) -> None:
    assert run.size == MANIFEST["distinct_messages"] == run.cfg.distinct_messages
    assert run.ds.report.folders_done == run.ds.report.folders_total > 0


def test_the_curve_is_the_persisted_message_count_after_each_batch(run: Run) -> None:
    distinct = MANIFEST["distinct_messages"]
    assert run.curve[0] == 1.0
    assert run.curve[-1] == 0.0
    # the first point is measured before the first batch, then one per BatchReport
    assert len(run.curve) == run.ds.report.batches + 1 >= 5
    assert all(a > b for a, b in zip(run.curve, run.curve[1:], strict=False))
    for point in run.curve:
        indexed = round((1.0 - point) * distinct)
        assert 1.0 - indexed / distinct == point
    # the fixture's curve (plan 05 Facts: batch_size=50 -> 4 batches)
    assert run.curve == pytest.approx([1.0, 0.727, 0.459, 0.197, 0.0], abs=1e-3)


def test_the_curve_is_read_through_a_read_only_connection_to_the_trained_index(run: Run) -> None:
    index = run.out / "index.db"
    ro_uri = "file:" + quote(str(index.absolute()), safe="/") + "?mode=ro"
    assert ro_uri in run.connects
    opened_at = run.connects.index(ro_uri)
    trained = [c for c in run.counts if c.path == index]
    # one count before the first batch (an empty index), then one per batch, each through a
    # read-only Store opened before it, and each one is the curve point at that position
    assert len(trained) == len(run.curve)
    assert trained[0].n == 0
    assert all(c.readonly and c.connects_before > opened_at for c in trained)
    distinct = MANIFEST["distinct_messages"]
    assert run.curve == [1.0 - c.n / distinct for c in trained]


class _PaddedStub(StubEmbedder):
    """The stub's id, but document vectors 8 zeros longer than its ``dim`` (a mis-registered
    vec table: the probe registers dim + 8)."""

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [v + [0.0] * 8 for v in await super().embed_documents(texts)]


def test_train_refuses_a_vec_table_of_another_dim_than_the_stub(run: Run, tmp_path: Path) -> None:
    padded = _PaddedStub(dim=smoke.STUB_DIM, seed=run.cfg.seed)
    assert padded.model_id == run.cfg.embedder.model_id
    cfg = dataclasses.replace(run.cfg, embedder=padded)
    with pytest.raises(smoke.SmokeError, match=r"dim 72\), the stub \(stub-fh-64-0, dim 64"):
        smoke.train(cfg, run.ds, tmp_path)


def test_train_embeds_every_chunk_with_the_stub(run: Run) -> None:
    with Store.open(run.out / "index.db", readonly=True) as store:
        model = vec.current(store.connection)
        assert model is not None
        assert (model.model_id, model.dim) == (run.cfg.embedder.model_id, 64)
        assert vec.pending_count(store.connection) == 0
        n_chunks = store.connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        n_vec = store.connection.execute("SELECT count(*) FROM chunks_vec").fetchone()[0]
        assert n_chunks == n_vec > 0
        statuses = {r[0] for r in store.connection.execute("SELECT status FROM ingest_state")}
        assert statuses == {"done"}


def test_train_and_build_refuse_an_existing_index(run: Run) -> None:
    with pytest.raises(smoke.SmokeError, match="already"):
        smoke.train(run.cfg, run.ds, run.out)
    with pytest.raises(smoke.SmokeError, match="already exists"):
        smoke.build_dataset(run.cfg)


# ---- predict ----------------------------------------------------------------------------------


def test_predict_is_deterministic_and_returns_five_mids_per_query(run: Run) -> None:
    assert run.first == run.second
    assert len(run.first) == len(smoke.SMOKE_QUERIES) == 3
    for mids in run.first:
        assert len(mids) == smoke.PREDICT_K == 5
        assert len(set(mids)) == 5


def test_predict_finds_the_invoice_and_the_greek_meeting(run: Run) -> None:
    acme, _german, greek = run.first
    assert set(acme[:2]) == {MID["inv-0042"], MID["inv-0042-reminder"]}
    assert set(greek[:2]) == {MID["meeting-thursday-el"], MID["meeting-thursday-el-re"]}


@pytest.mark.asyncio
async def test_the_german_query_has_no_fts_hit_so_its_hits_are_vector_only(run: Run) -> None:
    with Store.open(run.out / "index.db", readonly=True) as store:
        result = await SearchService(store, None, mode="fts").search(smoke.SMOKE_QUERIES[1])
    assert result.hits == ()
    assert len(run.first[1]) == 5


def test_predict_refuses_a_bare_string(run: Run) -> None:
    with pytest.raises(TypeError):
        smoke.predict(run.cfg, run.out, "Acme invoice March")


def test_predict_with_a_fresh_config_gives_the_same_mids(
    run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _trapped(monkeypatch, tmp_path / "trap")
    cfg = smoke.smoke_config(FIXTURE, tmp_path / "home2")
    assert smoke.predict(cfg, run.out, list(smoke.SMOKE_QUERIES)) == run.first
