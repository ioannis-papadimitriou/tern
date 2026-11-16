"""The tool registry (#6, plan 06a): discovery, metadata and ``Registry.call``.

Discovery, in this order:

1. **Built-ins**, registered explicitly from :data:`tern.tools.builtin.BUILTINS` (origin
   ``builtin``), never through entry points (plan 06a amendment 14).
2. **Entry points** in group ``tern.tools`` (origin ``plugin``). Each entry point resolves to a
   :class:`~tern.tools.contract.ToolSpec` or a non-empty list/tuple of them.
3. **Config paths**: ``config.toml [plugins] paths = [...]`` (origin ``plugin``), absolute paths to
   ``.py`` files, each exporting ``TOOLS: list[ToolSpec]``. Nothing is scanned implicitly: no
   directory is ever auto-loaded, no ``~`` is expanded.

A duplicate tool name (a plugin shadowing a built-in included), a missing, relative or unloadable
path, or an entry point or ``TOOLS`` that resolves to anything else fails loudly with
:class:`PluginLoadError`. Its message names the entry point or the path and the exception type,
never file content (a ``SyntaxError``'s text would quote a source line, so only its line number
is kept and the original exception is not chained).

:func:`describe_registry` (``tern tools describe``) lists built-ins and entry-point plugins only:
it never resolves ``tern.paths``, never reads ``config.toml`` and never loads config-path plugins
(the guard exempts the command as metadata-only).

:meth:`Registry.call` is the single entry point that runs a tool (CLAUDE.md hard rule). In
order: validate the caller (a tern bug otherwise: ``ValueError``, no row) -> resolve the tool ->
validate the args -> tier -> append the audit ``call`` row -> run. The row is written **before**
the tool runs (write-ahead): when the append fails, nothing executes. Every call writes exactly
one row, unknown tools, invalid args and denials included; a tool that fails gets an ``error``
event. Every failure is a :class:`~tern.tools.contract.ToolError` whose message holds field
locations and reason codes, never input values.

Forbidden effects (plan 06b step 11, T6): with an engine, a tool declaring ``filters_write``
(:data:`tern.perms.engine.FORBIDDEN_EFFECTS`) is refused right after it is resolved, before its
args are validated: a ``denied`` row with reason ``forbidden_effect`` (unvalidated args) and
``ToolError`` ``denied``/``forbidden_effect``; nothing else runs.

The tier: without a permission engine (``ToolRuntime.engine`` None) it is the tool's
``effective_tier`` and only R0/R1 tools run; a W-tier call is denied with ``tier_not_enabled``.
With an engine (plan 06b) it is the engine's escalated tier (count and recipient escalation;
a recipient provider that raises ``RecipientsUnavailable`` makes every recipient unknown, any
other provider failure is ``internal``), which the audit row and the log line record.

W1 with an engine (plan 06b step 3): the engine resolves the targets from the index, then live
(``mail.messages.find`` through the runtime's bridge); a refusal (``unknown_mid``,
``mid_conflicted``, ``ambiguous_location``, ``targets_changed``, ``unavailable``) gets a
``decision=error`` row with the code as its reason, and nothing runs. Otherwise the write-ahead
row (``decision=auto``, ``initial_status=tb_accepted``) is appended, the tool runs with the
resolved locations as ``ctx.targets``, the engine finds the targets again for the post state,
and the undo token (prev/post per location; never a ``tb_id`` or content) is recorded and
returned on :attr:`ToolResult.undo_token`.

W2/W3 with an engine (plan 06b step 5): without an approval UI (the engine's notifier is None) the
call is refused at once (``denied`` row, reason ``unavailable``; ``ToolError`` ``unavailable``,
``no_approval_ui``). Otherwise the targets are resolved as for W1, the engine opens an approval
(card from the index, args hash + nonce; at most ``Limits.max_pending_per_caller`` open per
caller, else ``rate_limited``/``pending_cap``) and the call **awaits the decision** (up to
``Limits.approval_ttl_s``). No row exists while it waits; a non-approved outcome's row is written
when the approval resolves (:data:`tern.perms.approvals.OUTCOME_ROWS`). An approved call re-hashes
the args it is about to execute (a difference: ``denied``/``binding_mismatch``), then re-checks
its targets (step 6, M6): every card location (mid, folder id) is found again through
``mail.messages.find``. A location that is gone (moved, deleted) refuses the whole call: row
``approved``, reason ``targets_changed``, not executed, a ``targets_changed`` event, and
``notifier.resolved(approval_id, "targets_changed")``; a bridge failure there is row
``approved``/``unavailable`` (or ``internal``), not executed, announced ``denied``. Otherwise it
appends its ``approved``/``tb_accepted`` row (with ``original_args_sha256`` after a W2 subset)
and runs exactly like a W1 call with the re-check's fresh locations (``ctx.targets``, post
state, undo token).

Write status (plan 06b step 7): once an executed write's undo token is recorded, the engine's
:class:`~tern.perms.write_status.WriteStatus` watches it (after a check against the index's
current state) until every target is seen in its post state, then appends ``observed``.

Undo (plan 06b step 8, :mod:`tern.perms.undo`): :meth:`Registry.undo` (``ui`` only; any other
caller gets a ``denied``/``undo_not_allowed`` row) reads the undo token, finds every target live
and refuses unless each is still in its recorded post state (``state_changed``, an ``error`` row,
nothing written). Otherwise it appends its own write-ahead row (tool ``undo``, W2 if any inverse
is a move else W1, ``auto``/``tb_accepted``), sends the inverse (``mail.update``, ``mail.move``)
through the engine's bridge, appends ``undone`` to the original call and records the undo's own
token (prev/post swapped: undoing it is a redo), which write status then watches. A bridge
failure part-way appends an ``error`` event (with the count of requests applied) and no
``undone``: TB is not transactional.

Ask (decisions #39, a chat in ``manual`` mode): ``Registry.call(..., ask=notifier)`` asks the
user before a call below W2 runs, through the engine's approvals queue
(:mod:`tern.perms.approvals`, "Ask approvals"): once the tier is known, an approval with no targets
opens, its card goes to ``ask`` (not the engine's notifier) and the call waits. Every outcome but
``approved`` writes its row when the approval resolves, exactly as for W2/W3 (user deny
``denied``/``user_denied``, expiry ``timeout``/``expired``, a cancelled turn
``denied``/``cancelled``) and raises the same ``ToolError``; an approved call then runs as it
would have, with ``approved`` in place of ``auto`` on its row (a W1 call's rate window and targets
are checked after the decision). W2/W3 calls ignore ``ask``: they have their own approval, whose
card goes to the engine's notifier (#7/#10's per-action confirmation is never lifted or moved).
Without an engine, ``ask`` refuses the call (``unavailable``, ``no_approval_ui``).

The W1 rate window (plan 06b step 4) is checked before the targets are resolved: a W1 call
whose target count would exceed its caller's or the global window (executed W1 targets in the
audit log plus in-flight reservations) gets a ``decision=denied`` row (reason ``rate_limited``)
and nothing runs. Otherwise its targets are reserved until its row is written (from then on the
audit counts them) or it fails before that.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.metadata
import importlib.util
import json
import logging
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic_core import PydanticSerializationError

from tern._model import BoundaryModel
from tern.bridge.client import ExtensionRequester
from tern.bridge.protocol import BridgeError, Mid
from tern.config import TernConfig
from tern.index.store import Store
from tern.perms import undo
from tern.perms.approvals import (
    OUTCOME_ROWS,
    Approval,
    ApprovalNotifier,
    Approvals,
    ApprovalStatus,
    Outcome,
    PendingCapReached,
    approval_sha256,
)
from tern.perms.audit import (
    CALLER_RE,
    INVALID_TOOL,
    AuditError,
    AuditLog,
    Decision,
    InitialStatus,
    UndoPayload,
    UndoToken,
    UnknownUndoToken,
    valid_caller_label,
)
from tern.perms.engine import (
    MAX_RECIPIENTS,
    Engine,
    RecipientChecks,
    TooManyRecipients,
    count_tier,
)
from tern.perms.ratelimit import RateLimited, Reservation
from tern.perms.targets import LiveTargets, TargetRefused
from tern.perms.undo import UNDO_TOOL
from tern.search.service import SearchService
from tern.tools.builtin import BUILTINS
from tern.tools.context import (
    NO_TARGETS,
    BridgeScopeError,
    ResolvedLocation,
    ScopedBridge,
    ToolContext,
    ToolContextError,
)
from tern.tools.contract import (
    NAME_RE,
    TOOL_RAISABLE_CODES,
    Origin,
    Tier,
    ToolDefinitionError,
    ToolError,
    ToolErrorCode,
    ToolMeta,
    ToolSpec,
)

log = logging.getLogger(__name__)

#: The entry-point group plugins register tools under (#6).
ENTRY_POINT_GROUP = "tern.tools"

#: The name a config-path plugin file exports its tools under.
PLUGIN_EXPORT = "TOOLS"

#: What log lines show for a name that is no registered tool (a model chooses it: the audit row
#: keeps a snake_case name, logs never do).
UNKNOWN_LOG_TOOL = "<unknown>"


class PluginLoadError(Exception):
    """A plugin (entry point or config path) cannot be loaded, or a tool name is taken."""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """A tool in the registry: its spec, origin (set here, never by the tool) and where it came
    from (``builtin``, ``entry point <name> (<value>)`` or ``plugin file <path>``)."""

    spec: ToolSpec
    origin: Origin
    source: str

    def meta(self) -> ToolMeta:
        return self.spec.meta(self.origin)


def _why(exc: BaseException) -> str:
    """A content-free description of a load failure."""
    if isinstance(exc, ToolDefinitionError):
        return f"ToolDefinitionError: {exc}"  # tern-authored, names and rules only
    if isinstance(exc, SyntaxError):
        return f"SyntaxError at line {exc.lineno}"
    return type(exc).__name__


def _as_specs(value: object, source: str) -> list[ToolSpec]:
    """``value`` as a list of tool specs: a ToolSpec, or a non-empty list/tuple of them."""
    if isinstance(value, ToolSpec):
        return [value]
    if isinstance(value, (list, tuple)):
        items: list[object] = list(cast("Sequence[object]", value))
        specs = [v for v in items if isinstance(v, ToolSpec)]
        if items and len(specs) == len(items):
            return specs
    raise PluginLoadError(
        f"{source}: resolves to {type(value).__name__}, not a ToolSpec or a non-empty "
        "list/tuple of ToolSpecs"
    )


def builtin_tools() -> list[RegisteredTool]:
    """The built-ins, from :data:`tern.tools.builtin.BUILTINS`."""
    return [RegisteredTool(s, "builtin", "builtin") for s in BUILTINS]


def entry_point_tools() -> list[RegisteredTool]:
    """The tools of every entry point in group ``tern.tools``, in name order."""
    eps = sorted(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name)
    found: list[RegisteredTool] = []
    for ep in eps:
        source = f"entry point {ep.name} ({ep.value})"
        try:
            value = ep.load()
        except Exception as exc:  # plugin code may raise anything on import: re-raised, loud
            raise PluginLoadError(f"{source}: cannot load ({_why(exc)})") from None
        found.extend(RegisteredTool(s, "plugin", source) for s in _as_specs(value, source))
    return found


def path_tools(path: Path) -> list[RegisteredTool]:
    """The ``TOOLS`` of one config-path plugin file (absolute ``.py`` path)."""
    source = f"plugin file {path}"
    if not path.is_absolute():
        raise PluginLoadError(f"{source}: not an absolute path (no ~ expansion)")
    if path.suffix != ".py":
        raise PluginLoadError(f"{source}: not a .py file")
    if not path.is_file():
        raise PluginLoadError(f"{source}: missing or not a regular file")
    # A unique module name: two files with the same basename (or one file loaded by two
    # registries) never share a module.
    module_name = f"_tern_plugin_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"{source}: not loadable as a Python module")
    module = importlib.util.module_from_spec(spec)
    # Registered while it executes so pydantic and typing can resolve the module's own names.
    # Removed again on every failure path (load error, no TOOLS, malformed TOOLS).
    sys.modules[module_name] = module
    loaded = False
    try:
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # plugin code may raise anything on import: re-raised, loud
            raise PluginLoadError(f"{source}: cannot load ({_why(exc)})") from None
        if not hasattr(module, PLUGIN_EXPORT):
            raise PluginLoadError(f"{source}: does not export {PLUGIN_EXPORT}")
        tools = [RegisteredTool(s, "plugin", source) for s in _as_specs(module.TOOLS, source)]
        loaded = True
        return tools
    finally:
        if not loaded:
            sys.modules.pop(module_name, None)


def discover(plugin_paths: Sequence[Path] = ()) -> list[RegisteredTool]:
    """Built-ins, then entry-point plugins, then ``plugin_paths`` (in the given order)."""
    found = builtin_tools() + entry_point_tools()
    for path in plugin_paths:
        found.extend(path_tools(path))
    return found


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A successful call: the tool's output, whether it holds attacker-controllable text (the
    agent wraps it with ``untrusted.wrap()``), the audit ``call_id`` and, for an executed write,
    its undo token (set by the permission engine after the write, never by the tool; None for
    R0/R1 calls)."""

    output: BoundaryModel
    untrusted_output: bool
    call_id: str
    undo_token: str | None = None


