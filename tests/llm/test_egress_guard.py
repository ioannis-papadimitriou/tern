"""Plan 05 step 2: the loopback egress guard (decisions #9). No DNS, no sockets."""

from __future__ import annotations

import pytest

from tern.config import TernConfig
from tern.llm.egress import EgressDenied, check_url

LOOPBACK_OK = [
    "http://127.0.0.1:1234/v1",
    "http://127.0.0.1/v1",
    "https://127.0.0.1:8443/v1",
    "http://127.5.6.7:1234/v1",
    "http://127.255.255.254/",
    "http://[::1]:1234/v1",
    "http://[::1]/v1",
    "http://localhost:1234/v1",
    "HTTP://LOCALHOST:1234/v1",
]

DENIED_BY_DEFAULT = [
    "http://127.0.0.1.evil.test/v1",  # a name that only starts like a loopback literal
    "http://localhost.evil.test/v1",
    "http://evil.test/v1",
    "https://api.example.com/v1",
    "http://10.0.0.1:1234/v1",
    "http://0.0.0.0:1234/v1",
    "http://8.8.8.8/v1",
    "http://[::ffff:8.8.8.8]/v1",  # IPv4-mapped IPv6
    "http://[::ffff:127.0.0.1]/v1",  # mapped, even onto loopback
    "http://[::127.0.0.1]/v1",  # IPv4-compatible, not ::1
    "http://[fe80::1]/v1",
    "http://[::1%25lo]/v1",  # zone id
    "http://127.1/v1",  # shorthand, not a strict IPv4 literal
    "http://2130706433/v1",  # decimal form of 127.0.0.1
]


@pytest.mark.parametrize("url", LOOPBACK_OK)
def test_loopback_is_allowed_and_returned_unchanged(url: str) -> None:
    assert check_url(url, TernConfig()) == url


@pytest.mark.parametrize("url", DENIED_BY_DEFAULT)
def test_non_loopback_is_denied_by_default(url: str) -> None:
    with pytest.raises(EgressDenied):
        check_url(url, TernConfig())


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost@evil.test/v1",
        "http://127.0.0.1@evil.test/v1",
        "http://hunter2:s3cretpw@127.0.0.1:1234/v1",
        "http://s3cretpw@localhost/v1",
    ],
)
def test_userinfo_is_denied_and_never_echoed(url: str) -> None:
    with pytest.raises(EgressDenied) as info:
        check_url(url, TernConfig())
    reason = str(info.value)
    assert "userinfo" in reason
    for secret in ("hunter2", "s3cretpw", "localhost@", "127.0.0.1@", "evil.test"):
        assert secret not in reason


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://127.0.0.1/v1",
        "file:///etc/passwd",
        "127.0.0.1:1234/v1",  # no scheme
        "localhost:s3cretpw@evil.test",  # parses as scheme "localhost"
        "http:///v1",  # no host
        "http://127.0.0.1:99999/v1",  # invalid port
        "http://127.0.0.1:12ab/v1",
        "http://127.0.0.1\\@evil.test/v1",  # backslash parser differential
        "http://127.0.0.1\t.evil.test/v1",  # urlsplit strips tabs; the HTTP client may not
        "http://127.0.0.1\n/v1",
        "http://127.0.0.1 /v1",
        "http://١٢٧.0.0.1/v1",  # non-ASCII digits
    ],
)
def test_malformed_urls_are_denied(url: str) -> None:
    with pytest.raises(EgressDenied) as info:
        check_url(url, TernConfig())
    assert "s3cretpw" not in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://[127.0.0.1]/",  # IPv4 in brackets
        "http://[::1/",  # unclosed bracket
        "http://[::1]x/",  # junk after the bracket
        "http://[::1]:1234@x/",  # bracketed "host" before an @ (urlsplit raises)
        "http://[::1]]/",
        "http://[]/",
        "http://[s3cretpw/",
    ],
)
def test_unparseable_bracketed_hosts_are_denied_not_crashing(url: str) -> None:
    with pytest.raises(EgressDenied, match="URL does not parse") as info:
        check_url(url, TernConfig())
    reason = str(info.value)
    assert "s3cretpw" not in reason
    assert "[" not in reason  # the URL is never echoed
    assert isinstance(info.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1234/v1?x",
        "http://127.0.0.1?@evil.test",  # query that looks like userinfo
        "http://127.0.0.1#@evil.test/",  # fragment that looks like userinfo
        "http://127.0.0.1:1234/v1?",  # empty query: "/embeddings" would land in it
        "http://127.0.0.1:1234/v1#",
        "http://[::1]:1234/v1#frag",
        "http://localhost:1234/v1?model=x",
    ],
)
def test_query_or_fragment_is_denied(url: str) -> None:
    with pytest.raises(EgressDenied, match="query or fragment") as info:
        check_url(url, TernConfig())
    assert "evil.test" not in str(info.value)


def test_listed_remote_url_with_a_query_is_still_denied() -> None:
    url = "https://llm.example.com/v1?key=s3cretpw"
    cfg = TernConfig(allow_remote_llm=True, remote_llm_urls=[url])
    with pytest.raises(EgressDenied, match="query or fragment") as info:
        check_url(url, cfg)
    assert "s3cretpw" not in str(info.value)


REMOTE = "https://llm.example.com/v1"


def test_remote_exact_match_is_allowed_with_the_flag() -> None:
    cfg = TernConfig(allow_remote_llm=True, remote_llm_urls=[REMOTE])
    assert check_url(REMOTE, cfg) == REMOTE
    # loopback stays allowed alongside
    assert check_url("http://127.0.0.1:1234/v1", cfg) == "http://127.0.0.1:1234/v1"


@pytest.mark.parametrize(
    "url",
    [
        REMOTE + "/",  # not string-equal
        "https://llm.example.com/v2",
        "http://llm.example.com/v1",
        "https://llm.example.com:443/v1",
        "https://other.example.com/v1",
        "https://user@llm.example.com/v1",
    ],
)
def test_remote_needs_an_exact_match(url: str) -> None:
    cfg = TernConfig(allow_remote_llm=True, remote_llm_urls=[REMOTE])
    with pytest.raises(EgressDenied):
        check_url(url, cfg)


def test_remote_listed_url_with_userinfo_is_still_denied() -> None:
    url = "https://user:pw@llm.example.com/v1"
    cfg = TernConfig(allow_remote_llm=True, remote_llm_urls=[url])
    with pytest.raises(EgressDenied, match="userinfo"):
        check_url(url, cfg)


def test_check_does_no_dns_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("DNS lookup attempted")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)
    assert check_url("http://localhost:1234/v1", TernConfig())
    with pytest.raises(EgressDenied):
        check_url("http://evil.test/v1", TernConfig())
