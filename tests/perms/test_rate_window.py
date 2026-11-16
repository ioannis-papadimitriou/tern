"""Plan 06b step 4: the W1 rate window (D2, M2, B-M17).

Each caller (``mcp:<proxy connection id>``, ...) gets at most 500 W1 targets per sliding hour and
all callers together at most 1000, counted from the audit log (executed calls whose escalated
tier is W1) plus in-flight reservations taken with no await between check and reserve. A call
that would exceed either window gets a ``denied``/``rate_limited`` row and nothing runs.

The window cases run twice: at the production limits (500/1000/3600, marked ``slow``: they add
several seconds to ``check-fast``) and as a fast twin at lowered injected limits (5/10/60).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from tern.bridge.protocol import FindParams, Mid
from tern.index.store import Store
from tern.perms.audit import AuditLog
from tern.perms.engine import Engine, Limits
from tern.perms.ratelimit import RateLimited, RateWindow
from tern.testing.fake_bridge import FakeBridge
from tern.tools import BoundaryModel, Effect, Tier, ToolContext, ToolError, tool
from tern.tools.registry import Registry, ToolResult

from .conftest import FIXTURE, Clock, mids

RAN: list[str] = []


class MidsIn(BoundaryModel):
    mids: list[Mid]


class NoMidsIn(BoundaryModel):
    name: str


class Out(BoundaryModel):
    n: int


@tool(name="touch", tier=Tier.W1, effects={Effect.MAIL_META}, untrusted_output=False)
async def touch(ctx: ToolContext, inp: MidsIn) -> Out:
    """A W1 dummy: runs on its engine-resolved targets, writes nothing."""
    RAN.append("touch")
    return Out(n=sum(len(locs) for locs in ctx.targets.values()))


@tool(
    name="no_targets",
    tier=Tier.W1,
    effects={Effect.MAIL_META},
    untrusted_output=False,
    count_targets=lambda inp: 0,
)
async def no_targets(ctx: ToolContext, inp: NoMidsIn) -> Out:
    """A W1 dummy without target mids (a create-tag shape reporting 0 targets)."""
    RAN.append("no_targets")
    return Out(n=0)


TOOLS = [touch, no_targets]

LOW = Limits(w1_per_caller=5, w1_global=10, window_s=60)


@dataclass(frozen=True)
class Size:
    limits: Limits

    @property
    def per(self) -> int:
        return self.limits.w1_per_caller

    @property
    def total(self) -> int:
        return self.limits.w1_global

    @property
    def window(self) -> int:
        return self.limits.window_s


SIZES = [
    pytest.param(Size(Limits()), id="production", marks=pytest.mark.slow),
    pytest.param(Size(LOW), id="lowered"),
]


class Yielding:
    """The FakeBridge behind a suspension point: every request first yields to the event loop
    (as a real extension round trip does), so concurrent calls interleave during target
    resolution."""

    def __init__(self, fb: FakeBridge) -> None:
        self.fb = fb

    async def request(self, method: str, params: BaseModel) -> Any:
        await asyncio.sleep(0)
        return await self.fb.request(method, params)

    async def notify(self, method: str, params: BaseModel) -> None:
        await self.fb.notify(method, params)


@pytest.fixture(autouse=True)
def _reset() -> None:
    RAN.clear()


@pytest.fixture
def fb() -> FakeBridge:
    return FakeBridge(FIXTURE)


@pytest.fixture
def writable(fixture_store: Store) -> list[str]:
    """Indexed mids a W1 call can target (a Message-ID, not conflicted), in mid order."""
    rows = fixture_store.connection.execute(
        "SELECT mid FROM messages WHERE mid NOT LIKE 'x-%' AND conflicted = 0 "
        "AND header_message_id IS NOT NULL ORDER BY mid LIMIT 25"
    ).fetchall()
    assert len(rows) == 25
    return [str(r[0]) for r in rows]


def make(make_registry: Any, fixture_store: Store, bridge: Any, limits: Limits) -> Registry:
    return make_registry(TOOLS, bridge=bridge, index=fixture_store, limits=limits)


def engine_of(reg: Registry) -> Engine:
    rt = reg._runtime  # pyright: ignore[reportPrivateUsage] -- the test reads the engine it built
    assert rt is not None and rt.engine is not None
    return rt.engine


Call = tuple[str, str | None, str, dict[str, Any]]


async def run(reg: Registry, calls: Sequence[Call]) -> list[ToolResult | ToolError]:
    """The calls one after another; each outcome is its result or its ToolError."""
    out: list[ToolResult | ToolError] = []
    for caller, label, name, args in calls:
        try:
            out.append(await reg.call(caller, name, args, caller_label=label))
        except ToolError as exc:
            out.append(exc)
    return out


def outcome(r: ToolResult | ToolError) -> str:
    return "ok" if isinstance(r, ToolResult) else f"{r.code}/{r.reason}"


def touch_one(caller: str, mid: str, label: str | None = None) -> Call:
    return (caller, label, "touch", {"mids": [mid]})


def seed(
    audit: AuditLog,
    caller: str,
    targets: int,
    *,
    tier: Tier = Tier.W1,
    executed: bool = True,
    label: str | None = None,
) -> None:
    """One audit row as an earlier call (this ternd's or a previous run's) left it."""
    audit.append_call(
        caller=caller,
        caller_label=label,
        tool="touch",
        tier=tier,
        decision="auto" if executed else "denied",
        reason=None if executed else "rate_limited",
        args={"mids": []},
        args_validated=True,
        target_count=targets,
        initial_status="tb_accepted" if executed else None,
    )


def w1_used(audit: AuditLog, caller: str | None = None) -> int:
    return audit.target_total(caller=caller, tier=Tier.W1, executed=True)


# ---- the window through Registry.call (production size: slow; lowered: fast twin) ---------------


@pytest.mark.parametrize("size", SIZES)
def test_one_connection_is_capped_at_its_window(
    size: Size,
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    reg = make(make_registry, fixture_store, fb, size.limits)
    extra = max(size.per // 5, 1)  # 600 calls at 500: the last 100 denied
    calls = [touch_one("mcp:conn-1", writable[0]) for _ in range(size.per + extra)]

    got = [outcome(r) for r in asyncio.run(run(reg, calls))]

    assert got == ["ok"] * size.per + ["rate_limited/caller_window"] * extra
    assert len(RAN) == size.per
    assert w1_used(audit, "mcp:conn-1") == size.per
    denied = [r for r in audit.list(limit=None) if r.decision == "denied"]
    assert len(denied) == extra
    assert {(r.tier, r.reason, r.initial_status, r.target_count) for r in denied} == {
        (Tier.W1, "rate_limited", None, 1)
    }
    assert engine_of(reg).rate_window.reserved() == 0


@pytest.mark.parametrize("size", SIZES)
def test_a_second_connection_is_unaffected_by_the_first(
    size: Size,
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    reg = make(make_registry, fixture_store, fb, size.limits)
    seed(audit, "mcp:conn-1", size.per)

    got = asyncio.run(
        run(reg, [touch_one("mcp:conn-1", writable[0]), touch_one("mcp:conn-2", writable[0])])
    )

    assert [outcome(r) for r in got] == ["rate_limited/caller_window", "ok"]
    assert w1_used(audit, "mcp:conn-2") == 1


@pytest.mark.parametrize("size", SIZES)
def test_rotating_client_names_over_three_connections_stop_at_the_global_ceiling(
    size: Size,
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    reg = make(make_registry, fixture_store, fb, size.limits)
    extra = max(size.total // 10, 1)  # 1100 calls at 1000: the last 100 denied
    n = size.total + extra
    assert -(-n // 3) <= size.per  # no connection reaches its own window
    calls = [
        touch_one(f"mcp:conn-{i % 3}", writable[i % len(writable)], label=f"client-{i}")
        for i in range(n)
    ]

    got = [outcome(r) for r in asyncio.run(run(reg, calls))]

    assert got == ["ok"] * size.total + ["rate_limited/global_window"] * extra
    assert w1_used(audit) == size.total
    labels = {r.caller_label for r in audit.list(limit=None)}
    assert len(labels) == n  # every call had its own client name


@pytest.mark.parametrize("size", SIZES)
def test_concurrent_calls_on_one_connection_run_exactly_the_window(
    size: Size,
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    bridge = Yielding(fb)
    reg = make(make_registry, fixture_store, bridge, size.limits)
    extra = max(size.per // 5, 1)
    n = size.per + extra
    in_flight: list[tuple[int, int]] = []

    async def go() -> list[ToolResult | ToolError]:
        async def one(mid: str) -> ToolResult | ToolError:
            try:
                return await reg.call("mcp:conn-1", "touch", {"mids": [mid]})
            except ToolError as exc:
                return exc

        tasks = [asyncio.ensure_future(one(writable[i % len(writable)])) for i in range(n)]
        await asyncio.sleep(0)  # every call has passed its check; none has written its row
        in_flight.append((engine_of(reg).rate_window.reserved("mcp:conn-1"), audit.count()))
        return list(await asyncio.gather(*tasks))

    got = [outcome(r) for r in asyncio.run(go())]

    # Mid-flight: the window's targets reserved, and the only rows are the denials'.
    assert in_flight == [(size.per, extra)]
    assert got.count("ok") == size.per
    assert got.count("rate_limited/caller_window") == extra
    assert len(RAN) == size.per and w1_used(audit, "mcp:conn-1") == size.per
    assert engine_of(reg).rate_window.reserved() == 0


@pytest.mark.parametrize("size", SIZES)
def test_the_window_slides(
    size: Size,
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    clock: Clock,
    writable: list[str],
) -> None:
    reg = make(make_registry, fixture_store, fb, size.limits)
    calls = [touch_one("mcp:conn-1", writable[0]) for _ in range(size.per)]
    assert {outcome(r) for r in asyncio.run(run(reg, calls))} == {"ok"}
    one = [touch_one("mcp:conn-1", writable[0])]

    clock.t += size.window  # the first calls are exactly one window old: still counted
    assert [outcome(r) for r in asyncio.run(run(reg, one))] == ["rate_limited/caller_window"]
    clock.t += 1  # 3601 s later
    assert [outcome(r) for r in asyncio.run(run(reg, one))] == ["ok"]


# ---- what counts ---------------------------------------------------------------------------------


def test_the_production_limits_are_pinned(
    make_registry: Any, fixture_store: Store, fb: FakeBridge
) -> None:
    limits = Limits()
    assert (limits.w1_per_caller, limits.w1_global, limits.window_s) == (500, 1000, 3600)
    reg = make_registry(TOOLS, bridge=fb, index=fixture_store)  # no limits passed
    assert engine_of(reg).limits == limits


def test_only_executed_w1_targets_count(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    reg = make(make_registry, fixture_store, fb, LOW)
    seed(audit, "mcp:conn-1", 21, tier=Tier.W2)  # escalated: never in the W1 window
    seed(audit, "mcp:conn-1", 21, tier=Tier.W3)
    seed(audit, "mcp:conn-1", 20, executed=False)  # a refused W1 call ran nothing
    seed(audit, "mcp:conn-1", LOW.w1_per_caller - 1)

    got = asyncio.run(run(reg, [touch_one("mcp:conn-1", writable[0])] * 2))

    assert [outcome(r) for r in got] == ["ok", "rate_limited/caller_window"]


def test_multi_target_calls_count_their_targets(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    limits = Limits(w1_per_caller=30, w1_global=100, window_s=60)
    reg = make(make_registry, fixture_store, fb, limits)
    calls: list[Call] = [
        ("mcp:conn-1", None, "touch", {"mids": writable[:20]}),
        ("mcp:conn-1", None, "touch", {"mids": writable[:11]}),  # 31 > 30: refused whole
        ("mcp:conn-1", None, "touch", {"mids": writable[:10]}),  # exactly 30
    ]
    got = [outcome(r) for r in asyncio.run(run(reg, calls))]
    assert got == ["ok", "rate_limited/caller_window", "ok"]
    assert w1_used(audit, "mcp:conn-1") == 30


def test_the_window_survives_a_restart(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    first = make(make_registry, fixture_store, fb, LOW)
    calls = [touch_one("mcp:conn-1", writable[0])] * LOW.w1_per_caller
    assert {outcome(r) for r in asyncio.run(run(first, calls))} == {"ok"}

    restarted = make(make_registry, fixture_store, fb, LOW)  # a new engine over the same log
    got = asyncio.run(run(restarted, [touch_one("mcp:conn-1", writable[0])]))
    assert [outcome(r) for r in got] == ["rate_limited/caller_window"]


def test_the_caller_label_is_not_part_of_the_key(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    reg = make(make_registry, fixture_store, fb, LOW)
    seed(audit, "mcp:conn-1", LOW.w1_per_caller, label="client-a")
    got = asyncio.run(run(reg, [touch_one("mcp:conn-1", writable[0], label="client-b")]))
    assert [outcome(r) for r in got] == ["rate_limited/caller_window"]


def test_every_caller_kind_has_its_own_window(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    limits = Limits(w1_per_caller=1, w1_global=10, window_s=60)
    reg = make(make_registry, fixture_store, fb, limits)
    callers = ["agent:s1", "mcp:c1", "job:triage", "ui"]
    calls = [touch_one(c, writable[0]) for c in callers for _ in range(2)]
    got = [outcome(r) for r in asyncio.run(run(reg, calls))]
    assert got == ["ok", "rate_limited/caller_window"] * len(callers)


def test_a_rate_limited_call_is_audited_and_runs_nothing(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    reg = make(make_registry, fixture_store, fb, LOW)
    seed(audit, "mcp:conn-1", LOW.w1_per_caller)

    with pytest.raises(ToolError) as info:
        asyncio.run(reg.call("mcp:conn-1", "touch", {"mids": [writable[0]]}))

    assert (info.value.code, info.value.reason) == ("rate_limited", "caller_window")
    assert info.value.message == "W1 rate window: at most 5 W1 targets per 60 s for this caller"
    assert RAN == [] and fb.calls == []  # refused before target resolution
    rec = audit.list(limit=1)[0]
    assert (rec.tool, rec.tier, rec.decision, rec.reason, rec.initial_status) == (
        "touch",
        Tier.W1,
        "denied",
        "rate_limited",
        None,
    )
    assert rec.target_mids == (writable[0],) and rec.target_count == 1
    assert rec.args == {"mids": [writable[0]]}


def test_a_refused_target_releases_its_reservation(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    reg = make(make_registry, fixture_store, fb, LOW)
    unknown = [touch_one("mcp:conn-1", mids(1)[0])] * (LOW.w1_per_caller + 1)
    got = asyncio.run(run(reg, unknown))
    assert {outcome(r) for r in got} == {"unknown_mid/not_indexed"}
    assert engine_of(reg).rate_window.reserved() == 0 and w1_used(audit) == 0

    ok = [touch_one("mcp:conn-1", writable[0])] * LOW.w1_per_caller
    assert {outcome(r) for r in asyncio.run(run(reg, ok))} == {"ok"}


def test_a_cancelled_call_releases_its_reservation(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    gate = asyncio.Event()

    class Hanging(Yielding):
        async def request(self, method: str, params: BaseModel) -> Any:
            if isinstance(params, FindParams):
                await gate.wait()  # never set: the live find hangs until cancelled
            return await super().request(method, params)

    reg = make(make_registry, fixture_store, Hanging(fb), LOW)
    window = engine_of(reg).rate_window

    async def go() -> int:
        task = asyncio.ensure_future(reg.call("mcp:conn-1", "touch", {"mids": [writable[0]]}))
        await asyncio.sleep(0)
        held = window.reserved("mcp:conn-1")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return held

    assert asyncio.run(go()) == 1
    assert window.reserved() == 0
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status) == ("denied", "cancelled", None)


def test_a_w1_call_without_targets_adds_nothing_but_is_refused_when_full(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
) -> None:
    reg = make(make_registry, fixture_store, fb, LOW)
    seed(audit, "mcp:conn-1", LOW.w1_per_caller - 1)
    calls: list[Call] = [("mcp:conn-1", None, "no_targets", {"name": "synthetic"})] * 3
    assert {outcome(r) for r in asyncio.run(run(reg, calls))} == {"ok"}
    assert w1_used(audit, "mcp:conn-1") == LOW.w1_per_caller - 1

    seed(audit, "mcp:conn-1", 1)
    got = asyncio.run(run(reg, calls[:1]))
    assert [outcome(r) for r in got] == ["rate_limited/caller_window"]


def test_overlapping_zero_target_calls_on_one_connection_all_run(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review fix (step 4 round 1): a 0-target reservation left a 0 entry that an overlapping
    # release deleted, so the next release raised KeyError out of Registry.call. Today a 0-target
    # call's resolution never suspends; the patch gives it the suspension point a later change
    # (or a real bridge round trip) may add, so the calls overlap between reserve and release:
    # the 0-target calls take longer than ``touch``'s one bridge round trip, so ``touch`` releases
    # (and, before the fix, deleted the caller's entry) while both are still in flight.
    real = Engine.find_live

    async def yielding(self: Engine, resolved: Any) -> Any:
        for _ in range(3 if not resolved else 0):
            await asyncio.sleep(0)
        return await real(self, resolved)

    monkeypatch.setattr(Engine, "find_live", yielding)
    reg = make(make_registry, fixture_store, Yielding(fb), LOW)

    async def go() -> list[ToolResult]:
        return list(
            await asyncio.gather(
                reg.call("mcp:conn-1", "no_targets", {"name": "synthetic"}),
                reg.call("mcp:conn-1", "no_targets", {"name": "synthetic"}),
                reg.call("mcp:conn-1", "touch", {"mids": [writable[0]]}),
            )
        )

    got = asyncio.run(go())

    assert [outcome(r) for r in got] == ["ok", "ok", "ok"]
    assert sorted(RAN) == ["no_targets", "no_targets", "touch"]
    assert engine_of(reg).rate_window.reserved() == 0
    assert w1_used(audit, "mcp:conn-1") == 1


def test_an_audit_read_failure_is_internal_with_a_row(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = make(make_registry, fixture_store, fb, LOW)

    def broken(self: AuditLog, *args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(AuditLog, "target_total", broken)
    with pytest.raises(ToolError) as info:
        asyncio.run(reg.call("mcp:conn-1", "touch", {"mids": [writable[0]]}))
    monkeypatch.undo()

    assert (info.value.code, info.value.reason) == ("internal", "rate_window")
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert RAN == [] and fb.calls == []
    (rec,) = audit.list()
    assert (rec.decision, rec.reason, rec.initial_status) == ("error", "internal", None)
    assert engine_of(reg).rate_window.reserved() == 0


def test_w2_calls_are_not_checked_against_the_window(
    make_registry: Any,
    fixture_store: Store,
    fb: FakeBridge,
    audit: AuditLog,
    writable: list[str],
) -> None:
    reg = make(make_registry, fixture_store, fb, LOW)
    seed(audit, "mcp:conn-1", LOW.w1_global)
    got = asyncio.run(run(reg, [("mcp:conn-1", None, "touch", {"mids": writable[:21]})]))
    # 21 targets escalate to W2, which needs an approval (no approval UI here): never rate_limited.
    assert [outcome(r) for r in got] == ["unavailable/no_approval_ui"]


# ---- RateWindow ----------------------------------------------------------------------------------


@pytest.fixture
def window(audit: AuditLog, clock: Clock) -> RateWindow:
    return RateWindow(audit, clock, per_caller=3, global_limit=5, window_s=60)


def test_reservations_count_until_released_once(window: RateWindow) -> None:
    a = window.reserve("mcp:a", 2)
    b = window.reserve("mcp:b", 3)
    assert (window.reserved("mcp:a"), window.reserved("mcp:b"), window.reserved()) == (2, 3, 5)
    with pytest.raises(RateLimited) as info:
        window.reserve("mcp:c", 1)
    assert (info.value.scope, info.value.reason) == ("global", "global_window")
    with pytest.raises(RateLimited) as info:
        window.reserve("mcp:a", 2)
    assert info.value.scope == "caller"
    a.release()
    a.release()  # idempotent: gives back 2 once
    assert a.released and (window.reserved("mcp:a"), window.reserved()) == (0, 3)
    b.release()
    assert window.reserved() == 0
    window.reserve("mcp:a", 3)


@pytest.mark.parametrize("order", ["zero_last", "zero_first"])
def test_a_zero_target_reservation_releases_in_any_order(window: RateWindow, order: str) -> None:
    zero = window.reserve("mcp:a", 0)
    two = window.reserve("mcp:a", 2)
    assert (window.reserved("mcp:a"), window.reserved()) == (2, 2)
    first, last = (two, zero) if order == "zero_last" else (zero, two)
    first.release()
    last.release()
    assert (window.reserved("mcp:a"), window.reserved()) == (0, 0)
    window.reserve("mcp:a", 3)  # nothing left over: the whole caller window is free


def test_overlapping_zero_target_reservations_release_cleanly(window: RateWindow) -> None:
    a = window.reserve("mcp:a", 0)
    b = window.reserve("mcp:a", 0)
    a.release()
    b.release()
    assert (window.reserved("mcp:a"), window.reserved()) == (0, 0)


def test_reserve_reads_the_clock_once(audit: AuditLog, clock: Clock) -> None:
    reads: list[float] = []

    def counting() -> float:
        reads.append(clock())
        return reads[-1]

    w = RateWindow(audit, counting, per_caller=3, global_limit=5, window_s=60)
    w.reserve("mcp:a", 1)
    # Both windows (caller and global) start at the same instant.
    assert len(reads) == 1


def test_a_refused_reservation_reserves_nothing(window: RateWindow) -> None:
    window.reserve("mcp:a", 3)
    with pytest.raises(RateLimited):
        window.reserve("mcp:a", 1)
    assert window.reserved() == 3


@pytest.mark.parametrize(
    ("caller", "targets"),
    [("mcp:a", -1), ("mcp:a", True), ("mcp:a", 1.0), ("nobody", 1), ("mcp:", 1)],
)
def test_reserve_refuses_bad_arguments(window: RateWindow, caller: Any, targets: Any) -> None:
    with pytest.raises(ValueError):
        window.reserve(caller, targets)
    assert window.reserved() == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"per_caller": 0, "global_limit": 1, "window_s": 1},
        {"per_caller": 1, "global_limit": True, "window_s": 1},
        {"per_caller": 1, "global_limit": 1, "window_s": 0.5},
    ],
)
def test_rate_window_refuses_bad_limits(
    audit: AuditLog, clock: Clock, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        RateWindow(audit, clock, **kwargs)
