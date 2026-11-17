"""The daemon's chat glue (plan 08b step 1): bridge ``chat.send`` / ``chat.cancel`` over the agent.

:class:`ChatHost` keeps up to :data:`MAX_SESSIONS` :class:`~tern.agent.loop.AgentSession`\\ s in
memory, keyed by ``session_key = sha256(session_id)[:32]`` (#35 note; the ``session_id`` itself is
never stored or logged). Its caller id is therefore ``agent:<session_key>``: every model tool call
goes through ``AgentSession`` → ``Registry.call`` (08a); this module never calls a tool.

``chat.send {session_id, text}`` starts a turn and answers ``{turn_id}`` at once. A pump task then
forwards each answer delta as ``chat.delta {turn_id, text}`` to the requesting peer
(``Peer.notify``, so a ``cli`` peer works as well as the extension), and ends the turn with
``chat.done {turn_id, citations}`` (outcome ``ok``) or ``chat.error {turn_id, reason=<outcome>}``
(#35). The response is written before the pump first runs: the handler starts the tasks without
awaiting anything, and the bridge writes the reply before its first await (P3).

Refusals (bridge errors; no turn starts): a role outside :data:`CHAT_ROLES` → ``role_rejected``
(bridge 1.0 puts no ``roles`` on ``chat.*``, so this module enforces it, like ``audit.undo``);
``allow_remote_llm`` → ``unavailable`` unless the peer is an extension that negotiated minor ≥ 1
and read ``daemon.info`` on this connection (its chat tab then shows #9's banner; a ``cli`` peer
has no banner and stays refused; A2, P10, decisions #37); ``text`` over :data:`MAX_CHAT_TEXT`
characters → ``invalid_params``; a turn already running in that session → ``invalid_params``;
every one of
:data:`MAX_SESSIONS` sessions busy → ``unavailable`` (else the least-recently-used idle session
is evicted, P4); after :meth:`ChatHost.shutdown` → ``unavailable``.

``chat.cancel {turn_id}``: the owning peer's running turn is cancelled (``TurnHandle.cancel``; it
ends ``chat.error reason=cancelled`` and no delta follows the reply) → ``{}``. A turn that already
ended (the last :data:`MAX_ENDED_TURNS` are remembered) → ``{}``; an unknown id or another peer's
turn → ``invalid_params`` (P5).

Permission modes (``tern-bridge/1.2``, decisions #39): ``chat.start {session_id, text, mode}`` is
``chat.send`` with a mode, refused exactly like it. ``bypass`` (and every ``chat.send``, which
carries no mode: an older client keeps today's behaviour) runs tool calls as the tier rules say.
``manual`` starts the turn with a per-turn asker (:class:`_TurnAsk`, an
:class:`~tern.perms.approvals.ApprovalNotifier`) that ``AgentSession`` passes to every
``Registry.call`` of the turn: the engine's approvals queue opens an ask approval (audited by the
registry like a W2/W3 one) and the asker sends ``chat.approval {turn_id, approval_id, nonce, tool,
tier, args, expires_at}`` to the owning peer. ``chat.approve {turn_id, approval_id, nonce,
decision, rest_of_turn}`` from that peer, for an approval of that turn still open, is decided as
caller ``ui`` (``Approvals.decide``) → ``{status}``; anything else (another peer's or an ended
turn, an approval of another turn or already resolved) → ``invalid_params``. ``rest_of_turn``
approves this call and makes the asker approve the rest of the turn's calls itself (each still
gets its approval and ``approved`` row; no card is sent). Args whose JSON is longer than
:data:`MAX_APPROVAL_ARGS_CHARS` are not shown: the card cannot be sent, so the call is refused
(``unavailable``, fail closed). A turn whose user never answers ends through the turn's own paths
(Stop, the turn timeout, the peer going away, shutdown: ``denied``/``cancelled`` rows) or the
approval's expiry (``timeout``/``expired``, the model is told and the turn goes on).

The owning peer going away cancels its running turns: when its connection closes (P11), and when
a ``chat.delta`` to it fails (``BridgeError``, whether or not ``closed`` is set yet). An exception
out of ``TurnHandle.result()`` (a tern bug) is logged at ERROR by type and sent as
``chat.error reason=internal``. Ended turns remember their peer by weak reference only.

Model picker (``tern-bridge/1.3``): ``chat.models {}`` → ``{models, selected}``, the chat models
loaded on the configured servers (``discover``, :func:`tern.llm.models.discover` in ternd) and the
key of the model new turns use. ``chat.select_model {key}`` selects one of them (a key not among
the loaded models → ``invalid_params``) and answers like ``chat.models``. The selection is ternd
wide and lasts until ternd restarts; each turn takes the model selected when it starts (a running
turn keeps its model). Selecting ``chat_base_url``/``chat_model`` goes back to the default client;
any other model gets its own ``LLMClient`` (``make_llm``), kept until :meth:`ChatHost.shutdown`.

Logging: turn ids, session keys, outcome codes and counts; never the user's text, the model's
text or the ``session_id``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from tern.agent.loop import AgentSession, Hold, TurnHandle
from tern.bridge.client import Peer
from tern.bridge.protocol import (
    ApprovalDecided,
    BridgeError,
    ChatApproval,
    ChatApproveParams,
    ChatDelta,
    ChatDone,
    ChatErrorNotif,
    ChatMode,
    ChatModel,
    ChatModels,
    ChatSelectModelParams,
    ChatSendParams,
    ChatStartParams,
    Empty,
    Role,
    TurnId,
)
from tern.config import TernConfig
from tern.index.store import Store
from tern.llm.client import LLMClient
from tern.llm.models import ModelChoice, choice_key
from tern.perms.approvals import ApprovalCard, Approvals, ApprovalStatus, DecideRefused
from tern.perms.audit import canonical_json
from tern.tools.registry import Registry

log = logging.getLogger(__name__)

#: The bridge roles that may chat: the extension's tab and ``tern`` CLI clients (P8).
CHAT_ROLES: frozenset[Role] = frozenset({"extension", "cli"})

#: The longest user message, in characters (P4; the context budget is for retrieved text).
MAX_CHAT_TEXT = 8000

#: Sessions kept in memory; the least-recently-used idle one is evicted beyond this (P1, P4).
MAX_SESSIONS = 8

#: Ended turn ids remembered for an idempotent ``chat.cancel`` (P5).
MAX_ENDED_TURNS = 256

#: ``chat.error.reason`` when the turn's task raised (a tern bug, not an agent outcome).
INTERNAL_REASON = "internal"

#: The longest tool-call arguments (canonical JSON, characters) a ``chat.approval`` carries; a
#: longer one is refused unseen (``unavailable``), never shown cut (decisions #39). Far below the
#: 512 KiB host→extension frame limit.
MAX_APPROVAL_ARGS_CHARS = 100_000

#: The caller every ``chat.approve`` decides as: the chat tab is the approval UI of its own turns.
DECIDING_CALLER = "ui"


def session_key(session_id: str) -> str:
    """The agent session key of a bridge ``session_id``: ``sha256(session_id)[:32]`` (#35)."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


class _TurnAsk:
    """The asker of one ``manual`` turn (module docstring): shows each ask approval's card to the
    turn's peer as ``chat.approval``, or, after ``rest_of_turn``, approves it itself (as ``ui``,
    with the card's own nonce)."""

    def __init__(self, approvals: Approvals, peer: Peer) -> None:
        self._approvals = approvals
        self._peer = peer
        #: Set once ``start_turn`` named the turn (before the turn's task first runs).
        self.turn_id = ""
        #: The approvals of this turn whose card was sent and that are not resolved yet.
        self.open: set[str] = set()
        self.rest_of_turn = False

    async def requested(self, card: ApprovalCard) -> None:
        if self.rest_of_turn:
            status = self._approvals.decide(
                card.approval_id, card.nonce, "approve", caller=DECIDING_CALLER
            )
            log.info(
                "chat approval approved for the rest of the turn",
                extra={"turn_id": self.turn_id, "approval_id": card.approval_id, "status": status},
            )
            return
        args_chars = len(canonical_json(card.args_view))
        if args_chars > MAX_APPROVAL_ARGS_CHARS:
            log.warning(
                "chat approval not sent: arguments too long",
                extra={"turn_id": self.turn_id, "approval_id": card.approval_id},
            )
            raise BridgeError("too_large", "the tool arguments are too long to show")
        self.open.add(card.approval_id)
        note = ChatApproval(
            turn_id=self.turn_id,
            approval_id=card.approval_id,
            nonce=card.nonce,
            tool=card.tool,
            tier=card.tier.value,
            args=card.args_view,
            expires_at=datetime.fromtimestamp(card.expires_at, tz=UTC),
        )
        log.info(
            "chat approval requested",
            extra={
                "turn_id": self.turn_id,
                "approval_id": card.approval_id,
                "tool": card.tool,
                "args_chars": args_chars,
            },
        )
        await self._peer.notify("chat.approval", note)

    def resolved(self, approval_id: str, status: ApprovalStatus) -> None:
        self.open.discard(approval_id)


@dataclass
class _Turn:
    handle: TurnHandle
    peer: Peer
    key: str
    pump: asyncio.Task[None] | None = None
    #: The asker of a ``manual`` turn, else None.
    ask: _TurnAsk | None = None


class ChatHost:
    """``Daemon.chat``: the agent sessions and running turns of one ternd (module docstring)."""

    def __init__(
        self,
        registry: Registry,
        llm: LLMClient,
        index: Store,
        cfg: TernConfig,
        *,
        hold: Hold | None = None,
        approvals: Approvals | None = None,
        discover: Callable[[], Awaitable[list[ModelChoice]]] | None = None,
        make_llm: Callable[[str, str], LLMClient] | None = None,
    ) -> None:
        self._registry = registry
        #: The engine's approvals queue (``manual`` mode asks through it), or None: then a
        #: ``manual`` turn is refused (``unavailable``).
        self._approvals = approvals
        self._llm = llm
        self._index = index
        self._cfg = cfg
        self._hold = hold
        #: session_key -> session, least recently used first.
        self._sessions: OrderedDict[str, AgentSession] = OrderedDict()
        #: session_key -> turn_id of the turn running in it.
        self._busy: dict[str, str] = {}
        #: turn_id -> running turn.
        self._turns: dict[str, _Turn] = {}
        #: turn_id -> owning peer (a weak reference: a closed connection is not kept alive), for
        #: turns that ended (oldest first).
        self._ended: OrderedDict[str, weakref.ref[Peer]] = OrderedDict()
        #: Pump tasks not finished yet (a pump outlives its turn by its last notification).
        self._pumps: set[asyncio.Task[None]] = set()
        self._shut_down = False
        #: Connections that read ``daemon.info`` (weakly held; plan 07 step 8 review).
        self._informed: weakref.WeakSet[Peer] = weakref.WeakSet()
        #: Lists the loaded chat models (bridge 1.3); None: the picker lists none.
        self._discover = discover
        #: Builds a client for a picked (base_url, model); None: only the default can be picked.
        self._make_llm = make_llm
        #: The default model's (base_url, model): ``chat_base_url``/``chat_model``.
        self._default = (cfg.chat_base_url, cfg.chat_model)
        #: The picked (base_url, model); the default until ``chat.select_model``.
        self._selected = self._default
        #: Clients of picked models other than the default, by (base_url, model).
        self._clients: dict[tuple[str, str], LLMClient] = {}

    @property
    def running(self) -> int:
        """How many turns are running."""
        return len(self._turns)

    @property
    def sessions(self) -> int:
        """How many sessions are held in memory."""
        return len(self._sessions)

    @property
    def chat_model(self) -> str:
        """The id of the model new turns use (``daemon.info``'s ``chat_model``)."""
        return self._selected[1]

    def _current_llm(self) -> LLMClient:
        if self._selected == self._default:
            return self._llm
        return self._clients[self._selected]

    def info_read(self, peer: Peer) -> None:
        """``peer`` read ``daemon.info`` (``remote_llm``) on this connection."""
        self._informed.add(peer)

    def _shows_banner(self, peer: Peer) -> bool:
        """Whether ``peer``'s chat can show #9's remote-model banner: an extension at bridge minor
        >= 1 that has read ``daemon.info`` on this connection (its tab gates sending on it)."""
        return peer.role == "extension" and peer.minor >= 1 and peer in self._informed

    # -- bridge handlers ------------------------------------------------------------------------

    async def send(self, peer: Peer, params: ChatSendParams) -> TurnId:
        """The ``chat.send`` handler: a turn with no mode (today's behaviour, module docstring)."""
        return self._start(peer, "chat.send", params.session_id, params.text, None)

    async def start(self, peer: Peer, params: ChatStartParams) -> TurnId:
        """The ``chat.start`` handler (bridge 1.2): ``chat.send`` with a permission mode."""
        return self._start(peer, "chat.start", params.session_id, params.text, params.mode)

    async def models(self, peer: Peer, params: Empty) -> ChatModels:
        """The ``chat.models`` handler (bridge 1.3, module docstring)."""
        self._check_role(peer, "chat.models")
        return self._listing(await self._loaded())

    async def select_model(self, peer: Peer, params: ChatSelectModelParams) -> ChatModels:
        """The ``chat.select_model`` handler (bridge 1.3, module docstring)."""
        self._check_role(peer, "chat.select_model")
        if self._shut_down:
            raise BridgeError("unavailable", "ternd is shutting down")
        loaded = await self._loaded()
        choice = next((m for m in loaded if m.key == params.key), None)
        if choice is None:
            log.info("chat.select_model refused: not a loaded model")
            raise BridgeError("invalid_params", "no loaded chat model has this key")
        pick = (choice.base_url, choice.model)
        if pick != self._default and pick not in self._clients:
            if self._make_llm is None:
                raise BridgeError("unavailable", "this ternd cannot switch chat models")
            self._clients[pick] = self._make_llm(*pick)
        self._selected = pick
        log.info("chat model selected", extra={"server": choice.server, "model": choice.model})
        return self._listing(loaded)

    async def _loaded(self) -> list[ModelChoice]:
        return [] if self._discover is None else await self._discover()

    def _listing(self, loaded: list[ModelChoice]) -> ChatModels:
        key = choice_key(*self._selected)
        return ChatModels(
            models=[
                ChatModel(key=m.key, model=m.model, server=m.server, label=m.label) for m in loaded
            ],
            selected=key if any(m.key == key for m in loaded) else None,
        )

    async def approve(self, peer: Peer, params: ChatApproveParams) -> ApprovalDecided:
        """The ``chat.approve`` handler (bridge 1.2, module docstring)."""
        self._check_role(peer, "chat.approve")
        turn = self._turns.get(params.turn_id)
        ask = None if turn is None or turn.peer is not peer else turn.ask
        if ask is None or params.approval_id not in ask.open or self._approvals is None:
            log.info(
                "chat.approve refused: no such open approval",
                extra={"turn_id": params.turn_id, "approval_id": params.approval_id},
            )
            raise BridgeError("invalid_params", "no open approval of a turn on this connection")
        try:
            status = self._approvals.decide(
                params.approval_id, params.nonce, params.decision, caller=DECIDING_CALLER
            )
        except DecideRefused as exc:
            log.info(
                "chat.approve refused",
                extra={"approval_id": params.approval_id, "reason": exc.reason},
            )
            raise BridgeError("invalid_params", exc.message) from None
        if params.rest_of_turn and status == "approved":
            ask.rest_of_turn = True
        log.info(
            "chat approval decided",
            extra={
                "turn_id": params.turn_id,
                "approval_id": params.approval_id,
                "status": status,
                "rest_of_turn": params.rest_of_turn,
            },
        )
        return ApprovalDecided(status=status)

    def _start(
        self, peer: Peer, method: str, session_id: str, text: str, mode: ChatMode | None
    ) -> TurnId:
        """Start a turn (``chat.send``/``chat.start``). Nothing in it awaits, so the reply is
        written before the pump task first runs (P3)."""
        self._check_role(peer, method)
        if self._cfg.allow_remote_llm and not self._shows_banner(peer):
            # A 1.1 extension that read daemon.info on this connection shows #9's banner; a 1.0
            # peer cannot, and no `tern` command shows one (cli stays refused, fail closed).
            log.info("chat.send refused: remote LLM allowed; this peer shows no banner")
            raise BridgeError(
                "unavailable",
                "chat is off while allow_remote_llm is set: this connection cannot show the "
                "remote-model banner",
            )
        if self._shut_down:
            raise BridgeError("unavailable", "ternd is shutting down")
        if len(text) > MAX_CHAT_TEXT:
            log.info("chat.send refused: text too long", extra={"chars": len(text)})
            raise BridgeError("invalid_params", f"text is longer than {MAX_CHAT_TEXT} characters")
        ask: _TurnAsk | None = None
        if mode == "manual":
            if self._approvals is None:
                log.warning("chat.start refused: manual mode without an approvals queue")
                raise BridgeError("unavailable", "manual mode is not available in this ternd")
            ask = _TurnAsk(self._approvals, peer)
        key = session_key(session_id)
        if key in self._busy:
            log.info("chat.send refused: session busy", extra={"session_key": key})
            raise BridgeError("invalid_params", "a turn is already running in this session")
        session = self._session(key)
        session.use_llm(self._current_llm())
        handle = session.start_turn(text, ask=ask)
        if ask is not None:
            ask.turn_id = handle.turn_id
        turn = _Turn(handle, peer, key, ask=ask)
        self._busy[key] = handle.turn_id
        self._turns[handle.turn_id] = turn
        turn.pump = asyncio.get_running_loop().create_task(
            self._pump(turn), name=f"chat-pump-{handle.turn_id}"
        )
        self._pumps.add(turn.pump)
        turn.pump.add_done_callback(self._pumps.discard)
        turn.pump.add_done_callback(_pump_ended)
        log.info(
            "chat turn started",
            extra={
                "turn_id": handle.turn_id,
                "session_key": key,
                "chars": len(text),
                "mode": "none" if mode is None else mode,
            },
        )
        return TurnId(turn_id=handle.turn_id)

    async def cancel(self, peer: Peer, params: TurnId) -> Empty:
        """The ``chat.cancel`` handler (P5)."""
        self._check_role(peer, "chat.cancel")
        turn = self._turns.get(params.turn_id)
        if turn is not None and turn.peer is peer:
            log.info("chat turn cancel requested", extra={"turn_id": params.turn_id})
            turn.handle.cancel()
            return Empty()
        owner = self._ended.get(params.turn_id) if turn is None else None
        if owner is not None and owner() is peer:
            return Empty()
        log.info("chat.cancel refused: unknown turn", extra={"turn_id": params.turn_id})
        raise BridgeError("invalid_params", "no such turn on this connection")

    # -- shutdown -------------------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Refuse new turns, cancel every running one and wait until each has ended, so no
        ``Registry.call`` runs after this returns (ternd closes the audit log afterwards).

        Every pump is awaited before a pump's own exception (a tern bug) is re-raised, so one
        failed pump never leaves another turn running. A tool that ignores cancellation blocks
        this, by design: returning early would let it call ``Registry.call`` after the audit log
        closed."""
        self._shut_down = True
        turns = list(self._turns.values())
        for turn in turns:
            turn.handle.cancel()
        if turns:
            log.info("chat turns cancelled at shutdown", extra={"n": len(turns)})
        pumps = list(self._pumps)
        if not pumps:
            return
        await asyncio.wait(pumps)
        for pump in pumps:
            if not pump.cancelled() and (exc := pump.exception()) is not None:
                raise exc

    async def aclose(self) -> None:
        """Close the clients of picked models (the default client is ternd's to close)."""
        clients, self._clients = list(self._clients.values()), {}
        self._selected = self._default
        for client in clients:
            await client.aclose()

    # -- internals ------------------------------------------------------------------------------

    def _check_role(self, peer: Peer, method: str) -> None:
        if peer.role not in CHAT_ROLES:
            log.warning("chat refused for role", extra={"method": method, "role": peer.role})
            raise BridgeError("role_rejected", f"{method} is not allowed for role {peer.role}")

    def _session(self, key: str) -> AgentSession:
        """The session for ``key``, created (evicting the least-recently-used idle session when
        full) if absent, and marked most recently used."""
        session = self._sessions.get(key)
        if session is not None:
            self._sessions.move_to_end(key)
            return session
        if len(self._sessions) >= MAX_SESSIONS:
            idle = next((k for k in self._sessions if k not in self._busy), None)
            if idle is None:
                log.info("chat.send refused: every session busy", extra={"n": len(self._sessions)})
                raise BridgeError("unavailable", f"all {MAX_SESSIONS} chat sessions are busy")
            del self._sessions[idle]
            log.info("chat session evicted", extra={"session_key": idle})
        session = AgentSession(
            key,
            self._registry,
            self._llm,
            self._index,
            self._cfg,
            hold=self._hold,
            today=datetime.now().astimezone().date(),  # local date, for "today"/"yesterday"
        )
        self._sessions[key] = session
        return session

    async def _pump(self, turn: _Turn) -> None:
        handle, peer, turn_id = turn.handle, turn.peer, turn.handle.turn_id
        watch = asyncio.get_running_loop().create_task(peer.closed.wait())
        watch.add_done_callback(lambda _w: self._peer_gone(turn))
        deltas = 0
        try:
            async for text in handle:
                try:
                    await peer.notify("chat.delta", ChatDelta(turn_id=turn_id, text=text))
                except BridgeError:
                    # The peer cannot take the delta (its connection is going away): no one is
                    # left to read the turn, so stop it, whether or not ``closed`` is set yet.
                    log.info("chat delta not delivered; turn cancelled", extra={"turn_id": turn_id})
                    handle.cancel()
                    break
                deltas += 1
            notif: ChatDone | ChatErrorNotif
            try:
                result = await handle.result()
            except Exception as exc:
                # A tern bug in the turn's task: by type only (its message may quote content).
                log.error(
                    "chat turn failed", extra={"turn_id": turn_id, "error": type(exc).__name__}
                )
                log.debug("chat turn traceback", exc_info=True, extra={"turn_id": turn_id})
                outcome = INTERNAL_REASON
                notif = ChatErrorNotif(turn_id=turn_id, reason=INTERNAL_REASON)
            else:
                outcome = result.outcome
                notif = (
                    ChatDone(turn_id=turn_id, citations=list(result.citations))
                    if outcome == "ok"
                    else ChatErrorNotif(turn_id=turn_id, reason=outcome)
                )
            # The session is free before the last notification goes out, so a client that sends
            # its next message as soon as it reads chat.done is never told the session is busy.
            self._end(turn)
            log.info(
                "chat turn ended",
                extra={"turn_id": turn_id, "outcome": outcome, "deltas": deltas},
            )
            method = "chat.done" if isinstance(notif, ChatDone) else "chat.error"
            try:
                await peer.notify(method, notif)
            except BridgeError:
                log.info("chat turn end not delivered: peer gone", extra={"turn_id": turn_id})
        finally:
            watch.cancel()
            self._end(turn)

    def _peer_gone(self, turn: _Turn) -> None:
        """The owning peer's connection closed: cancel its turn (a no-op once it ended)."""
        if not turn.peer.closed.is_set() or turn.handle.done or turn.handle.cancel_requested:
            return
        log.info("chat peer gone; turn cancelled", extra={"turn_id": turn.handle.turn_id})
        turn.handle.cancel()

    def _end(self, turn: _Turn) -> None:
        """Forget a running turn (idempotent): free its session, remember its id as ended."""
        turn_id = turn.handle.turn_id
        if self._turns.pop(turn_id, None) is None:
            return
        if self._busy.get(turn.key) == turn_id:
            del self._busy[turn.key]
        self._ended[turn_id] = weakref.ref(turn.peer)
        while len(self._ended) > MAX_ENDED_TURNS:
            self._ended.popitem(last=False)


def _pump_ended(pump: asyncio.Task[None]) -> None:
    """A pump task ended. Its own errors are bugs: logged at ERROR by type (never swallowed:
    :meth:`ChatHost.shutdown` re-raises one it awaits)."""
    if pump.cancelled():
        return
    exc = pump.exception()
    if exc is not None:
        log.error("chat pump failed", extra={"error": type(exc).__name__})
