"""Smoke-test adapter for tern: the ONLY file you edit per project.

tern has no training step. The generic contracts in test_smoke.py map onto tern like this:

    load_smoke_config  -> SmokeConfig: synthetic mailbox, temp XDG dirs, StubEmbedder, FakeLLM, FakeBridge
    build_tiny_dataset -> REAL ingest of the synthetic mailbox through FakeBridge into a temp index
    train_tiny         -> REAL index build (ingest + chunk + FTS + embed) persisted to out_dir;
                          "losses" = fraction of fixture messages NOT yet indexed after each ingest batch
                          (must be finite and fall to 0.0; catches an ingest that silently indexes nothing)
    predict            -> REAL hybrid search over the index RE-OPENED from out_dir (fresh objects)
    evaluate           -> REAL tern.eval retrieval metrics on evals/synthetic/qa-v1.jsonl
    reference_case     -> the reference query: expected cited message ids, computed end-to-end
    api_client/request -> REAL MCP server (tern mcp) over an in-memory transport, one tool call

Everything runs on CPU in < 2 min, without LM Studio, Thunderbird, network or GPU:
  - StubEmbedder: deterministic feature-hashing embedder (dim 64) behind the real EmbeddingClient
    interface. It exercises the real vector path, not model quality. The vec0 table is created with
    the dimension registered in `embedding_model` (64 here, the real model's dim in production; M9),
    so the smoke run goes through the same schema code as production, never a hard-coded 1024.
  - FakeLLM: scripted tool calls behind the real LLMClient interface (agent-loop plumbing only).
  - FakeBridge: serves tests/fixtures/synthetic_mailbox/ as tern-bridge/1 responses, built on the
    same pydantic protocol models as the real bridge.
No real mail is ever used here (decisions #11, #20). All addresses use example.com/.test domains.

Each function:
  - raises NotImplementedError  -> test FAILS ("not wired yet"), so nothing is silently skipped
  - raises NotApplicable("why") -> test is SKIPPED with that reason, shown in the report
  - returns a value             -> test runs
Wired in: plans/05-search.md (config, dataset, train, predict, inputs), 07-mcp.md (api_*),
08a-llm-agent.md (reference_case), 09-qa-eval.md (evaluate).
Fixture files under tests/fixtures/ and evals/synthetic/ are PROTECTED: they are created only in a
session started with CLAUDE_UNLOCK_PROTECTED=<the exact globs the plan step names> (plans 01, 08a, 09).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tern.testing import smoke

MAILBOX = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_mailbox"
SMOKE_SCRIPT = Path(__file__).resolve().parents[2] / "evals" / "synthetic" / "smoke_script.json"


class NotApplicable(Exception):
    """Raise when a contract genuinely doesn't apply to this project (gives a visible skip)."""


def load_smoke_config() -> Any:
    """Return a `tern.testing.SmokeConfig` with:
    - mailbox = tests/fixtures/synthetic_mailbox/ (≈200 messages: 3 accounts, 3 languages
      (en/de/el — placeholder until the user confirms their languages, A-22), threads, newsletters,
      invoices, calendar invites, 5 injection mails, 3 messages without Message-ID, 2 benign
      Message-ID duplicates across folders (Gmail-label style), 1 benign normalization pair (same
      Message-ID and content; CRLF + `>From `-quoted vs LF + unquoted), 1 adversarial
      same-Message-ID pair on invoice inv-0077 with differing content (D1), Sent-folder mail to 3
      addresses (sent_recipients), mixed charsets / RFC 2047 headers, planted SENTINEL- strings
      listed in MANIFEST.json, 1 malformed MIME);
    - tern_home = a fresh temp dir; TERN_HOME points at it, which relocates data/, state/,
      run/bridge.sock and config/config.toml together, so no real path or real daemon socket is
      ever touched;
    - embedder = StubEmbedder(dim=64, seed=0);
    - ingest_batch_size = 50 (so train_tiny yields ≥ 4 progress points); seed = 0.
    No LLM here: reference_case builds its own FakeLLM (plan 08a).
    Must not read ~/.config/tern; config comes only from this function.

    Wired (plan 05 step 8): the module-scoped smoke fixtures run before the autouse TERN_HOME
    fixture, so the environment is not read at all. "TERN_HOME points at it" means the
    SmokeConfig's `paths` are `tern.paths.resolve({"TERN_HOME": tern_home})`, and its TernConfig is
    built in code (embed_model = the stub's id).
    """
    return smoke.smoke_config(MAILBOX)


