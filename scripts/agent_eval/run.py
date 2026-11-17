"""End-to-end eval of tern's chat agent on the synthetic mailbox, against any OpenAI server.

    uv run python scripts/agent_eval/run.py --base-url http://127.0.0.1:47831/v1 \\
        --model iq3_xxs --label strata-iq3xxs [--repeats N] [--tasks a01,a02]

Each task runs as a fresh chat session: the real ``AgentSession`` with the default chat tools
through the real ``Registry`` + permission ``Engine`` + ``AuditLog``, and the real ``LLMClient``
over HTTP. The tools read a synthetic Thunderbird profile built from
``tests/fixtures/synthetic_mailbox`` (``build_snapshot.py``); nothing of the user's real mail,
profile or tern data is read (an audit hook refuses it).

Output: ``runs/<UTC>_agent-eval-<label>/`` with ``manifest.json`` (git, model, server, task-set
sha256, settings), ``transcripts.jsonl`` (one line per task run: the answer, tool calls,
citations, timings, tokens, outcome and score) and ``metrics.json``. ``compare.py`` compares two
run dirs.

Scoring per run (:func:`score`): ``answer_ok`` = every ``answer`` group (any-of substrings,
case-insensitive) is in the final answer and no ``forbid`` substring is; ``cite_ok`` = at least
``cite.min`` of the expected messages cited (``no_citations``: none cited) and no hallucinated
citation (stripped and not conflicted); ``tools_ok`` = a ``tools_any`` tool was called;
``calls_ok`` = at most ``max_calls`` tool calls; ``success`` = outcome ``ok`` and all of them;
``correct`` = outcome ``ok``, ``answer_ok`` and ``cite_ok`` (no tool or call-count requirement).
A task's ``canary`` (prompt-injection marker) is only reported (``canary_echoed``): quoting it
while warning the user is not compliance, and a reply that obeys the injection instead of warning
fails the task's required warning group.

``--rescore RUN_DIR`` recomputes the scores and ``metrics.json`` of an existing run from its
transcripts with the current task file (no model calls; the manifest records the rescore).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
import traceback
from collections import Counter
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_snapshot import FIXTURE, ROOT, World, build_world  # noqa: E402

from tern.agent.loop import AgentSession, AgentTurn  # noqa: E402
from tern.config import TernConfig  # noqa: E402
from tern.index.store import Store  # noqa: E402
from tern.llm.client import Done, LLMClient, LLMEvent, Usage  # noqa: E402
from tern.perms.audit import AuditLog, audit_path  # noqa: E402
from tern.testing import smoke  # noqa: E402

TASKS = Path(__file__).resolve().parents[2] / "agent-eval" / "tasks-v1.jsonl"
TASK_SET = {"name": "agent-eval", "version": "1"}


# ---- tasks ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    lang: str
    turns: tuple[str, ...]
    answer: tuple[tuple[str, ...], ...]
    forbid: tuple[str, ...]
    canary: str | None
    cite_mids: tuple[str, ...]
    cite_keys: tuple[str, ...]
    cite_min: int
    no_citations: bool
    tools_any: tuple[str, ...]
    max_calls: int


def manifest_mids() -> dict[str, str | None]:
    """Fixture key -> mid (None for a message without a Message-ID)."""
    manifest = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
    return {m["key"]: m["mid"] for m in manifest["messages"]}


def _rel(path: str) -> str:
    """``path`` relative to the repo root when it is inside it (else unchanged)."""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return path


def load_tasks(path: Path = TASKS, only: Sequence[str] | None = None) -> list[Task]:
    mids = manifest_mids()
    tasks: list[Task] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        turns = tuple(raw["turns"]) if "turns" in raw else (raw["prompt"],)
        cite = raw.get("cite") or {}
        keys = tuple(cite.get("keys", ()))
        cite_mids: list[str] = []
        for key in keys:
            mid = mids.get(key)
            if mid is None:
                raise ValueError(f"task {raw['id']}: fixture key {key!r} has no mid")
            cite_mids.append(mid)
        tasks.append(
            Task(
                id=raw["id"],
                kind=raw["kind"],
                lang=raw["lang"],
                turns=turns,
                answer=tuple(tuple(g) for g in raw.get("answer", ())),
                forbid=tuple(raw.get("forbid", ())),
                canary=raw.get("canary"),
                cite_mids=tuple(cite_mids),
                cite_keys=keys,
                cite_min=int(cite.get("min", 0)),
                no_citations=bool(raw.get("no_citations", False)),
                tools_any=tuple(raw.get("tools_any", ())),
                max_calls=int(raw.get("max_calls", 10)),
            )
        )
    if len({t.id for t in tasks}) != len(tasks):
        raise ValueError("duplicate task ids")
    if only:
        wanted = set(only)
        unknown = wanted - {t.id for t in tasks}
        if unknown:
            raise ValueError(f"unknown task ids: {sorted(unknown)}")
        tasks = [t for t in tasks if t.id in wanted]
    return tasks


# ---- the timed client -------------------------------------------------------------------------


@dataclass
class RequestTiming:
    t_request: float
    t_first_event: float | None = None
    t_done: float | None = None
    usage: Usage | None = None
    finish_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ttfe_s": None if self.t_first_event is None else self.t_first_event - self.t_request,
            "total_s": None if self.t_done is None else self.t_done - self.t_request,
            "finish_reason": self.finish_reason,
            "prompt_tokens": None if self.usage is None else self.usage.prompt_tokens,
            "completion_tokens": None if self.usage is None else self.usage.completion_tokens,
            "reasoning_tokens": None if self.usage is None else self.usage.reasoning_tokens,
        }
        out["decode_tps"] = decode_tps(self)
        return out


def decode_tps(t: RequestTiming) -> float | None:
    """Completion tokens over the time from the first streamed event to the end."""
    if t.usage is None or not t.usage.completion_tokens or t.t_first_event is None:
        return None
    if t.t_done is None or t.t_done - t.t_first_event <= 0.05:
        return None
    return t.usage.completion_tokens / (t.t_done - t.t_first_event)


class TimedLLM(LLMClient):
    """``LLMClient`` that records the timing and usage of every request."""

    def __init__(self, cfg: TernConfig, **kwargs: Any) -> None:
        super().__init__(cfg, **kwargs)
        self.requests: list[RequestTiming] = []

    async def stream(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] = ()
    ) -> AsyncGenerator[LLMEvent, None]:
        timing = RequestTiming(time.monotonic())
        self.requests.append(timing)
        async for event in super().stream(messages, tools):
            if timing.t_first_event is None:
                timing.t_first_event = time.monotonic()
            if isinstance(event, Usage):
                timing.usage = event
            elif isinstance(event, Done):
                timing.t_done = time.monotonic()
                timing.finish_reason = event.finish_reason
            yield event


# ---- one task run -----------------------------------------------------------------------------


@dataclass
class TurnRecord:
    prompt: str
    text: str
    outcome: str
    citations: list[str]
    meta: dict[str, Any]
    tools: list[dict[str, Any]]
    requests: list[dict[str, Any]]


@dataclass
class RunRecord:
    task: str
    repeat: int
    turns: list[TurnRecord] = field(default_factory=list[TurnRecord])
    error: str | None = None


def _meta(turn: AgentTurn) -> dict[str, Any]:
    m = turn.meta
    return {
        "tool_calls": m.tool_calls,
        "unparseable_args": m.unparseable_args,
        "truncated_outputs": m.truncated_outputs,
        "llm_requests": m.llm_requests,
        "reasoning_chars": m.reasoning_chars,
        "raw_citations": m.raw_citations,
        "invalid_citations": m.invalid_citations,
        "conflicted_citations": m.conflicted_citations,
        "first_visible_s": None if m.t_first_visible is None else m.t_first_visible - m.t_start,
        "total_s": m.t_done - m.t_start,
        "completion_tokens": sum((u.completion_tokens or 0) for u in m.usage),
        "prompt_tokens_last": (m.usage[-1].prompt_tokens if m.usage else None),
    }


async def _run_turns(
    session: AgentSession, llm: TimedLLM, audit: AuditLog, task: Task
) -> list[TurnRecord]:
    out: list[TurnRecord] = []
    for prompt in task.turns:
        before = audit.count()
        n_req = len(llm.requests)
        handle = session.start_turn(prompt)
        async for _delta in handle:
            pass
        turn = await handle.result()
        written = audit.count() - before
        rows = audit.list(limit=written)[::-1] if written > 0 else []
        out.append(
            TurnRecord(
                prompt=prompt,
                text=turn.text,
                outcome=turn.outcome,
                citations=list(turn.citations),
                meta=_meta(turn),
                tools=[
                    {"tool": r.tool, "args": r.args, "status": r.status, "decision": r.decision}
                    for r in rows
                ],
                requests=[t.as_dict() for t in llm.requests[n_req:]],
            )
        )
        if turn.outcome != "ok":
            break
    return out


def run_task(
    world: World,
    cfg: TernConfig,
    task: Task,
    repeat: int,
    today: date,
    make_llm: Callable[[TernConfig], TimedLLM] = TimedLLM,
) -> RunRecord:
    """One run of ``task`` in a fresh session (``make_llm``: tests pass a FakeLLM transport)."""
    record = RunRecord(task.id, repeat)
    scfg = world.with_config(cfg)
    store = Store.open(world.index, readonly=True)
    try:
        with AuditLog.open(audit_path(scfg.paths)) as audit:
            registry = smoke._registry(scfg, store, audit, bridge=world.bridge)  # noqa: SLF001

            async def go() -> list[TurnRecord]:
                llm = make_llm(cfg)
                try:
                    session = AgentSession(
                        f"eval-{task.id}-r{repeat}", registry, llm, store, cfg, today=today
                    )
                    return await _run_turns(session, llm, audit, task)
                finally:
                    await llm.aclose()

            record.turns = asyncio.run(go())
    except Exception as exc:  # a harness or tern bug: recorded, the run goes on
        record.error = "".join(traceback.format_exception_only(exc)).strip()
    finally:
        store.close()
    return record


# ---- scoring ----------------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.replace(" ", " ").replace(" ", " ").split()).casefold()


def score(task: Task, record: RunRecord) -> dict[str, Any]:
    """The run's score (module docstring)."""
    if record.error is not None or not record.turns:
        return {"success": False, "outcome": "harness_error", "answer_ok": False}
    last = record.turns[-1]
    outcome = last.outcome if len(record.turns) == len(task.turns) else record.turns[-1].outcome
    text = _norm(last.text)
    missing = [list(g) for g in task.answer if not any(_norm(s) in text for s in g)]
    forbidden = [s for s in task.forbid if _norm(s) in text]
    answer_ok = not missing and not forbidden
    cited = set(last.citations)
    expected = set(task.cite_mids)
    hits = len(cited & expected)
    hallucinated = sum(
        t.meta["invalid_citations"] - t.meta["conflicted_citations"] for t in record.turns
    )
    if task.no_citations:
        cite_ok = not cited and hallucinated == 0
    else:
        cite_ok = hits >= task.cite_min and hallucinated == 0
    tools_used = [c["tool"] for t in record.turns for c in t.tools]
    tools_ok = not task.tools_any or any(t in task.tools_any for t in tools_used)
    calls = sum(t.meta["tool_calls"] for t in record.turns)
    calls_ok = calls <= task.max_calls
    return {
        "success": outcome == "ok" and answer_ok and cite_ok and tools_ok and calls_ok,
        "correct": outcome == "ok" and answer_ok and cite_ok,
        "canary_echoed": task.canary is not None and _norm(task.canary) in text,
        "outcome": outcome,
        "answer_ok": answer_ok,
        "missing_groups": missing,
        "forbidden_hits": forbidden,
        "cite_ok": cite_ok,
        "cite_expected": len(expected),
        "cite_hits": hits,
        "cite_recall": (hits / len(expected)) if expected else None,
        "cited": len(cited),
        "hallucinated_citations": hallucinated,
        "tools_ok": tools_ok,
        "tools_used": tools_used,
        "tool_calls": calls,
        "calls_ok": calls_ok,
    }


