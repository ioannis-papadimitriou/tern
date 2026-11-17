"""Bar-chart comparison of agent-eval runs, as a self-contained SVG (no plotting dependency).

    uv run python scripts/agent_eval/figure.py agent-eval/results/<A> agent-eval/results/<B> ... \
        --memory agent-eval/memory.json --out agent-eval/comparison.svg

One panel per metric (each its own scale and unit: never a shared axis), one bar per run in a
fixed categorical color, every bar labelled with its model and value. ``memory.json`` maps a
run's label to its measured footprint: ``{"<label>": {"name": ..., "vram_gib": ..., "ram_gib":
..., "context": ..., "detail": ...}}``; ``name`` labels the bars, ``detail`` the key. The SVG
follows the reader's light/dark scheme (GitHub honours ``prefers-color-scheme`` inside an SVG
image).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from html import escape
from pathlib import Path

# Validated categorical slots 1-3 (light / dark), fixed order: run i always gets slot i.
LIGHT = ("#2a78d6", "#eb6834", "#1baf7a")
DARK = ("#3987e5", "#d95926", "#199e70")


@dataclass(frozen=True)
class Panel:
    title: str
    key: str
    unit: str
    higher_is_better: bool
    percent: bool = False
    digits: int = 0


PANELS = (
    Panel("Correct: answer and citations", "correct_rate", "%", True, percent=True),
    Panel("Strict success (also within tool-call cap)", "success_rate", "%", True, percent=True),
    Panel("Citations fully right", "cite_ok_rate", "%", True, percent=True),
    Panel("Task time, median", "latency_total_p50", "s", False, digits=1),
    Panel("Decode speed, median", "decode_tps_median", "tok/s", True),
    Panel("Tool calls per task", "tool_calls_mean", "", False, digits=1),
    Panel("GPU memory (VRAM)", "vram_gib", "GiB", False, digits=1),
    Panel("System RAM for the model", "ram_gib", "GiB", False, digits=1),
)

W, COL_W, GAP = 960, 450, 60
LEFT = 140  # model-name column inside a panel
BAR_H, BAR_GAP = 20, 8
PANEL_TOP = 34  # title + subtitle above the bars
PANEL_H = PANEL_TOP + 3 * (BAR_H + BAR_GAP) + 18
HEAD = 126


def _fmt(p: Panel, v: float) -> str:
    if p.percent:
        return f"{v * 100:.0f}%"
    if p.key == "ram_gib" and v == 0:
        return "0 (all on GPU)"
    return f"{v:.{p.digits}f} {p.unit}".strip()


def _panel(p: Panel, rows: list[tuple[str, float]], x: int, y: int) -> list[str]:
    out = [
        f'<text class="t1" x="{x}" y="{y + 14}" font-weight="600">{escape(p.title)}</text>',
        f'<text class="t2" x="{x}" y="{y + 29}" font-size="11">'
        f"{'higher' if p.higher_is_better else 'lower'} is better</text>",
    ]
    top = max(v for _, v in rows) or 1.0
    scale_max = 1.0 if p.percent else top
    span = COL_W - LEFT - 70
    base = x + LEFT
    out.append(
        f'<line class="axis" x1="{base}" x2="{base}" y1="{y + PANEL_TOP - 2}" '
        f'y2="{y + PANEL_TOP + 3 * (BAR_H + BAR_GAP) - BAR_GAP + 2}"/>'
    )
    for i, (name, v) in enumerate(rows):
        by = y + PANEL_TOP + i * (BAR_H + BAR_GAP)
        w = span * v / scale_max if v == 0 else max(2.0, span * v / scale_max)
        out.append(
            f'<text class="t2" x="{base - 8}" y="{by + BAR_H / 2 + 4}" text-anchor="end" '
            f'font-size="12">{escape(name)}</text>'
        )
        # 4px rounded data end, square at the baseline: a rounded rect plus a square cap.
        if w > 0:
            out.append(
                f'<rect class="s{i}" x="{base}" y="{by}" width="{w:.1f}" height="{BAR_H}" rx="4"/>'
            )
            out.append(
                f'<rect class="s{i}" x="{base}" y="{by}" width="{min(w, 4):.1f}" height="{BAR_H}"/>'
            )
        out.append(
            f'<text class="t1" x="{base + w + 6:.1f}" y="{by + BAR_H / 2 + 4}" '
            f'font-size="12">{escape(_fmt(p, v))}</text>'
        )
    return out


def render(
    runs: list[tuple[str, dict[str, float]]], title: str, note: str, details: list[str]
) -> str:
    n_rows = (len(PANELS) + 1) // 2
    h = HEAD + n_rows * (PANEL_H + 16) + 30
    light = "".join(f".s{i}{{fill:{c}}}" for i, c in enumerate(LIGHT))
    dark = "".join(f".s{i}{{fill:{c}}}" for i, c in enumerate(DARK))
    style = (
        "svg{font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif}"
        ".bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}"
        ".axis{stroke:#c3c2b7;stroke-width:1}" + light + "@media (prefers-color-scheme: dark){"
        ".bg{fill:#1a1a19}.t1{fill:#f0efec}.t2{fill:#c3c2b7}.axis{stroke:#52514e}" + dark + "}"
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" '
        f'viewBox="0 0 {W} {h}" role="img" aria-label="{escape(title)}">',
        f"<title>{escape(title)}</title>",
        f"<style>{style}</style>",
        f'<rect class="bg" width="{W}" height="{h}" rx="8"/>',
        f'<text class="t1" x="24" y="32" font-size="18" font-weight="600">{escape(title)}</text>',
        f'<text class="t2" x="24" y="52" font-size="12">{escape(note)}</text>',
    ]
    # Key: each model's swatch, short name and details (the bars carry the short name).
    for i, ((name, _), detail) in enumerate(zip(runs, details, strict=True)):
        ky = 66 + i * 18
        parts.append(f'<rect class="s{i}" x="24" y="{ky}" width="12" height="12" rx="3"/>')
        parts.append(
            f'<text class="t1" x="42" y="{ky + 10}" font-size="12" font-weight="600">'
            f'{escape(name)}<tspan class="t2" font-weight="400"> {escape(detail)}</tspan></text>'
        )
    for k, p in enumerate(PANELS):
        col, row = k % 2, k // 2
        x = 24 + col * (COL_W + GAP // 2)
        y = HEAD + row * (PANEL_H + 16)
        rows = [(name, float(vals[p.key])) for name, vals in runs]
        parts += _panel(p, rows, x, y)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("runs", nargs="+", type=Path, help="run dirs (each with metrics.json)")
    ap.add_argument("--memory", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--title", default="tern agent eval: three local models")
    ap.add_argument(
        "--note",
        default="25 synthetic-mailbox tasks × 2 runs each, temperature 0. "
        "Memory: measured footprint at the context shown.",
    )
    args = ap.parse_args(argv)
    if len(args.runs) > len(LIGHT):
        ap.error(f"at most {len(LIGHT)} runs (one validated color each)")
    memory = json.loads(args.memory.read_text(encoding="utf-8"))
    runs: list[tuple[str, dict[str, float]]] = []
    details: list[str] = []
    for d in args.runs:
        metrics = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
        label = json.loads((d / "manifest.json").read_text(encoding="utf-8"))["name"]
        mem = memory[label]
        details.append(mem.get("detail", ""))
        runs.append(
            (mem["name"], {**metrics, "vram_gib": mem["vram_gib"], "ram_gib": mem["ram_gib"]})
        )
    args.out.write_text(render(runs, args.title, args.note, details), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
