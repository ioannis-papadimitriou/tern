"""Compare two (or more) agent-eval run dirs as Markdown: overall metrics, then per task.

    uv run python scripts/agent_eval/compare.py runs/<A> runs/<B> [> comparison.md]

Refuses runs of different task sets (sha256) unless ``--allow-mixed``.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: (key, label, format) of the overall table.
OVERALL: tuple[tuple[str, str, str], ...] = (
    ("n_runs", "runs", "d"),
    ("success_rate", "task success (strict)", "pct"),
    ("success_ci95", "  95% CI", "ci"),
    ("correct_rate", "correct (answer + citations, any call count)", "pct"),
    ("answer_ok_rate", "answer correct", "pct"),
    ("calls_ok_rate", "within the task's tool-call cap", "pct"),
    ("canary_echoed", "injection canary quoted in answer", "d"),
    ("cite_ok_rate", "citations ok (tasks with expected cites)", "pct"),
    ("citation_recall_mean", "citation recall (mean)", "pct"),
    ("hallucinated_citation_rate", "hallucinated citations / all citations", "pct"),
    ("conflicted_citations", "conflicted (spoof) citations stripped", "d"),
    ("tool_calls_mean", "tool calls per task (mean)", "f1"),
    ("harness_errors", "harness errors", "d"),
    ("latency_first_visible_p50", "first visible answer p50 (s)", "f1"),
    ("latency_first_visible_p90", "first visible answer p90 (s)", "f1"),
    ("latency_total_p50", "task time p50 (s)", "f1"),
    ("latency_total_p90", "task time p90 (s)", "f1"),
    ("latency_total_sum", "wall time, all tasks (s)", "f0"),
    ("decode_tps_median", "decode tok/s (median per request)", "f1"),
    ("completion_tokens_mean", "completion tokens per task (mean)", "f0"),
    ("reasoning_chars_mean", "reasoning chars per task (mean)", "f0"),
)


def _fmt(value: Any, kind: str) -> str:
    if value is None:
        return "–"
    if kind == "pct":
        return f"{100 * value:.0f}%"
    if kind == "ci":
        return f"{100 * value[0]:.0f}–{100 * value[1]:.0f}%"
    if kind == "d":
        return str(value)
    if kind == "f0":
        return f"{value:.0f}"
    return f"{value:.1f}"


def load(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    return manifest, metrics


def _name(manifest: dict[str, Any]) -> str:
    return manifest["name"].removeprefix("agent-eval-")


def render(runs: Sequence[tuple[dict[str, Any], dict[str, Any]]]) -> str:
    names = [_name(m) for m, _ in runs]
    lines = ["## Overall", "", "| metric | " + " | ".join(names) + " |"]
    lines.append("|---|" + "---|" * len(runs))
    for key, label, kind in OVERALL:
        lines.append(f"| {label} | " + " | ".join(_fmt(x.get(key), kind) for _, x in runs) + " |")
    outcomes = sorted({o for _, x in runs for o in x["outcomes"]})
    for o in outcomes:
        lines.append(
            f"| outcome `{o}` | " + " | ".join(str(x["outcomes"].get(o, 0)) for _, x in runs) + " |"
        )

    lines += ["", "## By kind (successes / runs)", "", "| kind | " + " | ".join(names) + " |"]
    lines.append("|---|" + "---|" * len(runs))
    kinds = sorted({k for _, x in runs for k in x["per_kind"]})
    for k in kinds:
        cells = []
        for _, x in runs:
            v = x["per_kind"].get(k)
            cells.append("–" if v is None else f"{v['success']}/{v['runs']}")
        lines.append(f"| {k} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Per task (strict success / correct / runs · mean tool calls · mean time)",
        "",
        "| task | kind | lang | " + " | ".join(names) + " |",
    ]
    lines.append("|---|---|---|" + "---|" * len(runs))
    tasks = sorted({t for _, x in runs for t in x["per_task"]})
    for t in tasks:
        first = next(x["per_task"][t] for _, x in runs if t in x["per_task"])
        cells = []
        for _, x in runs:
            v = x["per_task"].get(t)
            if v is None:
                cells.append("–")
                continue
            secs = "–" if v["total_s_mean"] is None else f"{v['total_s_mean']:.0f}s"
            bad = {o: n for o, n in v["outcomes"].items() if o != "ok"}
            extra = (" " + ",".join(f"{o}×{n}" for o, n in bad.items())) if bad else ""
            cells.append(f"{v['success']}/{v['runs']} · {v['tool_calls_mean']:.1f} · {secs}{extra}")
        lines.append(f"| {t} | {first['kind']} | {first['lang']} | " + " | ".join(cells) + " |")

    lines += ["", "## Runs", ""]
    for m, _ in runs:
        s = m["settings"]
        dirty = " (dirty)" if m["git"]["dirty"] else ""
        lines.append(
            f"- **{_name(m)}**: model `{m['model']['id']}` at {m['model']['base_url']}, "
            f"{m['run_dir']}, git {m['git']['commit'][:10]}{dirty}, "
            f"repeats {s['repeats']}, max_tokens {s['max_tokens']}, temperature {s['temperature']}"
            + (f" — {m['note']}" if m.get("note") else "")
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--allow-mixed", action="store_true")
    args = ap.parse_args(argv)
    if len(args.run_dirs) < 2:
        ap.error("give at least two run dirs")
    runs = [load(d) for d in args.run_dirs]
    shas = {m["eval_set"]["sha256"] for m, _ in runs}
    if len(shas) > 1 and not args.allow_mixed:
        ap.error("the runs used different task sets (sha256 differs); pass --allow-mixed")
    print(render(runs), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
