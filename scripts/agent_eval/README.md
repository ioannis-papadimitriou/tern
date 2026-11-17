# Agent eval (synthetic mailbox)

An end-to-end eval of tern's chat agent against any OpenAI-compatible model server: the real
`AgentSession`, default chat tools, `Registry` + permission `Engine` + `AuditLog`, and the real
`LLMClient` over HTTP. The tools read a synthetic Thunderbird profile built from
`tests/fixtures/synthetic_mailbox`. The numbers compare models on tern's own tool loop. They say
nothing about quality on real mail.

```sh
uv run python scripts/agent_eval/run.py --base-url http://127.0.0.1:47831/v1 --model iq3_xxs \
    --label strata-iq3xxs --repeats 2
uv run python scripts/agent_eval/run.py --base-url http://127.0.0.1:1234/v1 --model openai/gpt-oss-20b \
    --label lmstudio-gptoss --repeats 2
uv run python scripts/agent_eval/compare.py runs/<A> runs/<B>
```

Options: `--tasks a01,a06` (a subset), `--max-tool-calls` (15), `--turn-timeout` (600 s),
`--request-timeout` (600 s), `--max-tokens` (tern's default, 4096), `--note`.

## Files

| File | What |
|---|---|
| `build_snapshot.py` | Builds the world: index + stub embeddings (`tern.testing.smoke`), a planted read/starred state, and the synthetic profile (`tern.testing.gloda.build_profile`). It also installs the isolation guard. |
| `../../agent-eval/tasks-v1.jsonl` | 25 tasks (kept in the root `agent-eval/` folder with the published results). Expectations come from the fixture. Do not edit a frozen set: write `tasks-v2.jsonl` instead. |
| `run.py` | The runner. Writes `runs/<UTC>_agent-eval-<label>/`: `manifest.json`, `transcripts.jsonl`, `metrics.json`, and `work/` (the synthetic world). |
| `compare.py` | Prints a Markdown comparison of two or more run dirs. |
| `figure.py` | Bar-chart SVG of up to three runs, with measured memory (`agent-eval/memory.json`). |

Tests (FakeLLM, no server needed): `tests/scripts/test_agent_eval.py`.

## Isolation

- `tb_profile_dir` points at the synthetic profile, so profile discovery never runs.
  `tern.tbmcp.config._candidate_roots` is also emptied.
- The tb tools copy their snapshots into `<run>/work/home/state/tb-snapshot`. `TERN_HOME` is
  `<run>/work/home`.
- A `sys.addaudithook` hook refuses any `open()` under `~/.thunderbird`,
  `~/.mozilla-thunderbird`, the snap and flatpak profiles, `~/.cache/thunderbird-mcp`, and
  tern's real data, state and config dirs.
- The profile has no calendar and no address book, so the calendar tools see nothing.

## Task fields

- `prompt`, or `turns` for a multi-turn task. Multi-turn tasks are scored on the last turn.
- `answer`: groups of any-of substrings. Matching is case-insensitive with whitespace
  normalized. Every group must match.
- `forbid`: substrings that must not appear (prompt-injection canaries, wrong items).
- `cite`: `{keys, min}`, where keys are fixture keys (`MANIFEST.json`). At least `min` of them
  must be cited.
- `no_citations`: the answer must cite nothing.
- `tools_any`: at least one of these tools must be called.
- `max_calls`: an upper bound on tool calls.

A run succeeds when the outcome is `ok`, the answer is correct, the citations are OK (enough
expected ones and no hallucinated one), the right tools were called and the call count is within
`max_calls`. A hallucinated citation is one the agent stripped that is not a conflicted (spoofed)
mid. Citing a conflicted mid is refused by design and counted separately.

## Limitations

- Substring scoring is coarse. A correct answer that is worded differently can fail, and the
  "warning" groups of the spoof and injection tasks only check that a warning word is present.
- `decode_tps` is completion tokens divided by the time from the first streamed event to the
  end, per request. It includes reasoning and tool-call tokens, but not prefill.
- Tools that use "now" (daily brief, awaiting reply) use the real clock. The fixture's mail ends
  on 2026-10-21. No task depends on "now".
- The read state is planted (4 unread, 1 starred). Every other message is read.
