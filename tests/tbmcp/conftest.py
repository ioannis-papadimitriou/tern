"""Shared fixtures: an isolated environment and a synthetic GLODA-shaped snapshot (ported from
thunderbird-mcp 4921d1e; settings via ``guard._settings`` instead of environment variables)."""

import pytest

from tern.tbmcp import accounts, guard, snapshot
from tern.tbmcp import config as tbconfig

from .synthetic import ME, build_snapshot


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch, request):
    """No guardrail settings leak in; UTC; caches under tmp_path; no real profile is ever found
    (an explicit profile dir that does not exist, and no discovery roots)."""
    monkeypatch.setattr(guard, "_settings", {"TIMEZONE": "UTC"})
    monkeypatch.setattr(tbconfig, "_override", tmp_path / "no-profile")
    monkeypatch.setattr(tbconfig, "_candidate_roots", lambda: [])
    tbconfig.profile_dir.cache_clear()
    request.addfinalizer(tbconfig.profile_dir.cache_clear)
    monkeypatch.setattr(snapshot, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(snapshot, "SNAPSHOT", tmp_path / "cache" / "snapshot.sqlite")
    monkeypatch.setattr(accounts, "local_timezone", lambda: None)
    monkeypatch.setattr(accounts, "my_addresses", lambda: set(ME))
    monkeypatch.setattr(accounts, "load_calendars", lambda: {})
    monkeypatch.setattr(snapshot, "contacts_snapshot_paths", lambda: [])
    monkeypatch.setattr(snapshot, "ensure_contacts_fresh", lambda: None)


@pytest.fixture
def fake_snapshot(tmp_path, monkeypatch, request):
    snap = tmp_path / "snapshot.sqlite"
    build_snapshot(snap, getattr(request, "param", ()))
    monkeypatch.setattr(snapshot, "SNAPSHOT", snap)
    monkeypatch.setattr(snapshot, "ensure_fresh", lambda: None)
    # Never read the real prefs.js of whoever runs the tests.
    monkeypatch.setattr(
        accounts,
        "server_emails",
        lambda: {
            ("bob", "mail.work.example.org"): ["bob@work.example.org"],
            ("alice@example.com", "imap.example.com"): ["alice@example.com"],
        },
    )
    yield snap
