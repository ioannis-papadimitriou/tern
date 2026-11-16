"""`tern install-host` (plan 02 step 6; S1: TB 153 reads ~/.mozilla/native-messaging-hosts/)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tern import cli
from tern.bridge.peercred import installed_nmhost_path

TEMPLATE = Path(__file__).resolve().parents[1] / "packaging" / "nmhost" / "tern.json.in"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERN_HOME", str(tmp_path / "th"))
    return tmp_path


def test_writes_into_the_mozilla_dir_by_default(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["install-host"]) == 0
    out = home / ".mozilla" / "native-messaging-hosts" / "tern.json"
    m = json.loads(out.read_text())
    assert m["name"] == "tern" and m["type"] == "stdio"
    assert m["allowed_extensions"] == ["tern@tern.invalid"]
    assert m["path"] == str(installed_nmhost_path()) and Path(m["path"]).is_absolute()
    assert not (home / ".thunderbird").exists()
    assert "wrote" in capsys.readouterr().out


def test_replacing_an_existing_manifest_says_so(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = home / ".mozilla" / "native-messaging-hosts"
    d.mkdir(parents=True)
    (d / "tern.json").write_text('{"name": "tern", "path": "/old/spike/nmhost.py"}')
    assert cli.main(["install-host"]) == 0
    assert "replaced" in capsys.readouterr().out
    assert json.loads((d / "tern.json").read_text())["path"] == str(installed_nmhost_path())


def test_refuses_a_relative_host_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        cli.install_host(tmp_path, Path("bin/tern-nmhost"))
    assert not (tmp_path / "tern.json").exists()


def test_matches_the_packaging_template() -> None:
    rendered = TEMPLATE.read_text().replace("@NMHOST_PATH@", "/abs/tern-nmhost")
    assert json.loads(rendered) == cli.host_manifest(Path("/abs/tern-nmhost"))
