"""Loopback egress guard for model URLs (decisions #9, plan 05 step 2; plan 08a reuses it).

``check_url(url, cfg)`` returns ``url`` unchanged when a model client may talk to it, and raises
``EgressDenied`` otherwise. Rules, all checked without any DNS lookup (so no rebinding):

- only printable ASCII, no backslash (no parser-differential tricks between this check and the
  HTTP client);
- the URL parses (a malformed bracketed host such as ``[127.0.0.1]`` or ``[::1`` is denied);
- scheme ``http`` or ``https``;
- no userinfo (``user:pass@`` or ``localhost@evil.test``);
- no ``?`` or ``#`` at all: a model URL is a base URL that clients append paths to, so any query
  or fragment (even an empty one) would swallow the appended path, and ``127.0.0.1?@evil.test``
  is a parser-differential shape;
- host is an IPv4 literal in 127.0.0.0/8, the IPv6 literal ``::1`` (no zone id), or the literal
  name ``localhost``. IPv4-mapped IPv6 (``::ffff:…``) is rejected, whatever it maps to.

Any other host needs ``cfg.allow_remote_llm`` and an exact (string-equal) entry in
``cfg.remote_llm_urls``. A denial reason never echoes the URL's userinfo.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from tern.config import TernConfig

_LOOPBACK_V4 = ipaddress.IPv4Network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.IPv6Address("::1")
_SCHEMES = frozenset({"http", "https"})


class EgressDenied(Exception):
    """A model URL is not loopback and not an explicitly allowed remote URL."""


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # a name other than "localhost": never resolved, so not loopback
    if isinstance(addr, ipaddress.IPv4Address):
        return addr in _LOOPBACK_V4
    if addr.scope_id is not None or addr.ipv4_mapped is not None:
        return False
    return addr == _LOOPBACK_V6


def check_url(url: str, cfg: TernConfig) -> str:
    """Return ``url`` if a model client may connect to it; raise ``EgressDenied`` otherwise."""
    if not url or any(not ("!" <= c <= "~") or c == "\\" for c in url):
        raise EgressDenied("URL is empty or contains whitespace, control, non-ASCII or '\\'")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:  # e.g. "[127.0.0.1]", "[::1", "[::1]x", "[::1]:1234@x"
        raise EgressDenied("URL does not parse") from exc
    if parts.scheme.lower() not in _SCHEMES:
        raise EgressDenied("URL scheme is not http or https")
    if "@" in parts.netloc:
        raise EgressDenied("URL carries userinfo")
    if "?" in url or "#" in url:
        raise EgressDenied("URL has a query or fragment (a base URL takes neither)")
    if not host:
        raise EgressDenied("URL has no host")
    try:
        _ = parts.port
    except ValueError as exc:
        raise EgressDenied("URL has an invalid port") from exc
    if _is_loopback_host(host):
        return url
    if cfg.allow_remote_llm and url in cfg.remote_llm_urls:
        return url
    raise EgressDenied(
        f"host {host!r} is not loopback (127.0.0.0/8, ::1, localhost) and the URL is not an "
        "allowed remote URL (allow_remote_llm + remote_llm_urls)"
    )
