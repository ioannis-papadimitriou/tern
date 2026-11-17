# Agent eval: task set and results

The data and results of tern's end-to-end agent eval. The harness lives in
[`scripts/agent_eval/`](../scripts/agent_eval/) (how it builds the synthetic world, isolation,
scoring); this folder holds what a run consumes and what it produced.

Synthetic data only: every task runs against `tests/fixtures/synthetic_mailbox`. These numbers
compare models on tern's own tool loop. They say nothing about quality on real mail.

## Files

| Path | What |
|---|---|
| `tasks-v1.jsonl` | The 25 tasks (sha256 in each run's `manifest.json`). Frozen: a changed set is `tasks-v2.jsonl`. |
| `results/<UTC>_agent-eval-<label>/` | One published run: `manifest.json` (git commit, model, server, settings, task-set hash), `metrics.json`, `transcripts.jsonl` (every answer, tool call and score). The synthetic world (`work/`) is left out; the runner rebuilds it. |
| `memory.json` | Each run's measured GPU memory and the system RAM the model needs (0 when fully on the GPU), and how they were measured. |
| `comparison.md` | `compare.py` output over the three runs: overall, by kind, per task. |
| `comparison.svg` | The bar charts in the main README (`figure.py`). |

## Runs

| Run | Model | Server | Context |
|---|---|---|---|
| `20261117T193857Z_agent-eval-strata-iq3xxs` | Qwen3.8-Flash-Next IQ3_XXS | Strata | 524,288 |
| `20261117T205142Z_agent-eval-lmstudio-gptoss20b` | gpt-oss-20b MXFP4 | LM Studio | 131,072 (max) |
| `20261117T205933Z_agent-eval-lmstudio-bonsai2-27b-180k` | Ternary Bonsai 2 27B Q2_0 | LM Studio | 180,224 (262,144 does not fit next to a desktop session on 24 GiB) |

All: 2 repeats, temperature 0, seed 0, `max_tokens` 4096, up to 15 tool calls a turn, tern's
default 200k-token tool-output budget, one RTX 3090 (24 GiB) and 94 GiB RAM, fully on the GPU
(LM Studio: one parallel slot for Bonsai, the default four for gpt-oss).

## Reproduce

```sh
uv run python scripts/agent_eval/run.py --base-url http://127.0.0.1:47831/v1 --model iq3_xxs \
    --label strata-iq3xxs --repeats 2
# publish a run: everything but its regenerable work/ dir
mkdir -p agent-eval/results/<run> && cp runs/<run>/{manifest,metrics}.json runs/<run>/transcripts.jsonl agent-eval/results/<run>/
uv run python scripts/agent_eval/compare.py agent-eval/results/<A> agent-eval/results/<B> ... > agent-eval/comparison.md
uv run python scripts/agent_eval/figure.py agent-eval/results/<A> ... --memory agent-eval/memory.json --out agent-eval/comparison.svg
```
