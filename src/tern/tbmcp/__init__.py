"""Vendored thunderbird-mcp (commit 4921d1e, MIT; see ``LICENSE`` here and the repo's ``NOTICE``).

Read-only queries over consistent private *snapshot* copies of Thunderbird's own SQLite databases
(the GLODA mail index, the calendar databases and the address books): ``snapshot.py`` copies them,
``db.py`` / ``calendar_db.py`` / ``contacts.py`` / ``briefing.py`` query the copies, ``guard.py``
applies the guardrails (scope, history limit, redaction, injection flags, output budget).

Decision (approved by the user 2026-11-16, amends decisions #20/#22 "ternd never reads TB profile
files"): tern's chat agent uses these tools. Changes from upstream, kept small:

- imports (``thunderbird_mcp`` -> ``tern.tbmcp``); upstream's FastMCP ``server.py`` is not vendored
  (tern exposes every registry tool through its own ``tern mcp``); the parts tern needs are in
  ``api.py`` (``INSTRUCTIONS``, the account and guardrail views);
- settings come from tern's config, never from the environment: ``guard.configure`` replaces the
  ``THUNDERBIRD_MCP_*`` variables, ``config.set_profile_dir`` replaces ``THUNDERBIRD_PROFILE`` and
  ``snapshot.set_cache_dir`` puts the snapshot copies under tern's state dir;
- upstream's own audit log (``guard.audit``) is removed: tern's audit (``Registry.call``) records
  every call;
- broad ``except Exception`` handlers are narrowed (tern's "fail loudly" rule), and the snapshot
  CLI (``print``) is not vendored;
- every listed message carries its ``header_message_id`` (``db.attachments`` items,
  ``db._summary`` for ``awaiting_reply`` / ``unanswered``, ``briefing._mail_brief`` for the daily
  brief and meeting prep), so tern can add its citable ``mid`` (plan 08c);
- the de-duplication by Message-ID (``db._DEDUP``) skips the Message-IDs a predicate installed
  with ``db.set_split`` names (SQL function ``tern_split``): tern names those its index marks
  conflicted, so a spoofed copy cannot hide the genuine one (plan 08c).

The tern-tool/1 wrappers are in ``tern.tools.builtin.tb``.
"""

__version__ = "0.1.0"
#: The upstream commit this copy was taken from.
UPSTREAM_COMMIT = "4921d1e"
