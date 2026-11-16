"""Known recipients: To/Cc of messages found in Sent folders (plan 04 step 7, B-M8, D3; §2 step 7).

``sent_recipients(addr, first_seen, count)`` counts, per lower-cased address, the distinct
messages (mids) that were seen in a folder whose ``special_use`` holds ``sent``. Counting is
idempotent: a mid is counted once, on its first successful insert into ``sent_counted``, however
often it is re-ingested, and whether it later shows up in a second Sent folder. ``first_seen`` is
the minimum ``date_utc`` over the counted messages (NULL while every one of them is undated).
Rows are never removed, also when the message is deleted later: a known recipient stays known.

Writes go through the caller's Store transaction (one per message). Nothing here logs addresses.
"""

from __future__ import annotations

from collections.abc import Iterable

from tern.index.store import Store

_UPSERT = (
    "INSERT INTO sent_recipients(addr, first_seen, count) VALUES (?, ?, 1) "
    "ON CONFLICT(addr) DO UPDATE SET count = count + 1, first_seen = CASE "
    "WHEN excluded.first_seen IS NULL THEN first_seen "
    "WHEN first_seen IS NULL THEN excluded.first_seen "
    "ELSE min(first_seen, excluded.first_seen) END"
)


def count_sent(store: Store, mid: str, addrs: Iterable[str], date_utc: int | None) -> bool:
    """Count ``mid``'s recipients (its To and Cc addresses) once. Returns False when the mid was
    already counted (nothing changes), True when it is counted now. An address listed twice in
    one message (e.g. in To and in Cc) counts once."""
    with store.transaction():
        conn = store.connection
        if conn.execute("INSERT OR IGNORE INTO sent_counted(mid) VALUES (?)", (mid,)).rowcount != 1:
            return False
        for addr in sorted({a.strip().lower() for a in addrs if a.strip()}):
            conn.execute(_UPSERT, (addr, date_utc))
    return True


def known_recipients(store: Store) -> dict[str, tuple[int, int | None]]:
    """``{addr: (count, first_seen)}`` for every known recipient."""
    return {
        str(r[0]): (int(r[1]), None if r[2] is None else int(r[2]))
        for r in store.connection.execute(
            "SELECT addr, count, first_seen FROM sent_recipients ORDER BY addr"
        )
    }
