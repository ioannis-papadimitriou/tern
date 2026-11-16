# Command surface for development: Python (uv) for src/tern, TypeScript (npm) for extension/.

PY       ?= uv run python
PYTEST   ?= uv run pytest
RUFF     ?= uv run ruff
TYPECHK  ?= uv run pyright
SRC      ?= src
EXT      ?= extension
NPM      ?= npm --prefix $(EXT)

# ---- Node 22 -------------------------------------------------------------------------------
# Prepend the newest installed nvm v<.nvmrc> bin dir to PATH; need_ext refuses any other Node major.
NODE_MAJOR := $(shell cat .nvmrc 2>/dev/null)
NODE_BIN   := $(shell ls -dv $(HOME)/.nvm/versions/node/v$(NODE_MAJOR).*/bin 2>/dev/null | tail -n 1)
ifneq ($(NODE_BIN),)
export PATH := $(NODE_BIN):$(PATH)
endif

# ---- ruff scope ----------------------------------------------------------------------------
# Generated or frozen files (fixtures, goldens, contracts, eval sets) are never auto-fixed.
RUFF_LINT_EXCLUDE := --extend-exclude tests/smoke/test_smoke.py --force-exclude
RUFF_FIX_EXCLUDE  := --extend-exclude "tests/smoke/test_smoke.py,evals,contracts,tests/fixtures,tests/golden,scripts/gen_synthetic_mailbox.py" --force-exclude

HAS_PY  := $(wildcard pyproject.toml)
HAS_EXT := $(wildcard $(EXT)/package.json)
need_py  = @test -n "$(HAS_PY)"  || (echo "pyproject.toml missing" && exit 2)
need_ext = @test -n "$(HAS_EXT)" || (echo "extension/package.json missing" && exit 2); \
           node --version 2>/dev/null | grep -q '^v$(NODE_MAJOR)\.' || \
           (echo "Node $(NODE_MAJOR) required (found: $$(node --version 2>/dev/null || echo none)). Run: nvm install $(NODE_MAJOR) && nvm alias default $(NODE_MAJOR)" && exit 2)

.PHONY: setup fmt lint lint-py lint-ts typecheck typecheck-py typecheck-ts test test-fast \
        test-py-fast test-ts test-slow node-version smoke check check-fast serve \
        ext-build ext-xpi ext-guard install-host requirements

setup:
	$(need_py)
	uv sync --all-extras
	$(need_ext)
	$(NPM) ci

fmt:
	$(need_py)
	$(RUFF) format $(RUFF_FIX_EXCLUDE) .
	$(RUFF) check --fix $(RUFF_FIX_EXCLUDE) .
	$(need_ext)
	$(NPM) run fmt

lint: lint-py lint-ts

lint-py:
	$(need_py)
	$(RUFF) check $(RUFF_LINT_EXCLUDE) .
	$(RUFF) format --check $(RUFF_LINT_EXCLUDE) .

lint-ts:
	$(need_ext)
	$(NPM) run lint

typecheck: typecheck-py typecheck-ts

typecheck-py:
	$(need_py)
	$(TYPECHK)

typecheck-ts:
	$(need_ext)
	$(NPM) run typecheck

# Unit tests only, quiet, short tracebacks. Skips `heavy` (expensive functional tests, listed in
# tests/conftest.py HEAVY) so check-fast stays under a minute; `make test` / `make check` run them.
test-py-fast:
	$(need_py)
	$(PYTEST) -q --tb=short -x -m "not smoke and not slow and not heavy" tests

test-ts:
	$(need_ext)
	$(NPM) run test

test-fast: test-py-fast test-ts

# Everything except smoke and `slow` tests (wall-clock latency budgets are flaky under load).
test:
	$(need_py)
	$(PYTEST) -q --tb=short -m "not smoke and not slow" tests
	$(need_ext)
	$(NPM) run test

# `slow` tests: wall-clock budgets (e.g. tern search < 300 ms p90). Idle machine; not part of `check`.
test-slow:
	$(need_py)
	$(PYTEST) -q --tb=short -m slow tests

# The Node that every JS target uses (nvm v<.nvmrc> on PATH)
node-version:
	@node --version
	@node --version | grep -q '^v$(NODE_MAJOR)\.' || (echo "not Node $(NODE_MAJOR): nvm install $(NODE_MAJOR) && nvm alias default $(NODE_MAJOR)" && exit 2)

