"""tern.config: defaults when absent, loud errors when wrong."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from tern import config


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "config.toml")
    assert cfg.thunderbird_bin == config.DEFAULT_THUNDERBIRD_BIN


def test_value_is_read(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('thunderbird_bin = "/usr/local/tb/thunderbird-bin"\n')
    assert config.load(f).thunderbird_bin == Path("/usr/local/tb/thunderbird-bin")


def test_unknown_key_is_an_error(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('thunderbird_binn = "/x"\n')
    with pytest.raises(ValidationError):
        config.load(f)


def test_broken_toml_is_an_error(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("thunderbird_bin = \n")
    with pytest.raises(config.ConfigError):
        config.load(f)


def test_non_utf8_config_is_a_config_error(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_bytes(b'thunderbird_bin = "/x\xff"\n')
    with pytest.raises(config.ConfigError, match="not UTF-8"):
        config.load(f)


def test_unreadable_config_is_a_config_error(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.mkdir()  # exists, but read_text raises IsADirectoryError
    with pytest.raises(config.ConfigError, match="cannot read"):
        config.load(f)


def test_default_location_follows_tern_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERN_HOME", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.toml").write_text('thunderbird_bin = "/y"\n')
    assert config.load().thunderbird_bin == Path("/y")


def test_ingest_defaults(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "config.toml")
    assert cfg.ingest_max_msgs_per_s == 0
    assert cfg.ingest_concurrency == 1
    assert cfg.backfill_window_days == 30
    assert cfg.reconcile_interval_s == 3600
    assert cfg.reconcile_daily_s == 86400


def test_ingest_values_are_read(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        "ingest_max_msgs_per_s = 12.5\ningest_concurrency = 8\nbackfill_window_days = 1\n"
        "reconcile_interval_s = 60\nreconcile_daily_s = 7200\n"
    )
    cfg = config.load(f)
    assert (cfg.ingest_max_msgs_per_s, cfg.ingest_concurrency, cfg.backfill_window_days) == (
        12.5,
        8,
        1,
    )
    assert (cfg.reconcile_interval_s, cfg.reconcile_daily_s) == (60, 7200)


@pytest.mark.parametrize(
    "line",
    [
        "ingest_max_msgs_per_s = -1",
        "ingest_concurrency = 0",
        "ingest_concurrency = 9",
        "backfill_window_days = 0",
        "reconcile_interval_s = 59",
        "reconcile_daily_s = 0",
    ],
)
def test_ingest_values_out_of_range_are_errors(tmp_path: Path, line: str) -> None:
    f = tmp_path / "config.toml"
    f.write_text(line + "\n")
    with pytest.raises(ValidationError):
        config.load(f)


def test_embed_and_egress_defaults(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "config.toml")
    assert cfg.embed_base_url == "http://127.0.0.1:47831/v1"
    assert cfg.embed_model is None
    assert cfg.embed_batch_size == 32
    assert cfg.embed_max_input_chars == 6000
    assert cfg.embed_query_prefix == ""
    assert cfg.embed_timeout_s == 60
    assert cfg.embed_idle_poll_s == 10
    assert cfg.allow_remote_llm is False
    assert cfg.remote_llm_urls == []


def test_embed_values_are_read(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        'embed_base_url = "http://localhost:8080/v1"\nembed_model = "some-embedder"\n'
        "embed_batch_size = 256\nembed_max_input_chars = 1\n"
        'embed_query_prefix = "Query: "\nembed_timeout_s = 2.5\nembed_idle_poll_s = 0.5\n'
    )
    cfg = config.load(f)
    assert (cfg.embed_base_url, cfg.embed_model) == ("http://localhost:8080/v1", "some-embedder")
    assert (cfg.embed_batch_size, cfg.embed_max_input_chars) == (256, 1)
    assert cfg.embed_query_prefix == "Query: "
    assert (cfg.embed_timeout_s, cfg.embed_idle_poll_s) == (2.5, 0.5)


def test_empty_embed_model_disables_embedding(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('embed_model = ""\n')
    assert config.load(f).embed_model is None
    assert config.TernConfig(embed_model=None).embed_model is None


@pytest.mark.parametrize(
    "line",
    [
        "embed_batch_size = 0",
        "embed_batch_size = 257",
        "embed_max_input_chars = 0",
        "embed_timeout_s = 0",
        "embed_idle_poll_s = 0",
        'embed_model = "   "',
    ],
)
def test_embed_values_out_of_range_are_errors(tmp_path: Path, line: str) -> None:
    f = tmp_path / "config.toml"
    f.write_text(line + "\n")
    with pytest.raises(ValidationError):
        config.load(f)


def test_remote_llm_with_flag_and_urls_is_read(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('allow_remote_llm = true\nremote_llm_urls = ["https://llm.example.com/v1"]\n')
    cfg = config.load(f)
    assert cfg.allow_remote_llm is True
    assert cfg.remote_llm_urls == ["https://llm.example.com/v1"]


@pytest.mark.parametrize(
    "text",
    [
        "allow_remote_llm = true\n",
        "allow_remote_llm = true\nremote_llm_urls = []\n",
        'remote_llm_urls = ["https://llm.example.com/v1"]\n',
        'allow_remote_llm = false\nremote_llm_urls = ["https://llm.example.com/v1"]\n',
    ],
)
def test_remote_llm_needs_both_flag_and_urls(tmp_path: Path, text: str) -> None:
    f = tmp_path / "config.toml"
    f.write_text(text)
    with pytest.raises(ValidationError):
        config.load(f)


def test_plugins_default_is_no_paths(tmp_path: Path) -> None:
    assert config.load(tmp_path / "config.toml").plugins.paths == []


def test_plugins_paths_are_read(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[plugins]\npaths = ["/opt/tern-plugins/a.py", "/srv/b.py"]\n')
    assert config.load(f).plugins.paths == [Path("/opt/tern-plugins/a.py"), Path("/srv/b.py")]


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ('"relative/plugin.py"', "absolute"),
        ('"~/plugin.py"', "absolute"),
        ('"/abs/plugins"', ".py file"),
        ('"/abs/plugin.txt"', ".py file"),
    ],
)
def test_plugins_paths_must_be_absolute_py_files(tmp_path: Path, value: str, reason: str) -> None:
    f = tmp_path / "config.toml"
    f.write_text(f"[plugins]\npaths = [{value}]\n")
    with pytest.raises(ValidationError, match=reason) as ei:
        config.load(f)
    assert value.strip('"') not in str(ei.value)  # hide_input_in_errors


@pytest.mark.parametrize(
    "text",
    [
        '[plugins]\npath = ["/abs/a.py"]\n',  # unknown key in the table
        '[plugins]\npaths = "/abs/a.py"\n',  # not a list
        'plugins = ["/abs/a.py"]\n',  # not a table
    ],
)
def test_plugins_table_errors(tmp_path: Path, text: str) -> None:
    f = tmp_path / "config.toml"
    f.write_text(text)
    with pytest.raises(ValidationError):
        config.load(f)


def test_chat_and_agent_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = config.load(tmp_path / "config.toml")
    assert cfg.chat_base_url == "http://127.0.0.1:47831/v1"
    assert cfg.chat_model == "iq3_xxs"
    assert (cfg.chat_temperature, cfg.chat_seed) == (0.0, 0)
    assert (cfg.chat_max_tokens, cfg.chat_request_timeout_s) == (4096, 120)
    assert cfg.llm_runtime == "lmstudio"
    assert cfg.lmstudio_cli == tmp_path / "home" / ".cache" / "lm-studio" / "bin" / "lms"
    assert cfg.agent_max_tool_calls == 0
    assert cfg.agent_turn_timeout_s == 0
    assert cfg.agent_context_budget_tokens == 200000


def test_chat_and_agent_values_are_read(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        'chat_base_url = "http://localhost:8080/v1"\nchat_model = "other-model"\n'
        "chat_temperature = 0.7\nchat_seed = 42\nchat_max_tokens = 512\n"
        'chat_request_timeout_s = 30.5\nllm_runtime = "lmstudio"\n'
        'lmstudio_cli = "/opt/lms/bin/lms"\nagent_max_tool_calls = 3\n'
        "agent_turn_timeout_s = 60\nagent_context_budget_tokens = 4000\n"
    )
    cfg = config.load(f)
    assert (cfg.chat_base_url, cfg.chat_model) == ("http://localhost:8080/v1", "other-model")
    assert (cfg.chat_temperature, cfg.chat_seed, cfg.chat_max_tokens) == (0.7, 42, 512)
    assert cfg.chat_request_timeout_s == 30.5
    assert cfg.lmstudio_cli == Path("/opt/lms/bin/lms")
    assert (cfg.agent_max_tool_calls, cfg.agent_turn_timeout_s) == (3, 60)
    assert cfg.agent_context_budget_tokens == 4000


@pytest.mark.parametrize(
    "line",
    [
        'chat_model = ""',
        'chat_model = "  "',
        "chat_temperature = -0.1",
        "chat_temperature = 2.5",
        "chat_seed = -1",
        "chat_max_tokens = 0",
        "chat_request_timeout_s = 0",
        'llm_runtime = "ollama"',
        'lmstudio_cli = "lms"',
        'lmstudio_cli = "~/lms"',
        "agent_max_tool_calls = -1",
        "agent_turn_timeout_s = -1",
        "agent_context_budget_tokens = 10",
        'chat_modle = "typo"',
    ],
)
def test_chat_and_agent_values_out_of_range_are_errors(tmp_path: Path, line: str) -> None:
    f = tmp_path / "config.toml"
    f.write_text(line + "\n")
    with pytest.raises(ValidationError):
        config.load(f)