@dataclass(frozen=True, slots=True)
class UndoResult:
    """A successful :meth:`Registry.undo`: the undo's own audit ``call_id``, the ``call_id`` of
    the write it undid (now ``undone``) and the undo's own undo token (undoing it is a redo)."""

    call_id: str
    undone_call_id: str
    undo_token: str


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    """What :meth:`Registry.call` needs to audit a call and build each tool's
    :class:`~tern.tools.context.ToolContext`. Checked at construction (a writable Store, an
    audit that is not an ``AuditLog``, ... is a :class:`~tern.tools.context.ToolContextError`)."""

    audit: AuditLog
    index: Store
    search: SearchService
    #: The extension (e.g. ternd's ``ExtensionLink``), or None when none can attach.
    bridge: ExtensionRequester | None
    config: TernConfig
    #: Parent logger of the tools' loggers (``ctx.log`` is ``log.getChild(<tool name>)``).
    log: logging.Logger
    #: Wall-clock time, epoch seconds (``ctx.clock``).
    clock: Callable[[], float]
    #: The permission engine (plan 06b), or None: then every W-tier call is denied with
    #: ``tier_not_enabled`` (06a behaviour). ternd always passes one.
    engine: Engine | None = None
    #: tern's state dir (``ctx.state_dir``): ternd's ``paths.resolve().state``, smoke's and the
    #: probe's synthetic one. None: tools that need one (``tb_*``) are ``unavailable``.
    state_dir: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.audit, AuditLog):
            raise ToolContextError("ToolRuntime.audit must be an AuditLog")
        if self.engine is not None:
            if not isinstance(self.engine, Engine):
                raise ToolContextError("ToolRuntime.engine must be an Engine or None")
            # One audit log (the rate window counts the rows the registry writes), one clock
            # and one extension link for the registry and its engine.
            if self.engine.audit is not self.audit:
                raise ToolContextError("ToolRuntime.engine must use ToolRuntime.audit")
            if self.engine.clock is not self.clock:
                raise ToolContextError("ToolRuntime.engine must use ToolRuntime.clock")
            if self.engine.bridge is not self.bridge:
                raise ToolContextError("ToolRuntime.engine must use ToolRuntime.bridge")
            if self.engine.index is not self.index:
                raise ToolContextError("ToolRuntime.engine must use ToolRuntime.index")
        # The checks every call's context gets, run once up front.
        self._context(ScopedBridge(self.bridge, (), tool="registry"), self.log)

    def _context(
        self,
        bridge: ScopedBridge,
        tool_log: logging.Logger,
        targets: Mapping[str, tuple[ResolvedLocation, ...]] = NO_TARGETS,
    ) -> ToolContext:
        return ToolContext(
            index=self.index,
            search=self.search,
            bridge=bridge,
            log=tool_log,
            config=self.config,
            clock=self.clock,
            targets=targets,
            state_dir=self.state_dir,
        )

    def context(
        self, spec: ToolSpec, targets: Mapping[str, tuple[ResolvedLocation, ...]] = NO_TARGETS
    ) -> ToolContext:
        """A fresh context for one call of ``spec``: its bridge is scoped to its effects, and its
        message writes to the tb_ids of ``targets`` (the engine-resolved locations of a W1 call;
        empty for R0/R1, so no message write)."""
        tb_ids = frozenset(loc.tb_id for locs in targets.values() for loc in locs)
        return self._context(
            ScopedBridge(self.bridge, spec.effects, tool=spec.name, target_tb_ids=tb_ids),
            self.log.getChild(spec.name),
            targets,
        )


