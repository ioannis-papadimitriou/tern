"""tern configuration: ``<config dir>/config.toml`` (located by ``tern.paths``), validated.

A missing file means all defaults. A present file with an unknown key, a bad value or broken
TOML is an error: config mistakes fail loudly. Environment variables are not a config source
(``TERN_HOME`` only selects where the file is).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from tern import paths
from tern._model import BoundaryModel

DEFAULT_THUNDERBIRD_BIN = Path("/opt/thunderbird-153/thunderbird-bin")


def default_lmstudio_cli() -> Path:
    """LM Studio's `lms` CLI in its AppImage install (CLAUDE.md Gotchas), under the home dir."""
    return Path.home().joinpath(".cache", "lm-studio", "bin", "lms")


class ConfigError(RuntimeError):
    """``config.toml`` exists but cannot be used."""


class PluginsConfig(BoundaryModel):
    """``[plugins]`` (#6, plan 06a step 2): user tool plugins, listed one file at a time."""

    #: Absolute paths to ``.py`` files, each exporting ``TOOLS: list[ToolSpec]``. Nothing is
    #: loaded from a directory and ``~`` is not expanded. Loaded by ``tern.tools.registry`` at
    #: daemon start (never by ``tern tools describe``).
    paths: list[Path] = Field(default_factory=list)

    @field_validator("paths")
    @classmethod
    def _absolute_py_files(cls, value: list[Path]) -> list[Path]:
        for i, path in enumerate(value):
            if not path.is_absolute():
                raise ValueError(f"plugins.paths[{i}] must be an absolute path (no ~ expansion)")
            if path.suffix != ".py":
                raise ValueError(f"plugins.paths[{i}] must name a .py file")
        return value


class TernConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="forbid", hide_input_in_errors=True)

    # The TB main-process binary. The bridge peer check requires the parent of a
    # `role=extension` peer to be this executable (plan 02 step 3, D6).
    thunderbird_bin: Path = DEFAULT_THUNDERBIRD_BIN

    # Ingest (plan 04 step 7, #16, #30). Throttle off and one fetch in flight by default: S1 saw
    # no stall, and a low cap alone would trip #16's 20 msg/s floor.
    #: Raw/full fetches per second across all ingest jobs (backfill, events and reconcile share
    #: one bucket); 0 = no throttle. ``find`` and refill fetches are not throttled.
    ingest_max_msgs_per_s: float = Field(default=0, ge=0)
    #: Raw/full fetches in flight at once (one folder at a time).
    ingest_concurrency: int = Field(default=1, ge=1, le=8)
    #: Backfill window, newest first (M8).
    backfill_window_days: int = Field(default=30, ge=1)
    #: Hourly reconcile period (plan 04 step 8/9).
    reconcile_interval_s: int = Field(default=3600, ge=60)
    #: Daily reconcile period (plan 04 step 8/9).
    reconcile_daily_s: int = Field(default=86400, ge=60)

    # Embeddings (plan 05 step 2, #15, #17). Any OpenAI-compatible `/v1/embeddings` server; the
    # base URL must pass the loopback egress guard (`tern.llm.egress`, #9).
    #: Base URL of the embeddings server (`<base>/embeddings` is POSTed to).
    embed_base_url: str = "http://127.0.0.1:47831/v1"
    #: Embedding model id (the S3 one). None disables embedding; in TOML, which has no null,
    #: `embed_model = ""` means None.
    embed_model: str | None = None
    #: Chunks per `/v1/embeddings` request.
    embed_batch_size: int = Field(default=32, ge=1, le=256)
    #: Longer chunk texts are cut to this many characters before embedding (counted and shown).
    embed_max_input_chars: int = Field(default=6000, ge=1)
    #: Prepended to search queries only (model-specific instruction; empty in v1).
    embed_query_prefix: str = ""
    #: Per-request timeout of the embeddings client.
    embed_timeout_s: float = Field(default=60, gt=0)
    #: Embed-queue poll period while nothing is pending.
    embed_idle_poll_s: float = Field(default=10, gt=0)

    # Chat model (plan 08a step 1, #15). Any OpenAI-compatible `/v1/chat/completions` server
    # (`tern.llm.client`); the base URL must pass the loopback egress guard (#9). Defaults are
    # S3's measured settings (temperature 0, seed 0, 4096 max tokens, 120 s cap).
    #: Base URL of the chat server (`<base>/chat/completions` is POSTed to).
    chat_base_url: str = "http://127.0.0.1:47831/v1"
    #: Chat model id (the S3-adopted model, #15 amendment 2026-11-13).
    chat_model: str = "iq3_xxs"
    #: More chat servers whose *loaded* models the chat tab offers in its model picker, next to
    #: the models of ``chat_base_url`` (`tern.llm.models`). Default: LM Studio's default port.
    #: Each must pass the loopback egress guard (#9), else it is skipped. [] = only
    #: ``chat_base_url``.
    chat_extra_base_urls: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:1234/v1"])
    chat_temperature: float = Field(default=0.0, ge=0, le=2)
    chat_seed: int = Field(default=0, ge=0)
    #: `max_tokens` of each chat request (reasoning included); `finish_reason "length"` is
    #: reported as truncated, never hidden.
    chat_max_tokens: int = Field(default=4096, ge=1)
    #: Per-request timeout of the chat client (connect, and each read of the stream).
    chat_request_timeout_s: float = Field(default=120, gt=0)
    #: Runtime shim for runtime-specific calls (`tern.llm.lmstudio`); LM Studio only in v1.
    llm_runtime: Literal["lmstudio"] = "lmstudio"
    #: The `lms` CLI (runtime version and engine, for the eval and `tern doctor`; never chat).
    lmstudio_cli: Path = Field(default_factory=default_lmstudio_cli)

    # Agent loop (plan 08a step 2b; S3 limits, P3).
    #: Tool calls per chat turn; one more ends the turn with outcome `max_tool_calls`.
    #: 0 = no limit (plan 08c, the user's choice for large-context runtimes; Stop still works).
    agent_max_tool_calls: int = Field(default=0, ge=0)
    #: Built-in tools offered to the chat model (plugin tools are always offered). None = the
    #: default chat set (#38): the vendored thunderbird-mcp tools, `skill` and
    #: `open_in_thunderbird` (`tern.agent.loop.DEFAULT_CHAT_BUILTINS`).
    agent_tools: list[str] | None = None
    #: Wall-clock limit of one chat turn (outcome `timeout`).
    #: 0 = no limit (plan 08c). `chat_request_timeout_s` still bounds each model request.
    agent_turn_timeout_s: float = Field(default=0, ge=0)
    #: Estimated tokens (UTF-8 bytes / 4) of one turn's tool-output text (P4). Per turn since
    #: plan 08c (#38): earlier turns' tool outputs are dropped from the history, nothing already
    #: sent is rewritten, and only outputs the model has not seen are cut (`tern.agent.budget`).
    #: 16000 is about half of a 32768-token context.
    agent_context_budget_tokens: int = Field(default=200000, ge=256)

    # LLM egress (#9). A non-loopback model URL needs both the flag and an exact entry here.
    allow_remote_llm: bool = False
    remote_llm_urls: list[str] = Field(default_factory=list)

    # Thunderbird snapshot tools (vendored thunderbird-mcp, `tern.tbmcp`; decision of 2026-11-16).
    # They read private snapshot copies of TB's SQLite databases (kept under the state dir). The
    # `tb_*` guard keys map one to one onto thunderbird-mcp's guardrails (`tern.tbmcp.guard`,
    # upstream's THUNDERBIRD_MCP_<NAME> variables, which tern never reads).
    #: The TB profile directory (absolute); None = discover it from TB's `profiles.ini`.
    tb_profile_dir: Path | None = None
    #: Only these accounts (emails or server usernames); empty = all.
    tb_accounts: list[str] = Field(default_factory=list)
    #: Never these accounts.
    tb_exclude_accounts: list[str] = Field(default_factory=list)
    #: Only folders with these names; empty = all.
    tb_folders: list[str] = Field(default_factory=list)
    #: Never these folders; None = thunderbird-mcp's default (Junk, Spam, Trash, Bin, Deleted
    #: Items, Deleted Messages), [] = none.
    tb_exclude_folders: list[str] | None = None
    #: Only these calendars (names or ids); empty = all.
    tb_calendars: list[str] = Field(default_factory=list)
    #: Never these calendars.
    tb_exclude_calendars: list[str] = Field(default_factory=list)
    #: Hide mail and events older than this many days; None = no limit.
    tb_max_age_days: int | None = Field(default=None, ge=1)
    #: Private events: shown, shown as "Busy" without details, or hidden.
    tb_private_events: Literal["show", "busy", "hide"] = "show"
    #: Mask login codes, reset links, cards, IBANs, API keys and passwords in tool output.
    tb_redact: bool = True
    #: Cap on one tool result, in JSON characters (thunderbird-mcp's output budget).
    tb_max_output_chars: int = Field(default=100_000, ge=1000)
    #: The user's IANA timezone; None = TB's calendar timezone, else the system's.
    tb_timezone: str | None = None

    # Tool plugins (#6, plan 06a step 2): `[plugins] paths = ["/abs/plugin.py", ...]`.
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)

    @field_validator("embed_model")
    @classmethod
    def _empty_model_disables(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            if value:
                raise ValueError('embed_model is blank; use "" to disable embedding')
            return None
        return value

    @field_validator("chat_model")
    @classmethod
    def _chat_model_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chat_model is blank")
        return value

    @field_validator("lmstudio_cli")
    @classmethod
    def _cli_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("lmstudio_cli must be an absolute path (no ~ expansion)")
        return value

    @field_validator("tb_profile_dir")
    @classmethod
    def _profile_absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("tb_profile_dir must be an absolute path (no ~ expansion)")
        return value

    @field_validator(
        "tb_accounts",
        "tb_exclude_accounts",
        "tb_folders",
        "tb_exclude_folders",
        "tb_calendars",
        "tb_exclude_calendars",
    )
    @classmethod
    def _plain_names(cls, value: list[str] | None) -> list[str] | None:
        # thunderbird-mcp's guard takes comma-separated lists
        for i, item in enumerate(value or ()):
            if not item.strip() or "," in item:
                raise ValueError(f"item {i} is blank or contains a comma")
        return value

    @field_validator("tb_timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("tb_timezone is not a known IANA timezone") from None
        return value

    @model_validator(mode="after")
    def _remote_needs_flag_and_urls(self) -> TernConfig:
        if self.allow_remote_llm and not self.remote_llm_urls:
            raise ValueError("allow_remote_llm is set but remote_llm_urls is empty")
        if self.remote_llm_urls and not self.allow_remote_llm:
            raise ValueError("remote_llm_urls is set but allow_remote_llm is false")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def load(config_file: Path | None = None) -> TernConfig:
    """Load ``config_file`` (default: the resolved ``config.toml``); defaults if it is absent.
    Raises ConfigError when it cannot be read (a directory, no permission), is not UTF-8 or is
    not TOML; pydantic's ValidationError when a value is wrong."""
    path = paths.resolve().config_file if config_file is None else config_file
    if not path.exists():
        return TernConfig()
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not UTF-8 (invalid byte at offset {exc.start})") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc.strerror or type(exc).__name__}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    return TernConfig(**data)