def build_tiny_dataset(cfg: Any) -> Any:
    """Run the REAL ingest path (`tern.index.ingest.Backfill` over `FakeBridge(cfg.mailbox)`) into
    a temp index under cfg.tern_home and return a handle `(index_path, ingest_report)`.
    This exercises date-window paging with the durable cursor (M8), `mail.messages.raw`, MIME parse,
    mid computation (#5), dedup of the benign cross-folder duplicates and of the CRLF/`>From `
    normalization pair into one message with two locations each (not conflicted), the adversarial
    inv-0077 pair flagged `conflicted` (D1), `sent_recipients` filled from the Sent folder, and
    parse_status for the malformed one.
    """
    return smoke.build_dataset(cfg)


def dataset_size(ds: Any) -> int:
    """Number of distinct `mid`s in the index built by build_tiny_dataset. Must equal the fixture
    manifest's `distinct_messages` (benign duplicates, the normalization pair and the conflicted pair each counted once; the
    malformed message is still indexed
    with parse_status='partial' or 'failed', never dropped)."""
    return smoke.dataset_size(ds)


def train_tiny(cfg: Any, ds: Any, out_dir: Path) -> list[float]:
    """Build the full index (ingest + chunk + FTS5 + embeddings via StubEmbedder into sqlite-vec)
    with the REAL `tern.index` code, writing `index.db` into out_dir.
    Return the ingest-progress curve: after each batch, (fixture messages not yet indexed) / total,
    e.g. [1.0, 0.75, 0.5, 0.25, 0.0]. The first value is measured BEFORE the first batch (1.0);
    the last must be 0.0. This is a progress measure, not a model loss; it is real output of the
    ingest job's state (the persisted index: `messages` rows; `ingest_state` for completion), not a
    computed sequence.
    """
    return smoke.train(cfg, ds, out_dir)


def predict(cfg: Any, checkpoint_dir: Path, inputs: list[Any]) -> list[Any]:
    """Open `checkpoint_dir/index.db` with FRESH objects (new connection, new SearchService) and run
    the REAL hybrid search (FTS5 BM25 + vector, RRF k=60) for each input query.
    Return, per query, a tuple of the top-5 `mid`s (stable order; ties broken by mid).
    Two calls must return identical results (determinism contract).
    """
    return smoke.predict(cfg, checkpoint_dir, inputs)


def smoke_inputs() -> list[Any]:
    """Three fixed queries covering the synthetic mailbox:
    1. "Acme invoice March" (English, should hit the invoice thread);
    2. "Rechnung Hausverwaltung" (German);
    3. "συνάντηση Πέμπτη" (Greek: 'meeting Thursday').
    Returned as plain strings. The German query has no fixture mail (languages en/el, A-22
    placeholder), so its hits are vector-only by design."""
    return list(smoke.SMOKE_QUERIES)


def evaluate(cfg: Any, checkpoint_dir: Path, seed: int) -> dict[str, float]:
    """Run the REAL `tern.eval.qa` retrieval evaluation (the same code `make exp` uses with the
    qa config) on the SYNTHETIC evals/synthetic/qa-v1.jsonl (10 questions with expected mids; not the
    private qa-v1 of the same version name) against the index in
    checkpoint_dir, retrieval-only mode (no LLM). Return at least
    {"recall_at_10": …, "mrr_at_10": …, "invalid_mid_rate": …}. Same seed must give equal dicts.
    """
    raise NotImplementedError