@functools.cache
def _field_names(model: type[BoundaryModel]) -> frozenset[str]:
    """Every property name (and alias) in ``model``'s JSON Schema, nested models included: the
    location parts a validation message may name. Anything else (an extra key the caller sent)
    is caller-chosen text and is shown as ``*``."""
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in cast("list[object]", node):
                walk(item)
        elif isinstance(node, dict):
            d = cast("dict[str, object]", node)
            props = d.get("properties")
            if isinstance(props, dict):
                names.update(cast("dict[str, object]", props))
            for value in d.values():
                walk(value)

    walk(model.model_json_schema(by_alias=False))
    walk(model.model_json_schema(by_alias=True))
    return frozenset(names)


_MAX_REPORTED_ERRORS = 10


def _validation_message(exc: ValidationError, model: type[BoundaryModel]) -> str:
    """``invalid arguments: <loc>: <pydantic error type>; ...``: locations and codes only."""
    known = _field_names(model)
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    parts: list[str] = []
    for err in errors[:_MAX_REPORTED_ERRORS]:
        loc = ".".join(str(p) if isinstance(p, int) or p in known else "*" for p in err["loc"])
        parts.append(f"{loc or '<root>'}: {err['type']}")
    if len(errors) > _MAX_REPORTED_ERRORS:
        parts.append(f"... {len(errors) - _MAX_REPORTED_ERRORS} more")
    return "invalid arguments: " + "; ".join(parts)


def _validate(model: type[BoundaryModel], args: Mapping[str, Any]) -> BoundaryModel | str:
    """The validated input, or the content-free validation message. Anything else a validator
    raises (pydantic wraps only ValueError/AssertionError) propagates to :meth:`Registry.call`."""
    try:
        return model.model_validate(args)
    except ValidationError as exc:
        return _validation_message(exc, model)


def _dump(inp: BoundaryModel) -> dict[str, Any] | None:
    """Validated args as JSON, or None when the In model cannot serialize them (a custom
    serializer is tool code, and pydantic's message then quotes its text)."""
    try:
        dumped: dict[str, Any] = json.loads(inp.model_dump_json())
    except PydanticSerializationError:
        return None
    return dumped


#: Validates provenance entries against the bridge's own mid type (one definition, no drift).
_MID: TypeAdapter[str] = TypeAdapter(Mid, config=ConfigDict(hide_input_in_errors=True))


def _provenance(value: object) -> tuple[str, ...]:
    """``value`` as a tuple of mids; anything else is a ``ValueError`` (a tern bug) whose
    message never quotes the value."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("provenance must be a sequence of mids")
    mids: list[str] = []
    for item in cast("Sequence[object]", value):
        if not isinstance(item, str):
            raise ValueError("provenance must be a sequence of mids")
        try:
            mids.append(_MID.validate_python(item, strict=True))
        except ValidationError:
            raise ValueError("provenance must be a sequence of mids") from None
    return tuple(mids)


_EXC_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


def _matches_snapshot(approval: Approval) -> bool:
    """Whether every target to re-check is a card location: its mid and folder ids as the
    approval's snapshot recorded them when the card was opened."""
    return all(approval.snapshot.get(t.mid) == t.folder_ids for t in approval.live.targets)


def _exc_name(value: object) -> str:
    """A type name fit for an audit event detail (``[A-Za-z0-9_]``, at most 64)."""
    cls = value if isinstance(value, type) else type(value)
    return _EXC_NAME_RE.sub("_", cls.__name__)[:64] or "unnamed"


def _as_tool_error(exc: Exception) -> ToolError:
    """How a tool's failure is reported (plan 06a step 4 provisional choices)."""
    if isinstance(exc, ToolError):
        if exc.code in TOOL_RAISABLE_CODES:
            return exc
        return ToolError("internal", "tool raised a registry-only error code", reason="bad_code")
    if isinstance(exc, BridgeError):
        if exc.name == "unavailable":
            return ToolError(
                "unavailable", "the Thunderbird extension is not available", reason="unavailable"
            )
        if exc.name == "tb_error":
            return ToolError("unavailable", "Thunderbird reported an error", reason="tb_error")
        return ToolError("internal", f"bridge error {exc.name}", reason=exc.name)
    if isinstance(exc, BridgeScopeError):
        return ToolError(
            "internal", "tool called a bridge method outside its effects", reason="bridge_scope"
        )
    return ToolError("internal", f"tool failed ({_exc_name(exc)})", reason="tool_failed")