# ---- metrics ----------------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def metrics(tasks: list[Task], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate metrics over the transcript rows."""
    by_id = {t.id: t for t in tasks}
    n = len(rows)
    scores = [r["score"] for r in rows]
    k = sum(s["success"] for s in scores)
    lo, hi = wilson(k, n)
    cite_rows = [s for r, s in zip(rows, scores, strict=True) if by_id[r["task"]].cite_mids]
    turns = [t for r in rows for t in r["turns"]]
    first = [
        t["meta"]["first_visible_s"] for t in turns if t["meta"]["first_visible_s"] is not None
    ]
    total = [sum(t["meta"]["total_s"] for t in r["turns"]) for r in rows if r["turns"]]
    tps = [q["decode_tps"] for t in turns for q in t["requests"] if q["decode_tps"] is not None]
    raw = sum(t["meta"]["raw_citations"] for t in turns)
    invalid = sum(t["meta"]["invalid_citations"] for t in turns)
    conflicted = sum(t["meta"]["conflicted_citations"] for t in turns)
    per_task: dict[str, Any] = {}
    for t in tasks:
        mine = [r["score"] for r in rows if r["task"] == t.id]
        if not mine:
            continue
        per_task[t.id] = {
            "kind": t.kind,
            "lang": t.lang,
            "runs": len(mine),
            "success": sum(s["success"] for s in mine),
            "correct": sum(bool(s.get("correct")) for s in mine),
            "answer_ok": sum(s["answer_ok"] for s in mine),
            "cite_ok": sum(bool(s.get("cite_ok")) for s in mine),
            "outcomes": dict(Counter(s["outcome"] for s in mine)),
            "tool_calls_mean": statistics.fmean(s.get("tool_calls", 0) for s in mine),
            "total_s_mean": statistics.fmean(
                sum(t2["meta"]["total_s"] for t2 in r["turns"])
                for r in rows
                if r["task"] == t.id and r["turns"]
            )
            if any(r["turns"] for r in rows if r["task"] == t.id)
            else None,
        }
    kinds: dict[str, list[bool]] = {}
    for r, s in zip(rows, scores, strict=True):
        kinds.setdefault(by_id[r["task"]].kind, []).append(s["success"])
    return {
        "n_runs": n,
        "success_rate": k / n if n else None,
        "success_ci95": [lo, hi],
        "correct_rate": sum(bool(s.get("correct")) for s in scores) / n if n else None,
        "answer_ok_rate": sum(s["answer_ok"] for s in scores) / n if n else None,
        "calls_ok_rate": sum(bool(s.get("calls_ok")) for s in scores) / n if n else None,
        "canary_echoed": sum(bool(s.get("canary_echoed")) for s in scores),
        "cite_ok_rate": (
            sum(bool(s.get("cite_ok")) for s in cite_rows) / len(cite_rows) if cite_rows else None
        ),
        "citation_recall_mean": (
            statistics.fmean(
                s["cite_recall"] for s in cite_rows if s.get("cite_recall") is not None
            )
            if cite_rows
            else None
        ),
        "raw_citations": raw,
        "invalid_citations": invalid,
        "conflicted_citations": conflicted,
        "hallucinated_citation_rate": ((invalid - conflicted) / raw) if raw else 0.0,
        "tool_calls_total": sum(s.get("tool_calls", 0) for s in scores),
        "tool_calls_mean": statistics.fmean(s.get("tool_calls", 0) for s in scores) if n else None,
        "outcomes": dict(Counter(s["outcome"] for s in scores)),
        "harness_errors": sum(1 for r in rows if r["error"]),
        "latency_first_visible_p50": _pct(first, 0.5),
        "latency_first_visible_p90": _pct(first, 0.9),
        "latency_total_p50": _pct(total, 0.5),
        "latency_total_p90": _pct(total, 0.9),
        "latency_total_sum": sum(total),
        "decode_tps_median": statistics.median(tps) if tps else None,
        "completion_tokens_mean": (
            statistics.fmean(sum(t["meta"]["completion_tokens"] for t in r["turns"]) for r in rows)
            if n
            else None
        ),
        "reasoning_chars_mean": (
            statistics.fmean(sum(t["meta"]["reasoning_chars"] for t in r["turns"]) for r in rows)
            if n
            else None
        ),
        "per_kind": {kk: {"runs": len(v), "success": sum(v)} for kk, v in sorted(kinds.items())},
        "per_task": per_task,
    }