def reference_case() -> tuple[float, float, float]:
    """The reference query, end to end (fresh temp index from the synthetic mailbox):
    query "When is the Acme invoice INV-0042 due and who sent the reminder?" through the agent loop
    with FakeLLM(script=evals/synthetic/smoke_script.json) (created in plan 08a step 5, in a session
    started with CLAUDE_UNLOCK_PROTECTED=evals/synthetic/smoke_script.json)
    scripted to call search_messages, then answer citing what it retrieved plus one hallucinated mid
    and the conflicted inv-0077 mid (both must be stripped by citation validation).
    Expected cited mids (hand-derived from the fixture manifest, written in a comment when wired):
      mid("<inv-0042@acme.example.com>") and mid("<inv-0042-reminder@acme.example.com>").
    computed = |citations ∩ expected| / |expected| after the daemon's citation validation
    (citations not returned by a tool this turn are stripped, so a hallucinated mid scores 0).
    Return (computed, 1.0, 0.0).

    Wired (plan 08a step 5): `tern.testing.smoke.reference_case` (real ingest + StubEmbedder,
    hybrid search, real Registry + Engine + AuditLog, caller `agent:smoke`, the real LLMClient
    over FakeLLM). It also raises `SmokeError` unless the hallucinated and the conflicted mid were
    both stripped (absent from the citations and the shown text) and counted as raw and invalid
    citations: the score alone would stay 1.0 with stripping switched off (plan 08a A9).
    """
    # Hand-derived from tests/fixtures/synthetic_mailbox/MANIFEST.json (mid_rule: sha256 of the
    # Message-ID without angle brackets, lower-cased; decisions #5):
    #   sha256("inv-0042@acme.example.com")          = ca20f5b0…bf732e (message key inv-0042)
    #   sha256("inv-0042-reminder@acme.example.com") = e6d2ae08…f3000c (key inv-0042-reminder)
    #   sha256("inv-0077@acme.example.com")          = 7b16c110…0215ed (adversarial_pair,
    #     expected_conflicted: true)
    # The hallucinated mid is 64 hex characters that name no fixture message.
    expected = (
        "ca20f5b069f8a8eec269b0e50cbf82cda8896811ab89e1eed85f993b8ebf732e",
        "e6d2ae082619fcd814c79d1b3628c73bf9c5b4240af87f65a54ab3b33bf3000c",
    )
    result = smoke.reference_case(
        MAILBOX,
        SMOKE_SCRIPT,
        question="When is the Acme invoice INV-0042 due and who sent the reminder?",
        expected=expected,
        hallucinated="deadbeef" * 8,
        conflicted="7b16c110ce53752d011d6689051ca561c8b4c3b950e933fa45faf490840215ed",
    )
    return (result.computed, 1.0, 0.0)


def api_client() -> Any:
    """Return a small wrapper around the REAL MCP server (`tern.mcp.server.build_server()` with the
    real registry + permission engine over FakeBridge and a temp index built from the synthetic
    mailbox), connected via the SDK's in-memory client session (verify the mcp 2.x API name).
    The wrapper exposes `.request(method, path, json)` and returns an object with `.status_code`
    and `.text`: method "TOOL" calls the tool named by `path` with `json` as arguments;
    status 200 = success result, 403 = denied/pending approval, 422 = input validation error,
    500 = tool error. It must go through Registry.call (audit row written in the temp audit.db).

    Wired (plan 07 step 4): `tern.testing.smoke.api_client` builds its own fresh temp index (real
    ingest + StubEmbedder, hybrid search) and serves `build_server(RegistryBackend(registry,
    "mcp:smoke"))`, the SDK's `mcp.client.Client` connected in memory, one session per request.
    Status: success 200; `invalid_params` 422; `denied`, `expired`, `rate_limited`,
    `tier_not_enabled`, `targets_changed`, `state_changed` 403; any other tool error 500. Each
    request raises `SmokeError` unless exactly one new audit row (caller `mcp:smoke`) was written.
    """
    return smoke.api_client(MAILBOX)


def api_smoke_request() -> tuple[str, str, dict | None, int]:
    """("TOOL", "search_messages", {"query": "Acme invoice", "limit": 5}, 200)."""
    return ("TOOL", "search_messages", {"query": "Acme invoice", "limit": 5}, 200)