class Registry:
    """The tools by name. A duplicate name is a :class:`PluginLoadError` naming both sources.

    Without a ``runtime`` the registry only describes tools (``tern tools describe``);
    :meth:`call` needs one.
    """

    def __init__(
        self, tools: Iterable[RegisteredTool], *, runtime: ToolRuntime | None = None
    ) -> None:
        if runtime is not None and not isinstance(runtime, ToolRuntime):
            raise TypeError("runtime must be a ToolRuntime")
        self._runtime = runtime
        self._tools: dict[str, RegisteredTool] = {}
        for t in tools:
            if (have := self._tools.get(t.spec.name)) is not None:
                raise PluginLoadError(
                    f"{t.source}: tool name {t.spec.name} is already registered by {have.source}"
                )
            self._tools[t.spec.name] = t

    @classmethod
    def discover(
        cls, plugin_paths: Sequence[Path] = (), *, runtime: ToolRuntime | None = None
    ) -> Registry:
        """Built-ins + entry points + ``plugin_paths`` (ternd passes ``config.plugins.paths``)."""
        return cls(discover(plugin_paths), runtime=runtime)

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[ToolMeta]:
        """Every tool's ``tern-tool/1`` metadata, sorted by name."""
        return [self._tools[n].meta() for n in self.names()]

    # -- calls ----------------------------------------------------------------------------------

    async def call(
        self,
        caller: str,
        tool: str,
        args: Mapping[str, Any],
        *,
        caller_label: str | None = None,
        provenance: Sequence[str] = (),
        ask: ApprovalNotifier | None = None,
    ) -> ToolResult:
        """Run ``tool`` for ``caller`` with ``args`` (module docstring). ``provenance``: the mids
        whose untrusted text led to this call (the approval card shows them; plan 08a passes the
        session's). ``ask``: ask the user through this notifier before a call below W2 runs
        (module docstring, "Ask"; None: no asking, the tier rules alone). Raises
        :class:`~tern.tools.contract.ToolError` on every failure, ``ValueError`` for an invalid
        caller id, label or provenance (a tern bug: no row) and ``RuntimeError`` without a
        runtime."""
        if not isinstance(caller, str) or not CALLER_RE.fullmatch(caller):
            raise ValueError("invalid caller id (ui or agent|mcp|job:<id>)")
        if ask is not None and not isinstance(ask, ApprovalNotifier):
            raise TypeError("ask must provide async requested(card) and resolved(id, status)")
        if caller_label is not None and not valid_caller_label(caller_label):
            raise ValueError("caller_label must be 1..128 printable characters")
        prov = _provenance(provenance)
        rt = self._runtime
        if rt is None:
            raise RuntimeError("this registry has no ToolRuntime: it can describe tools only")
        call = _Call(rt, caller, caller_label)
        call.provenance = prov

        reg = self._tools.get(tool) if isinstance(tool, str) else None
        if reg is None:
            # The name is attacker-reachable (a model chooses it): recorded only when it is a
            # valid tool name, never echoed.
            named = isinstance(tool, str) and NAME_RE.fullmatch(tool) is not None
            call.tool = tool if named else INVALID_TOOL
            call.append(decision="error", reason="unknown_tool", args=args, validated=False)
            raise call.fail(ToolError("unknown_tool", "no tool has this name"))
        spec = reg.spec
        call.tool = spec.name
        call.known = True
        call.tier = tier = spec.effective_tier
        if rt.engine is not None and rt.engine.forbidden_effects(spec):
            # T6 (plan 06b step 11): refused before any tool code (validators, count_targets,
            # recipients) runs and before any approval; the args were never validated.
            call.append(decision="denied", reason="forbidden_effect", args=args, validated=False)
            err = ToolError(
                "denied",
                "this tool declares an effect tern never allows",
                reason="forbidden_effect",
            )
            raise call.fail(err)

        # Every audit append runs outside the `except` that caught a validator's, serializer's,
        # count_targets' or tool's exception (the helpers below return the outcome): an audit
        # failure raised inside it would carry that exception, whose text may quote content, as
        # its implicit __context__.
        inp: BoundaryModel | str = ""
        validate_failed: str | None = None
        try:
            inp = _validate(spec.input_model, args)
        except Exception as exc:  # tool-authored In-model validator: recorded, ToolError below
            validate_failed = _exc_name(exc)
        if validate_failed is not None:
            call.append(decision="error", reason="internal", args=args, validated=False)
            err = ToolError(
                "internal", f"cannot validate the arguments ({validate_failed})", reason="validate"
            )
            raise call.fail(err)
        if isinstance(inp, str):
            call.append(decision="error", reason="invalid_params", args=args, validated=False)
            raise call.fail(ToolError("invalid_params", inp))

        # Validated args as JSON (inf/nan -> null, so the append never refuses them).
        dumped = _dump(inp)
        if dumped is None:
            call.append(decision="error", reason="internal", args=args, validated=False)
            err = ToolError("internal", "cannot serialize the arguments", reason="serialize")
            raise call.fail(err)

        # The targets: the Mid fields are read by tern (a failure is a tern bug and stays raw);
        # count_targets is tool code.
        call.target_mids = spec.target_mids(inp)
        count_failed: str | None = None
        try:
            call.target_count = spec.target_count(inp)
        except Exception as exc:  # tool-authored count_targets: recorded, ToolError raised below
            count_failed = _exc_name(exc)
        if count_failed is not None:
            call.append(decision="error", reason="internal", args=args, validated=False)
            err = ToolError("internal", f"cannot count targets ({count_failed})", reason="targets")
            raise call.fail(err)

        engine = rt.engine
        if engine is not None:
            # Recipient lookups run tool code (``recipients``) and the provider (plugin/bridge
            # code); count escalation uses the count recorded above (count_targets ran once).
            # The count-escalated tier is known already: a row written before the lookup ends
            # (failure, cancel, too many recipients) carries at least that tier.
            call.tier = tier = count_tier(spec.effective_tier, call.target_count)
            recipients = RecipientChecks()
            lookup_failed: str | None = None
            too_many = False
            try:
                recipients = await engine.check_recipients(spec, inp)
            except asyncio.CancelledError:
                call.append_cancelled(args=dumped)
                raise
            except TooManyRecipients:
                too_many = True
            except Exception as exc:  # tool/provider code: recorded, ToolError raised below
                lookup_failed = _exc_name(exc)
            if too_many:
                call.append(decision="error", reason="invalid_params", args=dumped, validated=True)
                err = ToolError("invalid_params", f"more than {MAX_RECIPIENTS} distinct recipients")
                raise call.fail(err)
            if lookup_failed is not None:
                call.append(decision="error", reason="internal", args=dumped, validated=True)
                err = ToolError(
                    "internal",
                    f"cannot check the recipients ({lookup_failed})",
                    reason="recipients",
                )
                raise call.fail(err)
            call.recipients_unavailable = recipients.unavailable
            call.recipients = recipients
            call.tier = tier = engine.tier(spec, call.target_count, recipients)

        if tier.rank >= Tier.W1.rank and engine is None:
            call.append(decision="denied", reason="tier_not_enabled", args=dumped, validated=True)
            err = ToolError(
                "tier_not_enabled", f"{tier.value} tools are not enabled (no permission engine)"
            )
            raise call.fail(err)

        asked: Approval | None = None
        if ask is not None and tier.rank < Tier.W2.rank:
            if engine is None:
                call.append(decision="denied", reason="unavailable", args=dumped, validated=True)
                err = ToolError(
                    "unavailable",
                    "asking before a call needs the permission engine",
                    reason="no_approval_ui",
                )
                raise call.fail(err)
            asked = await self._ask(call, engine, spec, tier, inp, dumped, ask)
        # An asked call that was approved runs as it would have, recorded ``approved``.
        executes: Decision = "auto" if asked is None else "approved"

        live: LiveTargets | None = None
        if engine is not None and tier.rank >= Tier.W2.rank:
            inp, live = await self._approve(call, engine, spec, tier, inp, dumped)
        elif engine is not None and tier is Tier.W1:
            reservation = self._reserve_w1(call, engine, dumped)
            try:
                live = await self._resolve_targets(call, engine, spec, inp, dumped)
                call.append(
                    decision=executes,
                    reason=None,
                    args=dumped,
                    validated=True,
                    initial="tb_accepted",
                )
            finally:
                # The row now counts the targets (no await since the append), or the call failed
                # before it executed: either way the reservation ends here.
                reservation.release()
        else:
            call.append(decision=executes, reason=None, args=dumped, validated=True, initial="ok")
        if asked is not None:
            assert engine is not None
            self._announce_asked(call, engine.approvals, asked)
        ctx = rt.context(spec) if live is None else rt.context(spec, live.locations)
        failed: tuple[ToolError, str] | None = None
        out: object = None
        try:
            out = await spec.impl(ctx, inp)
        except Exception as exc:  # any tool failure: error event, then ToolError (never a default)
            log.debug("tool %s failed", spec.name, exc_info=True)  # may quote content: DEBUG only
            failed = (_as_tool_error(exc), _exc_name(exc))
        except BaseException as exc:  # cancellation, KeyboardInterrupt, SystemExit
            # Recorded so the derived status is never a stale "ok", then re-raised unchanged: an
            # audit failure here is logged at ERROR by record_error and must not replace it.
            if isinstance(exc, asyncio.CancelledError):
                aborted = ToolError("internal", "call cancelled", reason="cancelled")
                if call.record_error(aborted, "cancel", code="cancelled") is None:
                    call.log_done("cancelled")
            else:
                aborted = ToolError("internal", "call aborted", reason="aborted")
                if call.record_error(aborted, _exc_name(exc), code="aborted") is None:
                    call.log_done("aborted")
            raise
        if failed is not None:
            err, exc_name = failed
            call.error_event(err, exc_name)
            # A ToolError the tool raised passes through: drop the chain it may carry.
            raise call.fail(err) from None
        if not isinstance(out, spec.output_model):
            err = ToolError("internal", "tool returned the wrong output type", reason="bad_output")
            call.error_event(err, _exc_name(out))
            raise call.fail(err)
        undo_token = None
        if live is not None:
            assert engine is not None
            undo_token = await self._record_undo(call, engine, live)
        call.log_done(None)
        assert call.call_id is not None  # the "auto" row above
        return ToolResult(
            output=out,
            untrusted_output=spec.untrusted_output,
            call_id=call.call_id,
            undo_token=undo_token,
        )

    # -- undo (plan 06b step 8) -----------------------------------------------------------------

    async def undo(self, caller: str, token: str) -> UndoResult:
        """Undo the write whose undo token is ``token``, for ``caller`` (module docstring of
        :mod:`tern.perms.undo`). Only ``ui`` may undo. Writes the undo's own write-ahead row
        (tool ``undo``, tier W2 if any inverse is a move else W1, ``decision=auto``,
        ``initial_status=tb_accepted``) once every target was found live in its recorded post
        state, sends the inverse through the engine's bridge, then appends ``undone`` to the
        original call and records the undo's own token (prev/post swapped: undoing it is a
        redo). Raises :class:`~tern.tools.contract.ToolError` on every refusal or failure (each
        with its row): ``denied`` (``undo_not_allowed``), ``invalid_params`` (unknown token),
        ``state_changed`` (used token, an undo of it in progress, an unsupported target, a
        target no longer in its post state or no longer locatable), ``unavailable``/``internal``
        (bridge, audit). ``ValueError`` for an invalid caller id and ``RuntimeError`` without a
        runtime or an engine (tern bugs: no row)."""
        if not isinstance(caller, str) or not CALLER_RE.fullmatch(caller):
            raise ValueError("invalid caller id (ui or agent|mcp|job:<id>)")
        rt = self._runtime
        if rt is None:
            raise RuntimeError("this registry has no ToolRuntime: it can describe tools only")
        engine = rt.engine
        if engine is None:
            raise RuntimeError("undo needs a permission engine")
        call = _Call(rt, caller, None)
        call.tool = UNDO_TOOL
        call.known = True
        call.tier = Tier.W1
        if caller != "ui":
            # The token is not read for another caller: its row records no args.
            call.append(decision="denied", reason="undo_not_allowed", args={}, validated=True)
            err = ToolError("denied", "only the user (ui) may undo", reason="undo_not_allowed")
            raise call.fail(err)

        record: UndoToken | None = None
        read_failed: str | None = None
        unknown = not isinstance(token, str)
        try:
            if not unknown:
                record = rt.audit.undo_token(token)
        except UnknownUndoToken:
            unknown = True
        except (sqlite3.Error, ValidationError) as exc:
            # A stored payload that no longer validates is an unreadable token, like a failed
            # read (type name only: the error may quote the stored payload).
            read_failed = _exc_name(exc)
        if read_failed is not None:
            call.append(decision="error", reason="internal", args={}, validated=True)
            err = ToolError(
                "internal", f"cannot read the undo token ({read_failed})", reason="audit_read"
            )
            raise call.fail(err)
        if unknown or record is None:
            call.append(decision="error", reason="invalid_params", args={}, validated=True)
            raise call.fail(ToolError("invalid_params", "no such undo token", reason="unknown"))

        payload = record.payload
        args = {"undo_of": record.call_id}
        call.target_mids = undo.target_mids(payload)
        call.target_count = len(call.target_mids)
        if record.used:
            raise self._undo_refused(
                call,
                args,
                TargetRefused("state_changed", "already_undone", "this write is already undone"),
            )
        if not engine.undo_claims.claim(record.token):
            raise self._undo_refused(
                call,
                args,
                TargetRefused("state_changed", "in_progress", "this write is being undone"),
            )
        try:
            return await self._undo_claimed(call, engine, record, args)
        finally:
            engine.undo_claims.release(record.token)

    async def _undo_claimed(
        self, call: _Call, engine: Engine, record: UndoToken, args: dict[str, Any]
    ) -> UndoResult:
        """:meth:`undo` once its token is claimed: state check, row, inverse, events, token."""
        rt = call.rt
        payload = record.payload
        refused: TargetRefused | None = None
        located: list[undo.LocatedTarget] = []
        try:
            undo.check_payload(payload)
            call.tier = undo.inverse_tier(payload)
            located = await engine.locate_undo(payload)
        except asyncio.CancelledError:
            call.append_cancelled(args=args)
            raise
        except TargetRefused as exc:
            refused = exc
        if refused is not None:
            raise self._undo_refused(call, args, refused)

        # Write-ahead: the row before the first inverse request.
        call.append(decision="auto", reason=None, args=args, validated=True, initial="tb_accepted")
        assert call.call_id is not None
        bridge = engine.bridge
        assert bridge is not None  # locate found every target through it
        requests = undo.inverse_requests(located)
        applied = 0
        failed: tuple[ToolError, str] | None = None
        try:
            for method, params in requests:
                await bridge.request(method, params)
                applied += 1
        except BridgeError as exc:
            failed = (_as_tool_error(exc), exc.name)
        except BaseException as exc:  # cancellation or a bug: recorded, then re-raised unchanged
            if isinstance(exc, asyncio.CancelledError):
                name, code = "cancel", "cancelled"
                aborted = ToolError("internal", "undo cancelled", reason="cancelled")
            else:
                name, code = _exc_name(exc), "internal"
                aborted = ToolError("internal", "undo failed", reason="inverse_failed")
            # An audit failure here is logged at ERROR and must not replace the exception.
            if call.inverse_failed(aborted, name, applied, code=code) is None:
                call.log_done(code)
            raise
        if failed is not None:
            err, name = failed
            # TB is not transactional: the requests already sent stay applied. No ``undone``.
            failure = call.inverse_failed(err, name, applied)
            if failure is not None:
                raise ToolError(
                    "internal",
                    "undo failed and its audit event write failed",
                    reason="audit_failed",
                ) from failure
            raise call.fail(
                ToolError(
                    err.code,
                    f"the undo failed after {applied} of {len(requests)} "
                    "changes; check the messages",
                    reason="partial" if applied else "inverse_failed",
                )
            )

        inverse = undo.inverse_payload(payload)
        audit_failure: BaseException | None = None
        new_token = ""
        original_pending = False
        try:
            rt.audit.append_event(record.call_id, "undone", {"undo_call_id": call.call_id})
            # Once the original is undone it is no longer watched, whatever happens next (its
            # post state will not come back: it must not turn into a stale write).
            original_pending = engine.write_status.forget(record.call_id)
            new_token = rt.audit.add_undo_token(call.call_id, inverse)
        except (sqlite3.Error, ValueError, AuditError) as exc:
            audit_failure = exc
        if audit_failure is not None:
            call.error_event(
                ToolError("internal", "undo not recorded", reason="audit_failed"),
                _exc_name(audit_failure),
            )
            raise call.fail(
                ToolError(
                    "internal",
                    "the undo ran but it could not be fully recorded",
                    reason="audit_failed",
                )
            )
        # An original the index had not seen yet: the index still shows the undo's post state
        # (the original prev state), so the check on register would mark the undo ``observed``
        # with nothing confirming TB applied it. Only a later index event may.
        self._watch(call.call_id, engine, inverse, check=not original_pending)
        call.log_done(None)
        return UndoResult(call_id=call.call_id, undone_call_id=record.call_id, undo_token=new_token)

    @staticmethod
    def _undo_refused(call: _Call, args: dict[str, Any], refused: TargetRefused) -> ToolError:
        """An undo refused before anything was written: its ``decision=error`` row (the code as
        its reason) and the ToolError to raise."""
        call.append(decision="error", reason=refused.code, args=args, validated=True)
        return call.fail(ToolError(refused.code, refused.message, reason=refused.reason))

    async def _approve(
        self,
        call: _Call,
        engine: Engine,
        spec: ToolSpec,
        tier: Tier,
        inp: BoundaryModel,
        dumped: dict[str, Any],
    ) -> tuple[BoundaryModel, LiveTargets]:
        """A W2/W3 call's approval (plan 06b step 5, module docstring): returns the input and
        the live targets to execute once approved and its ``approved`` row is written; every
        other outcome raises (its row written when the approval resolved)."""
        approvals = engine.approvals
        if approvals.notifier is None:
            call.append(decision="denied", reason="unavailable", args=dumped, validated=True)
            err = ToolError(
                "unavailable",
                f"{tier.value} calls need approval and no approval UI is attached",
                reason="no_approval_ui",
            )
            raise call.fail(err)
        live = await self._resolve_targets(call, engine, spec, inp, dumped)
        record = self._outcome_recorder(call, dumped)

        capped: PendingCapReached | None = None
        approval: Approval | None = None
        try:
            approval = approvals.open(
                caller=call.caller,
                spec=spec,
                tier=tier,
                inp=inp,
                args=dumped,
                live=live,
                target_mids=call.target_mids,
                targets=engine.card_targets(live.targets),
                recipients=None if spec.recipients is None else tuple(call.recipients),
                provenance=engine.card_provenance(call.provenance),
                record=record,
            )
        except PendingCapReached as exc:
            capped = exc
        if capped is not None or approval is None:
            assert capped is not None
            call.append(decision="denied", reason="rate_limited", args=dumped, validated=True)
            raise call.fail(ToolError("rate_limited", capped.message, reason="pending_cap"))

        outcome = await approvals.wait(approval)
        if outcome != "approved":
            if approval.row_failure is not None:
                raise ToolError(
                    "internal", "audit log write failed; nothing was run", reason="audit_failed"
                ) from approval.row_failure
            code, why = _OUTCOME_CODES[outcome], _OUTCOME_REASONS[outcome]
            err = ToolError(code, _OUTCOME_MESSAGES[outcome], reason=why)
            raise call.fail(err)

        # Approved: what executes must hash to what was approved (and narrowed, after a subset).
        exec_args = _dump(approval.inp)
        if exec_args is None or approval_sha256(spec.name, exec_args) != approval.args_sha256:
            call.append(decision="denied", reason="binding_mismatch", args=dumped, validated=True)
            approvals.announce(approval, "denied")
            err = ToolError(
                "denied",
                "the arguments to execute differ from the approved ones",
                reason="binding_mismatch",
            )
            raise call.fail(err)
        if approval.narrowed:
            call.target_mids = list(approval.target_mids)
            call.target_count = len(approval.target_mids)
        # The execution re-check (M6, step 6): the card's locations found again; the call runs
        # with this fresh resolution, or not at all.
        live = await self._recheck(call, engine, approval, exec_args)
        call.append(
            decision="approved",
            reason=None,
            args=exec_args,
            validated=True,
            initial="tb_accepted",
            original_args_sha256=approval.original_args_sha256,
        )
        try:
            approvals.announce(approval, "approved")
        except Exception as exc:  # a notifier bug after the row: error event, then re-raised
            # The row says tb_accepted and nothing will run: the error event derives "error", so
            # the row is never counted as an accepted write that was not applied.
            failed = ToolError("internal", "approval UI failed", reason="notifier_failed")
            if call.record_error(failed, _exc_name(exc), code="internal") is None:
                call.log_done("internal")
            raise
        return approval.inp, live

    async def _ask(
        self,
        call: _Call,
        engine: Engine,
        spec: ToolSpec,
        tier: Tier,
        inp: BoundaryModel,
        dumped: dict[str, Any],
        ask: ApprovalNotifier,
    ) -> Approval:
        """An ask approval for a call below W2 (module docstring, "Ask"): returns the approved
        approval (the caller then writes the ``approved`` row and runs the call); every other
        outcome raises, its row written when the approval resolved."""
        approvals = engine.approvals
        capped: PendingCapReached | None = None
        approval: Approval | None = None
        try:
            approval = approvals.open(
                caller=call.caller,
                spec=spec,
                tier=tier,
                inp=inp,
                args=dumped,
                live=_NO_LIVE_TARGETS,
                target_mids=call.target_mids,
                targets=(),
                recipients=None if spec.recipients is None else tuple(call.recipients),
                provenance=engine.card_provenance(call.provenance),
                record=self._outcome_recorder(call, dumped),
                ask=ask,
            )
        except PendingCapReached as exc:
            capped = exc
        if capped is not None or approval is None:
            assert capped is not None
            call.append(decision="denied", reason="rate_limited", args=dumped, validated=True)
            raise call.fail(ToolError("rate_limited", capped.message, reason="pending_cap"))
        outcome = await approvals.wait(approval)
        if outcome != "approved":
            if approval.row_failure is not None:
                raise ToolError(
                    "internal", "audit log write failed; nothing was run", reason="audit_failed"
                ) from approval.row_failure
            code, why = _OUTCOME_CODES[outcome], _OUTCOME_REASONS[outcome]
            raise call.fail(ToolError(code, _OUTCOME_MESSAGES[outcome], reason=why))
        # No subset exists for an ask approval, so the args are the ones asked about; the binding
        # is checked all the same, as for W2/W3.
        if approval_sha256(spec.name, dumped) != approval.args_sha256:
            call.append(decision="denied", reason="binding_mismatch", args=dumped, validated=True)
            approvals.announce(approval, "denied")
            err = ToolError(
                "denied",
                "the arguments to execute differ from the approved ones",
                reason="binding_mismatch",
            )
            raise call.fail(err)
        return approval

    @staticmethod
    def _announce_asked(call: _Call, approvals: Approvals, approval: Approval) -> None:
        """Tell the asking UI its approved call is about to run (after its ``approved`` row); a
        notifier bug is an ``error`` event on the row, then re-raised, as for W2/W3."""
        try:
            approvals.announce(approval, "approved")
        except Exception as exc:  # a notifier bug after the row: error event, then re-raised
            failed = ToolError("internal", "approval UI failed", reason="notifier_failed")
            if call.record_error(failed, _exc_name(exc), code="internal") is None:
                call.log_done("internal")
            raise

    @staticmethod
    def _outcome_recorder(
        call: _Call, dumped: dict[str, Any]
    ) -> Callable[[Outcome], BaseException | None]:
        """The row writer the approvals queue calls for a non-approved outcome."""

        def record(outcome: Outcome) -> BaseException | None:
            decision, reason = OUTCOME_ROWS[outcome]
            code = "cancelled" if outcome == "cancelled" else _OUTCOME_CODES[outcome]
            failure = call.append_quiet(decision=decision, reason=reason, args=dumped, code=code)
            if failure is None and outcome in ("cancelled", "notifier_failed"):
                call.log_done(code)  # no ToolError is raised for these: the call's log line
            return failure

        return record

    async def _recheck(
        self, call: _Call, engine: Engine, approval: Approval, args: dict[str, Any]
    ) -> LiveTargets:
        """The execution re-check of an approved call (plan 06b step 6): every card location
        (mid, folder id) is found again just before the row. Returns the fresh live targets the
        call executes with. Targets that no longer match the card's snapshot write the
        ``denied``/``binding_mismatch`` row and raise ``denied``. A refusal writes the
        ``approved`` row with the refusal's code as its reason and no initial status (not
        executed), plus a ``targets_changed`` event for ``targets_changed``, announces
        ``targets_changed`` (any other refusal: ``denied``) and raises the refusal's ToolError. A
        cancel writes the ``denied``/``cancelled`` row and announces ``denied``; any other
        exception writes the ``approved``/``internal`` row, announces ``denied`` and propagates
        unchanged. Nothing executes in any of these cases."""
        approvals = engine.approvals
        if not _matches_snapshot(approval):
            call.append(decision="denied", reason="binding_mismatch", args=args, validated=True)
            self._announce(call, approvals, approval, "denied")
            err = ToolError(
                "denied",
                "the targets to execute differ from the approved ones",
                reason="binding_mismatch",
            )
            raise call.fail(err)
        try:
            return await engine.recheck(approval.live)
        except asyncio.CancelledError:
            call.append_cancelled(args=args)
            self._announce(call, approvals, approval, "denied")
            raise
        except TargetRefused as exc:
            refused = exc
        except BaseException:
            # Not a refusal (a bug in the re-check, or an interrupt): the approval was granted,
            # so it still leaves its row and the UI hears it ended; the exception propagates.
            failure = call.append_quiet(
                decision="approved",
                reason="internal",
                args=args,
                code="internal",
                original_args_sha256=approval.original_args_sha256,
            )
            if failure is None:
                call.log_done("internal")
            self._announce(call, approvals, approval, "denied")
            raise
        # Outside the except: an audit failure must not carry the refusal as its __context__.
        changed = refused.code == "targets_changed"
        try:
            call.append(
                decision="approved",
                reason=refused.code,
                args=args,
                validated=True,
                original_args_sha256=approval.original_args_sha256,
            )
            if changed:
                call.targets_changed_event(refused.reason)
        finally:
            # The UI hears how its approval ended whether or not the audit write worked.
            self._announce(call, approvals, approval, "targets_changed" if changed else "denied")
        raise call.fail(ToolError(refused.code, refused.message, reason=refused.reason))

    @staticmethod
    def _announce(
        call: _Call, approvals: Approvals, approval: Approval, status: ApprovalStatus
    ) -> None:
        """``notifier.resolved`` for a call that is already failing with its own exception: a
        notifier failure is logged at ERROR (``notifier_failed``) and never replaces it."""
        try:
            approvals.announce(approval, status)
        except Exception as exc:  # the call's own error wins; this one is logged, not dropped
            log.error(
                "approval UI failed",
                extra={
                    "tool": call.log_tool,
                    "caller": call.caller,
                    "reason": "notifier_failed",
                    "error": _exc_name(exc),
                },
            )

    def _reserve_w1(self, call: _Call, engine: Engine, dumped: dict[str, Any]) -> Reservation:
        """Reserve a W1 call's targets in the rate window (plan 06b step 4). A call that would
        exceed it appends the ``denied``/``rate_limited`` row and raises ``rate_limited``; an
        audit read failure appends an ``error``/``internal`` row and raises ``internal``
        (``rate_window``). Synchronous: no await between the check and the reservation."""
        limited: RateLimited | None = None
        read_failed: str | None = None
        try:
            return engine.reserve_w1(call.caller, call.target_count)
        except RateLimited as exc:
            limited = exc
        except sqlite3.Error as exc:
            read_failed = _exc_name(exc)
        # Outside the except (as the other appends): an audit failure carries no __context__.
        if limited is not None:
            call.append(decision="denied", reason="rate_limited", args=dumped, validated=True)
            raise call.fail(ToolError("rate_limited", limited.message, reason=limited.reason))
        call.append(decision="error", reason="internal", args=dumped, validated=True)
        err = ToolError(
            "internal", f"cannot read the W1 rate window ({read_failed})", reason="rate_window"
        )
        raise call.fail(err)

    async def _resolve_targets(
        self,
        call: _Call,
        engine: Engine,
        spec: ToolSpec,
        inp: BoundaryModel,
        dumped: dict[str, Any],
    ) -> LiveTargets:
        """A W1 call's targets, resolved from the index and live (plan 06b step 3). A refusal
        appends the ``decision=error`` row (the code as its reason) and raises its ToolError;
        a cancel appends the ``denied``/``cancelled`` row. Anything else is a tern bug and
        propagates."""
        try:
            return await engine.find_live(engine.resolve_index(spec, inp, call.target_mids))
        except asyncio.CancelledError:
            call.append_cancelled(args=dumped)
            raise
        except TargetRefused as exc:
            refused = exc
        # Outside the except: an audit failure must not carry the refusal as its __context__.
        call.append(decision="error", reason=refused.code, args=dumped, validated=True)
        raise call.fail(ToolError(refused.code, refused.message, reason=refused.reason))

    async def _record_undo(self, call: _Call, engine: Engine, live: LiveTargets) -> str | None:
        """After a W1 write ran: the post state (the engine finds every target again) and the
        undo token, or None for a call without target mids. The write already happened, so a
        failure here is recorded as the call's ``error`` event and raised (never a result
        without its token): a bridge failure as ``unavailable``/``internal`` (reason
        ``post_state``), an audit failure as ``internal`` (``audit_failed``)."""
        refused: TargetRefused | None = None
        try:
            undo_targets = await engine.post_state(live)
        except asyncio.CancelledError:
            aborted = ToolError("internal", "call cancelled", reason="cancelled")
            if call.record_error(aborted, "cancel", code="cancelled") is None:
                call.log_done("cancelled")
            raise
        except TargetRefused as exc:
            refused = exc
            undo_targets = []
        if refused is not None:
            err = ToolError(
                refused.code,
                "the write was sent but its new state could not be read back (no undo token)",
                reason="post_state",
            )
            call.error_event(err, refused.reason)
            raise call.fail(err)
        if not undo_targets:
            return None
        assert call.call_id is not None
        try:
            payload = UndoPayload(targets=undo_targets)
            token = call.rt.audit.add_undo_token(call.call_id, payload)
        except (sqlite3.Error, ValueError, AuditError) as exc:
            failure: BaseException = exc
        else:
            self._watch(call.call_id, engine, payload)
            return token
        call.error_event(
            ToolError("internal", "undo token not recorded", reason="audit_failed"),
            _exc_name(failure),
        )
        raise call.fail(
            ToolError(
                "internal",
                "the write ran but its undo token could not be recorded",
                reason="audit_failed",
            )
        )

    def _watch(
        self, call_id: str, engine: Engine, payload: UndoPayload, *, check: bool = True
    ) -> None:
        """Hand the executed write to the engine's write status (plan 06b step 7): it is watched
        until the index shows every target in its post state, checked against the index right
        away (TB's events may already have been applied). The write and its undo token stand
        whatever happens here, so an index or audit read/write failure (``sqlite3.Error``) is
        logged at ERROR and the call still returns its token. A failure in the check itself
        leaves the write in the pending set (the observer settles it); one before its
        conditions are known (the folder list) leaves it to the next ternd start's
        :meth:`~tern.perms.write_status.WriteStatus.load`. Any other exception is a tern bug
        and propagates. ``check=False``: no check on register (an undo of a write still
        pending, :meth:`_undo_claimed`)."""
        try:
            engine.write_status.register(call_id, payload, check=check)
        except sqlite3.Error as exc:
            failure: BaseException = exc
        else:
            return
        watched = call_id in engine.write_status.pending()
        log.error(
            "write status check failed: the write stays watched"
            if watched
            else "write status check failed: not watched until restart",
            extra={"call_id": call_id, "error": _exc_name(failure)},
        )