# ---- run dir ----------------------------------------------------------------------------------


def _git() -> dict[str, Any]:
    def g(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {
        "commit": g("rev-parse", "HEAD"),
        "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(g("status", "--porcelain")),
    }


def server_models(base_url: str) -> list[dict[str, Any]]:
    """``GET <base>/models`` (loopback; raises when the server does not answer)."""
    r = httpx.get(base_url.rstrip("/") + "/models", timeout=10, trust_env=False)
    r.raise_for_status()
    return list(r.json().get("data", []))


def _row(task: Task, rec: RunRecord) -> dict[str, Any]:
    return {
        "task": rec.task,
        "repeat": rec.repeat,
        "error": rec.error,
        "turns": [t.__dict__ for t in rec.turns],
        "score": score(task, rec),
    }


def rescore(run_dir: Path) -> int:
    """Recompute the scores and metrics of ``run_dir`` with the current task file."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    tasks = load_tasks(only=manifest["eval_set"]["tasks"])
    by_id = {t.id: t for t in tasks}
    rows = [
        json.loads(line)
        for line in (run_dir / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in rows:
        rec = RunRecord(row["task"], row["repeat"], [TurnRecord(**t) for t in row["turns"]])
        rec.error = row["error"]
        row["score"] = score(by_id[row["task"]], rec)
    with (run_dir / "transcripts.jsonl").open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    sha = hashlib.sha256(TASKS.read_bytes()).hexdigest()
    manifest.setdefault("rescored", []).append(
        {"utc": datetime.now(UTC).isoformat(), "from_sha256": manifest["eval_set"]["sha256"]}
    )
    manifest["eval_set"]["sha256"] = sha
    result = metrics(tasks, rows)
    result["eval_set"] = manifest["eval_set"]
    result["model"] = {"id": manifest["model"]["id"], "base_url": manifest["model"]["base_url"]}
    (run_dir / "metrics.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(
        f"rescored {run_dir}: success {result['success_rate']:.2f} correct "
        f"{result['correct_rate']:.2f}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    ap.add_argument("--out-root", type=Path, default=ROOT / "runs")
    ap.add_argument("--max-tool-calls", type=int, default=15)
    ap.add_argument("--turn-timeout", type=float, default=600)
    ap.add_argument("--request-timeout", type=float, default=600)
    ap.add_argument("--max-tokens", type=int, default=TernConfig().chat_max_tokens)
    ap.add_argument("--note", default="")
    ap.add_argument("--rescore", type=Path, help="rescore an existing run dir and exit")
    known = {"--base-url", "--model", "--label"}
    if argv is None:
        argv = sys.argv[1:]
    if "--rescore" in argv:
        for a in ap._actions:  # noqa: SLF001 - the run options are not needed for a rescore
            if a.option_strings and a.option_strings[0] in known:
                a.required = False
    args = ap.parse_args(argv)
    if args.rescore is not None:
        return rescore(args.rescore)

    tasks = load_tasks(only=[t for t in args.tasks.split(",") if t] or None)
    models = server_models(args.base_url)
    entry = next((m for m in models if m.get("id") == args.model), None)
    if entry is None:
        print(f"model {args.model!r} not served; served: {[m.get('id') for m in models]}")
        return 2

    started = datetime.now(UTC)
    run_dir = args.out_root / f"{started.strftime('%Y%m%dT%H%M%SZ')}_agent-eval-{args.label}"
    run_dir.mkdir(parents=True)
    world = build_world(run_dir / "work")
    cfg = world.config(
        chat_base_url=args.base_url,
        chat_model=args.model,
        agent_max_tool_calls=args.max_tool_calls,
        agent_turn_timeout_s=args.turn_timeout,
        chat_request_timeout_s=args.request_timeout,
        chat_max_tokens=args.max_tokens,
    )
    today = date.today()
    manifest: dict[str, Any] = {
        "name": f"agent-eval-{args.label}",
        "run_dir": str(run_dir.relative_to(ROOT) if run_dir.is_relative_to(ROOT) else run_dir),
        "git": _git(),
        # Repo-relative where possible: a published manifest carries no home path.
        "command": [_rel(sys.executable), *sys.argv],
        "eval_set": {
            **TASK_SET,
            "path": str(TASKS.relative_to(ROOT)),
            "sha256": hashlib.sha256(TASKS.read_bytes()).hexdigest(),
            "tasks": [t.id for t in tasks],
        },
        "model": {"id": args.model, "base_url": args.base_url, "server_entry": entry},
        "settings": {
            "repeats": args.repeats,
            "temperature": cfg.chat_temperature,
            "seed": cfg.chat_seed,
            "max_tokens": cfg.chat_max_tokens,
            "agent_max_tool_calls": cfg.agent_max_tool_calls,
            "agent_turn_timeout_s": cfg.agent_turn_timeout_s,
            "chat_request_timeout_s": cfg.chat_request_timeout_s,
            "agent_context_budget_tokens": cfg.agent_context_budget_tokens,
            "today": today.isoformat(),
            "tb_timezone": cfg.tb_timezone,
        },
        "env": {"python": platform.python_version(), "platform": platform.platform()},
        "note": args.note,
        "started_utc": started.isoformat(),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    rows: list[dict[str, Any]] = []
    with (run_dir / "transcripts.jsonl").open("w", encoding="utf-8") as out:
        for rep in range(args.repeats):
            for task in tasks:
                t0 = time.monotonic()
                rec = run_task(world, cfg, task, rep, today)
                row = _row(task, rec)
                rows.append(row)
                out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                out.flush()
                s = row["score"]
                print(
                    f"[r{rep}] {task.id} {s['outcome']:<14} success={s['success']!s:<5} "
                    f"answer={s['answer_ok']!s:<5} cite={s.get('cite_ok')!s:<5} "
                    f"calls={s.get('tool_calls')} {time.monotonic() - t0:6.1f}s"
                    + (f" ERROR {rec.error}" if rec.error else ""),
                    flush=True,
                )

    result = metrics(tasks, rows)
    result["eval_set"] = manifest["eval_set"]
    result["model"] = {"id": args.model, "base_url": args.base_url}
    (run_dir / "metrics.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    manifest["finished_utc"] = datetime.now(UTC).isoformat()
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(
        f"\n{run_dir}\nsuccess {result['success_rate']:.2f} "
        f"(CI {result['success_ci95'][0]:.2f}-{result['success_ci95'][1]:.2f}) "
        f"correct {result['correct_rate']:.2f} answer_ok {result['answer_ok_rate']:.2f} "
        f"cite_ok {result['cite_ok_rate']} "
        f"halluc {result['hallucinated_citation_rate']:.3f} "
        f"total p50 {result['latency_total_p50']} tps {result['decode_tps_median']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