smoke:
	$(need_py)
	$(PYTEST) -q --tb=short -rs tests/smoke

# esbuild bundle → extension/dist/ (load dist/manifest.json in Thunderbird via Debug Add-ons)
ext-build:
	$(need_ext)
	$(NPM) run build

# tern.xpi: the built extension as an installable add-on (Add-ons Manager → Install Add-on From
# File); stays installed across restarts. Re-run and reinstall after every change.
ext-xpi: ext-build
	rm -f tern.xpi
	cd $(EXT)/dist && zip -qr -X ../../tern.xpi .
	@echo "built tern.xpi"

# tern never sends mail: no code path may reach a send API. Layers:
# 1) Primary: the manifest must not request the send permissions, so TB doesn't expose the APIs.
# 2) Sources (.ts/.tsx/.js/.mjs in src/ and experiments/): no compose/messages(?.)sendMessage,
#    bracket access or same-line destructuring.
# 3) Bundle: every `sendMessage` in dist/*.js must be runtime.sendMessage or tabs.sendMessage
#    (internal messaging). Catches aliasing and multi-line destructuring that (2) misses, since
#    esbuild keeps property names. Not caught: names built at runtime from strings (review).
# 4) Experiments (privileged code): no iTIP / MailServices / nsIMsgSend / sendMail at all.
SEND_PERM_RE := "\"(compose\.send|messages\.send)\""
SEND_API_RE  := "(compose|messages)[[:space:]]*\??\.[[:space:]]*sendMessage|\[[[:space:]]*[\"'\`]sendMessage[\"'\`][[:space:]]*\]|\{[^}]*\bsendMessage\b[^}]*\}[[:space:]]*=[[:space:]]*(messenger|browser)\.(compose|messages)"
EXP_SEND_RE  := "\b(itip|MailServices|nsIMsgSend|sendMail|sendMessage)\b"
# Every grep gets explicit, existing operands: `grep -r` with no file operand would search the
# whole working tree (docs and this Makefile mention sendMessage) and fail forever.
EXT_SRC_INCLUDES := --include=*.ts --include=*.tsx --include=*.js --include=*.mjs
EXT_SCAN_DIRS     = $(wildcard $(EXT)/src $(EXT)/experiments $(EXT)/dist)
ext-guard: ext-build
	@test -f $(EXT)/manifest.json && test -f $(EXT)/dist/manifest.json && test -d $(EXT)/src || \
	  (echo "ext-guard: missing $(EXT)/manifest.json, $(EXT)/dist/manifest.json or $(EXT)/src" && exit 1)
	@! grep -nE $(SEND_PERM_RE) $(EXT)/manifest.json $(EXT)/dist/manifest.json || \
	  (echo "ext-guard: send permission requested in manifest (tern never sends mail)" && exit 1)
	@! grep -rnE $(SEND_API_RE) $(EXT_SCAN_DIRS) $(EXT_SRC_INCLUDES) || \
	  (echo "ext-guard: forbidden send API reference (tern never sends mail)" && exit 1)
	@! grep -rhoE "[[:alnum:]_\$$?.{,]{0,40}sendMessage" $(EXT)/dist --include=*.js --include=*.mjs | \
	  grep -vE "(runtime|tabs)\??\.sendMessage$$" || \
	  (echo "ext-guard: bundle references sendMessage outside runtime/tabs (tern never sends mail)" && exit 1)
	@if [ -d "$(EXT)/experiments" ]; then \
	  ! grep -rnE $(EXP_SEND_RE) "$(EXT)/experiments" $(EXT_SRC_INCLUDES) --include=*.json || \
	  (echo "ext-guard: experiment code reaches send/iTIP machinery (tern never sends mail)" && exit 1); \
	fi
	@echo "ext-guard: OK (no send permission, no send API reference)"

check-fast: lint typecheck-ts test-fast

check: lint typecheck test ext-guard smoke

# requirements.txt for plain-pip installs, exported from uv.lock (runtime deps only)
requirements:
	uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt

# The daemon in the foreground (Unix socket only; never a TCP port), with logs on stderr. For
# development run `TERN_HOME=<tmp dir> make serve`: TERN_HOME relocates data, state, socket and config.
serve:
	$(need_py)
	uv run ternd --foreground

# Writes the native-messaging host manifest so Thunderbird can start tern-nmhost
install-host:
	$(need_py)
	uv run tern install-host