#: The live targets of an ask approval: none (an R0/R1 call resolves no targets; a W1 call
#: resolves its own after the decision).
_NO_LIVE_TARGETS = LiveTargets((), {}, {})

#: The ToolError code of each non-approved approval outcome that is raised as one.
_OUTCOME_CODES: Mapping[Outcome, ToolErrorCode] = {
    "user_denied": "denied",
    "binding_mismatch": "denied",
    "expired": "expired",
    "cancelled": "internal",
    "shutdown": "denied",
    "notify_unavailable": "unavailable",
    "notifier_failed": "internal",
}

_OUTCOME_REASONS: Mapping[Outcome, str] = {
    **{o: reason for o, (_, reason) in OUTCOME_ROWS.items()},
    "notify_unavailable": "notify_failed",
    "notifier_failed": "notifier_failed",
}

_OUTCOME_MESSAGES: Mapping[Outcome, str] = {
    "user_denied": "the user denied this call",
    "binding_mismatch": "the approval decision did not match this call (nonce or subset)",
    "expired": "the approval expired before a decision",
    "cancelled": "call cancelled",
    "shutdown": "ternd is shutting down",
    "notify_unavailable": "the approval UI could not be reached",
    "notifier_failed": "the approval UI failed",
}


class _Call:
    """One :meth:`Registry.call` in progress: what its audit row and log line record."""

    def __init__(self, rt: ToolRuntime, caller: str, caller_label: str | None) -> None:
        self.rt = rt
        self.caller = caller
        self.caller_label = caller_label
        self.started = time.monotonic()
        self.tool = INVALID_TOOL
        #: False until the name resolves to a registered tool: an unknown name is model-chosen
        #: text, kept in the audit row only and never logged.
        self.known = False
        self.tier: Tier | None = None
        self.target_mids: list[str] = []
        self.target_count = 0
        #: Validated provenance mids (the approval card's, plan 06b step 5).
        self.provenance: tuple[str, ...] = ()
        #: The recipient checks (the approval card's recipients, plan 06b step 5).
        self.recipients: RecipientChecks = RecipientChecks()
        #: True when the recipient provider was unavailable (every recipient assumed unknown):
        #: on the call's log line, so the engine's WARNING matches this call's ``call_id``.
        self.recipients_unavailable = False
        self.decision: Decision | None = None
        self.call_id: str | None = None

    def append(
        self,
        *,
        decision: Decision,
        reason: str | None,
        args: object,
        validated: bool,
        initial: InitialStatus | None = None,
        original_args_sha256: str | None = None,
    ) -> None:
        """The write-ahead ``call`` row. A failed append stops the call: nothing executes."""
        failure = self._append_row(
            decision=decision,
            reason=reason,
            args=args,
            validated=validated,
            initial=initial,
            original_args_sha256=original_args_sha256,
        )
        if failure is not None:
            raise ToolError(
                "internal", "audit log write failed; nothing was run", reason="audit_failed"
            ) from failure

    def append_quiet(
        self,
        *,
        decision: Decision,
        reason: str,
        args: object,
        code: str,
        original_args_sha256: str | None = None,
    ) -> BaseException | None:
        """A ``call`` row for validated args that never raises: returns the audit failure
        (already logged at ERROR with the call's log line, ``code``) or None. The approvals queue
        writes an outcome's row through it (plan 06b step 5)."""
        return self._append_row(
            decision=decision,
            reason=reason,
            args=args,
            validated=True,
            code=code,
            original_args_sha256=original_args_sha256,
        )

    def append_cancelled(self, *, args: object) -> None:
        """The ``denied``/``cancelled`` row of a call cancelled before its row existed (an
        awaited engine step). Never raises: the cancellation propagates unchanged, and a failed
        append was already logged at ERROR (with the call's log line) by :meth:`_append_row`."""
        failure = self._append_row(
            decision="denied", reason="cancelled", args=args, validated=True, code="cancelled"
        )
        if failure is None:
            self.log_done("cancelled")

    def _append_row(
        self,
        *,
        decision: Decision,
        reason: str | None,
        args: object,
        validated: bool,
        initial: InitialStatus | None = None,
        code: str = "internal",
        original_args_sha256: str | None = None,
    ) -> BaseException | None:
        """Append the ``call`` row. Returns the audit failure (logged at ERROR, the call's log
        line written with ``code``) or None."""
        self.decision = decision
        try:
            self.call_id = self.rt.audit.append_call(
                caller=self.caller,
                caller_label=self.caller_label,
                tool=self.tool,
                tier=self.tier,
                decision=decision,
                reason=reason,
                args=args,
                args_validated=validated,
                target_mids=self.target_mids,
                target_count=self.target_count,
                initial_status=initial,
                original_args_sha256=original_args_sha256,
            )
        except (sqlite3.Error, ValueError, AuditError) as exc:
            self._audit_failed("audit write failed: call not run", exc, code=code)
            return exc
        return None

    def error_event(self, err: ToolError, exc_name: str) -> None:
        """The ``error`` event of an executed call that failed; a failed append raises."""
        failure = self.record_error(err, exc_name)
        if failure is not None:
            raise ToolError(
                "internal", "tool failed and its audit event write failed", reason="audit_failed"
            ) from failure

    def inverse_failed(
        self, err: ToolError, exc_name: str, applied: int, *, code: str = "internal"
    ) -> BaseException | None:
        """The ``error`` event of an undo whose inverse failed after ``applied`` bridge requests
        (plan 06b step 8). Returns the audit failure (already logged at ERROR, the call's log
        line written with ``code``) or None."""
        assert self.call_id is not None
        detail: dict[str, object] = {"code": err.code, "exc": exc_name, "applied": applied}
        if err.reason is not None:
            detail["reason"] = err.reason
        try:
            self.rt.audit.append_event(self.call_id, "error", detail)
        except (sqlite3.Error, ValueError, AuditError) as exc:
            self._audit_failed("audit error event not written", exc, code=code)
            return exc
        return None

    def targets_changed_event(self, reason: str) -> None:
        """The ``targets_changed`` event of an approved call the execution re-check refused (plan
        06b step 6); a failed append raises ``internal``/``audit_failed``."""
        assert self.call_id is not None
        failure: BaseException | None = None
        try:
            self.rt.audit.append_event(
                self.call_id, "targets_changed", {"code": "targets_changed", "reason": reason}
            )
        except (sqlite3.Error, ValueError, AuditError) as exc:
            failure = exc
        if failure is not None:
            self._audit_failed("audit targets_changed event not written", failure)
            raise ToolError(
                "internal", "audit log write failed; nothing was run", reason="audit_failed"
            ) from failure

    def record_error(
        self, err: ToolError, exc_name: str, *, code: str = "internal"
    ) -> BaseException | None:
        """Append the ``error`` event. Returns the audit failure (already logged at ERROR, the
        call's log line written with ``code``) or None; the cancel/abort path passes its own
        ``code`` and re-raises its own exception instead."""
        assert self.call_id is not None
        detail: dict[str, object] = {"code": err.code, "exc": exc_name}
        if err.reason is not None:
            detail["reason"] = err.reason
        try:
            self.rt.audit.append_event(self.call_id, "error", detail)
        except (sqlite3.Error, ValueError, AuditError) as exc:
            self._audit_failed("audit error event not written", exc, code=code)
            return exc
        return None

    def _audit_failed(self, msg: str, exc: BaseException, *, code: str = "internal") -> None:
        self.log_done(code)
        extra = {"tool": self.log_tool, "caller": self.caller, "error": _exc_name(exc)}
        log.error(msg, extra=extra)

    def fail(self, err: ToolError) -> ToolError:
        """Log the call's end with ``err``'s code and return ``err`` for raising."""
        self.log_done(err.code)
        return err

    @property
    def log_tool(self) -> str:
        """The tool name for log lines: a registered name, else ``UNKNOWN_LOG_TOOL``."""
        return self.tool if self.known else UNKNOWN_LOG_TOOL

    def log_done(self, code: str | None) -> None:
        """One log line per call: tool, tier, caller, decision, counts, duration; never args."""
        level = logging.WARNING if code in ("internal", "cancelled", "aborted") else logging.INFO
        log.log(
            level,
            "tool call",
            extra={
                "tool": self.log_tool,
                "tier": None if self.tier is None else self.tier.value,
                "caller": self.caller,
                "decision": self.decision,
                "code": code,
                "call_id": self.call_id,
                "targets": self.target_count,
                "recipients_unavailable": self.recipients_unavailable,
                "duration_ms": round((time.monotonic() - self.started) * 1000, 1),
            },
        )


def describe_registry() -> Registry:
    """The registry ``tern tools describe`` prints: built-ins and entry-point plugins only. Never
    resolves ``tern.paths``, reads ``config.toml`` or loads config-path plugins."""
    return Registry(builtin_tools() + entry_point_tools())
