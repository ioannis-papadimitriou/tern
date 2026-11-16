"""tern.testing.procs: subprocess ternd runs are isolated from the embeddings server (plan 05
step 7 review, H1)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tern import config, paths
from tern.testing.procs import (
    env_with_home,
    isolate_from_embeddings,
    stop,
    ternd_cmd,
    wait_for_socket,
)


def _config_file(home: Path) -> Path:
    return paths.resolve({"TERN_HOME": str(home)}).config_file


def test_env_with_home_sets_tern_home_and_disables_embedding(tern_home: Path) -> None:
    env = env_with_home(tern_home)
    assert env["TERN_HOME"] == str(tern_home)
    cfg = config.load(_config_file(tern_home))
    assert cfg.embed_model is None
    assert cfg.model_dump(exclude={"embed_model"}) == config.TernConfig().model_dump(
        exclude={"embed_model"}
    )


def test_existing_keys_are_kept_and_embedding_is_disabled(tern_home: Path) -> None:
    f = _config_file(tern_home)
    f.parent.mkdir(parents=True)
    f.write_text('thunderbird_bin = "/x/tb"')  # no trailing newline
    assert isolate_from_embeddings(tern_home) == f
    cfg = config.load(f)
    assert (cfg.thunderbird_bin, cfg.embed_model) == (Path("/x/tb"), None)
    isolate_from_embeddings(tern_home)  # idempotent: no second line, still valid TOML
    assert f.read_text().count("embed_model") == 1


def test_an_explicit_embed_model_is_left_alone(tern_home: Path) -> None:
    f = _config_file(tern_home)
    f.parent.mkdir(parents=True)
    text = 'embed_model = "stub-model"\nembed_base_url = "http://127.0.0.1:9/v1"\n'
    f.write_text(text)
    env_with_home(tern_home)
    assert f.read_text() == text


def test_a_subprocess_ternd_started_with_it_runs_with_embedding_disabled(tern_home: Path) -> None:
    proc = subprocess.Popen(ternd_cmd(), env=env_with_home(tern_home))
    try:
        wait_for_socket(paths.resolve({"TERN_HOME": str(tern_home)}).socket)
    finally:
        assert stop(proc) == 0
    log = paths.resolve({"TERN_HOME": str(tern_home)}).state / "ternd.log"
    msgs = [json.loads(line)["msg"] for line in log.read_text().splitlines()]
    assert "embed disabled" in msgs
