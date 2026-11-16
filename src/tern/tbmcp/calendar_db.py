"""Read-only queries over Thunderbird's calendar databases.

Thunderbird stores calendar items in ``calendar-data/local.sqlite`` (local
calendars) and ``calendar-data/cache.sqlite`` (offline caches of CalDAV, Google
and ICS calendars). We query private snapshots of both (see ``snapshot.py``).

Relevant schema (schema version 23):

    cal_events(cal_id, id, title, flags, privacy, ical_status,
               event_start, event_end, event_start_tz, event_end_tz,
               recurrence_id, recurrence_id_tz, offline_journal, ...)
    cal_properties(item_id, cal_id, recurrence_id, key, value)   DESCRIPTION, LOCATION, ...
    cal_attendees(item_id, cal_id, recurrence_id, icalString)    ATTENDEE / ORGANIZER lines
    cal_recurrence(item_id, cal_id, icalString)                  RRULE / EXDATE / RDATE lines

Storage conventions:

* Times are microseconds since the Unix epoch. Zoned times are UTC instants;
  *floating* times (``event_start_tz = 'floating'``, which includes all-day
  events) store the wall-clock time as if it were UTC. We interpret floating
  times in your timezone (see ``guard.user_timezone``).
* All-day events have flag 8 and an exclusive end (a one-day event on the 11th
  ends at midnight on the 12th).
* A recurring series is one row with ``recurrence_id IS NULL`` plus its
  ``cal_recurrence`` lines. A modified occurrence is another row with the same
  ``id`` and ``recurrence_id`` set to the start of the occurrence it replaces.
* ``offline_journal = 3`` marks an item deleted while offline.

Calendar names come from ``calendar.registry.<cal_id>.*`` in ``prefs.js``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrule, rruleset, rrulestr

from tern.tbmcp import accounts, guard, snapshot
from tern.tbmcp.db import _clamp, _like, _parse_date

FLAG_PRIVATE = 1
FLAG_ALLDAY = 8
FLAG_RECURRENCE_ID_ALLDAY = 512

MAX_LIMIT = 200
MAX_WINDOW_DAYS = 366 * 2
DEFAULT_WINDOW_DAYS = 30
PREVIEW_CHARS = 300
MAX_DESCRIPTION_CHARS = 50_000
_CHUNK = 500
_DAY_US = 86_400_000_000
_MARGIN_US = _DAY_US  # floating times may be up to ~14h off UTC

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = timedelta(microseconds=1)
_PROPS = ("DESCRIPTION", "LOCATION", "TRANSP", "STATUS", "URL")


# --- connections -----------------------------------------------------------------


def _connections() -> list[sqlite3.Connection]:
    snapshot.ensure_calendar_fresh()
    cons = []
    for path in snapshot.calendar_snapshot_paths():
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        cons.append(con)
    return cons


def _chunks(items: list, n: int = _CHUNK) -> Iterator[list]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


# --- time helpers ----------------------------------------------------------------


def _now_us() -> int:
    return _dt_to_us(datetime.now(UTC))


def _us_to_dt(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=us)


def _dt_to_us(dt: datetime) -> int:
    if dt.tzinfo is None:  # floating: wall time stored as if UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - _EPOCH) // _US


def _zone(tz: str | None):
    """tzinfo for a Thunderbird timezone id; ``None`` means floating."""
    if not tz or tz == "floating":
        return None
    if tz in ("UTC", "Z"):
        return UTC
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):  # unknown or custom VTIMEZONE blob: UTC
        return UTC


def _instant(stored_us: int, zone, user_tz: tzinfo) -> int:
    """Real UTC instant of a stored time (floating = wall time in your timezone)."""
    if zone is not None:
        return stored_us
    wall = _us_to_dt(stored_us).replace(tzinfo=None)
    return _dt_to_us(wall.replace(tzinfo=user_tz))


def _fmt(us: int, zone, all_day: bool) -> str:
    dt = _us_to_dt(us)
    if all_day:
        return dt.date().isoformat()
    if zone is None:
        return dt.replace(tzinfo=None).isoformat()
    return dt.astimezone(zone).isoformat()


def _fmt_span(start_us: int, end_us: int, zone, all_day: bool) -> tuple[str, str]:
    if all_day:  # report an inclusive last day: a 1-day event starts and ends on the same date
        first = _us_to_dt(start_us).date()
        last = max(first, _us_to_dt(end_us).date() - timedelta(days=1))
        return first.isoformat(), last.isoformat()
    return _fmt(start_us, zone, False), _fmt(end_us, zone, False)


def _local_iso(instant_us: int, user_tz: tzinfo) -> str:
    return _us_to_dt(instant_us).astimezone(user_tz).isoformat()


# --- iCalendar line parsing ------------------------------------------------------


def _split_outside_quotes(s: str, sep: str, maxsplit: int = -1) -> list[str]:
    parts, buf, quoted = [], [], False
    for ch in s:
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted and maxsplit != 0:
            parts.append("".join(buf))
            buf = []
            maxsplit -= 1
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_line(line: str) -> tuple[str, dict[str, str], str]:
    """``NAME;P1=a;P2="b:c":value`` -> ``("NAME", {"P1": "a", "P2": "b:c"}, "value")``."""
    line = line.replace("\r\n ", "").replace("\n ", "").replace("\r\n\t", "").strip()
    parts = _split_outside_quotes(line, ":", 1)
    head, value = parts[0], (parts[1] if len(parts) > 1 else "")
    name, *raw = _split_outside_quotes(head, ";")
    params = {}
    for p in raw:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return name.upper(), params, value


def _parse_ical_dt(
    value: str, params: dict[str, str], dtstart: datetime, end_of_day: bool = False
) -> datetime | None:
    """Parse an iCal DATE / DATE-TIME so it is comparable with ``dtstart``."""
    v = value.strip()
    try:
        if len(v) == 8:  # DATE
            d = datetime.strptime(v, "%Y%m%d")
            if end_of_day:
                d += timedelta(days=1) - _US
            else:
                d = d.replace(hour=dtstart.hour, minute=dtstart.minute, second=dtstart.second)
            return d.replace(tzinfo=dtstart.tzinfo)
        if v.endswith("Z"):
            d = datetime.strptime(v[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        else:
            d = datetime.strptime(v, "%Y%m%dT%H%M%S")
            if "TZID" in params:
                d = d.replace(tzinfo=_zone(params["TZID"]))
    except ValueError:
        return None
    if dtstart.tzinfo is None:
        return d.replace(tzinfo=None)  # floating series: compare wall times
    if d.tzinfo is None:
        return d.replace(tzinfo=dtstart.tzinfo)
    return d


def _build_rule(value: str, dtstart: datetime) -> rrule:
    keep, until = [], None
    for part in value.strip().split(";"):
        if not part:
            continue
        key, _, val = part.partition("=")
        if key.upper() == "UNTIL":
            until = _parse_ical_dt(val, {}, dtstart, end_of_day=True)
        else:
            keep.append(part)
    rule = rrulestr(";".join(keep), dtstart=dtstart)
    if not isinstance(rule, rrule):  # one RRULE value never parses to a set
        raise ValueError("recurrence rule is not a single rule")
    return rule.replace(until=until) if until is not None else rule


def _occurrences(
    row: sqlite3.Row,
    lines: list[str],
    skip_stored: set[int],
    win_start: int,
    win_end: int,
    max_count: int,
    user_tz: tzinfo,
) -> list[int]:
    """Stored start times of a series' occurrences overlapping the window (instants)."""
    all_day = bool(row["flags"] & FLAG_ALLDAY)
    zone = None if all_day else _zone(row["event_start_tz"])
    start = _us_to_dt(row["event_start"])
    dtstart = start.replace(tzinfo=None) if zone is None else start.astimezone(zone)
    duration = (row["event_end"] or row["event_start"]) - row["event_start"]

    rset = rruleset()
    rset.rdate(dtstart)  # DTSTART is always the first instance (RFC 5545)
    for line in lines:
        name, params, value = _parse_line(line)
        if name in ("RRULE", "EXRULE"):
            rule = _build_rule(value, dtstart)
            rset.rrule(rule) if name == "RRULE" else rset.exrule(rule)
        elif name in ("RDATE", "EXDATE"):
            for v in value.split(","):
                dt = _parse_ical_dt(v, params, dtstart)
                if dt is not None:
                    rset.rdate(dt) if name == "RDATE" else rset.exdate(dt)

    lo = _us_to_dt(win_start - duration)
    lo = lo.astimezone(user_tz).replace(tzinfo=None) if zone is None else lo.astimezone(zone)
    out = []
    for dt in rset.xafter(lo, count=max_count + len(skip_stored), inc=False):
        stored = _dt_to_us(dt)
        if _instant(stored, zone, user_tz) >= win_end:
            break
        if stored not in skip_stored:
            out.append(stored)
            if len(out) >= max_count:
                break
    return out


def _party(line: str) -> dict[str, Any] | None:
    name, params, value = _parse_line(line)
    if name not in ("ATTENDEE", "ORGANIZER"):
        return None
    email = value[7:] if value.lower().startswith("mailto:") else value
    out: dict[str, Any] = {"name": params.get("CN") or None, "email": (email or "").lower() or None}
    if name == "ATTENDEE":
        out["status"] = params.get("PARTSTAT")
        out["role"] = params.get("ROLE")
    return out


# --- scope -----------------------------------------------------------------------


def _registry() -> dict[str, dict]:
    return accounts.load_calendars()


def _name(cal_id: str, registry: dict) -> str:
    return registry.get(cal_id, {}).get("name", cal_id)


def _select_cal_ids(
    calendar: str | None, registry: dict[str, dict], present: Iterable[str]
) -> set[str]:
    ids = {c for c in set(registry) | set(present) if guard.calendar_allowed(c, _name(c, registry))}
    enabled = {c for c in ids if not registry.get(c, {}).get("disabled")}
    if not calendar:
        return enabled
    want = calendar.strip().lower()
    exact = {c for c in ids if want in (c.lower(), _name(c, registry).lower())}
    if exact:
        return exact
    return {c for c in enabled if want in _name(c, registry).lower()}


def _is_private(r: sqlite3.Row) -> bool:
    return bool(r["flags"] & FLAG_PRIVATE) or (r["privacy"] or "").upper() in (
        "PRIVATE",
        "CONFIDENTIAL",
    )


_LIVE = "(e.offline_journal IS NULL OR e.offline_journal != 3)"


# --- loading ---------------------------------------------------------------------


def _props(
    con: sqlite3.Connection, keys: list[tuple[str, str]]
) -> dict[tuple[str, str, int | None], dict[str, str]]:
    out: dict[tuple[str, str, int | None], dict[str, str]] = {}
    ids = sorted({i for _, i in keys})
    marks = ",".join("?" * len(_PROPS))
    for chunk in _chunks(ids):
        sql = (
            f"SELECT cal_id, item_id, recurrence_id, key, value FROM cal_properties "
            f"WHERE key IN ({marks}) AND item_id IN ({','.join('?' * len(chunk))})"
        )
        for r in con.execute(sql, [*_PROPS, *chunk]):
            v = r["value"]
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            out.setdefault((r["cal_id"], r["item_id"], r["recurrence_id"]), {})[r["key"]] = v
    return out


def _lines(
    con: sqlite3.Connection, table: str, keys: list[tuple[str, str]], with_rid: bool
) -> dict[tuple, list[str]]:
    out: dict[tuple, list[str]] = {}
    ids = sorted({i for _, i in keys})
    rid = ", recurrence_id" if with_rid else ""
    for chunk in _chunks(ids):
        sql = (
            f"SELECT cal_id, item_id{rid}, icalString FROM {table} "
            f"WHERE item_id IN ({','.join('?' * len(chunk))}) ORDER BY rowid"
        )
        for r in con.execute(sql, chunk):
            key = (
                (r["cal_id"], r["item_id"], r["recurrence_id"])
                if with_rid
                else (r["cal_id"], r["item_id"])
            )
            out.setdefault(key, []).append(r["icalString"] or "")
    return out


@dataclass
class _Occ:
    """One occurrence of an event, with everything needed to format it."""

    row: sqlite3.Row
    cal_id: str
    item_id: str
    stored_start: int  # storage convention (floating = wall time as UTC)
    start: int  # real instant
    end: int  # real instant
    zone: Any
    all_day: bool
    recurring: bool
    props: dict[str, str]
    people: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def stored_end(self) -> int:
        return self.stored_start + (self.end - self.start)


def _collect(
    win_start: int,
    win_end: int,
    *,
    query: str | None = None,
    calendar: str | None = None,
    attendee: str | None = None,
    event_id: str | None = None,
    limit: int = MAX_LIMIT,
) -> tuple[list[_Occ], bool]:
    """Occurrences overlapping [win_start, win_end) (real instants), sorted by start.

    Returns the occurrences (at most ``limit``) and whether more existed.
    """
    registry, user_tz = _registry(), guard.user_timezone()
    cutoff = guard.max_age_cutoff_us()
    if cutoff is not None:
        win_start = max(win_start, cutoff)
    if win_start >= win_end:
        return [], False

    found: dict[tuple[str, str, int | None], tuple[sqlite3.Row, dict, list[str], list[str]]] = {}
    cons = _connections()
    try:
        for con in cons:
            present = [r[0] for r in con.execute("SELECT DISTINCT cal_id FROM cal_events")]
            cal_ids = sorted(_select_cal_ids(calendar, registry, present) & set(present))
            if not cal_ids:
                continue
            where = [
                _LIVE,
                "e.event_start IS NOT NULL",
                f"e.cal_id IN ({','.join('?' * len(cal_ids))})",
            ]
            params: list[Any] = list(cal_ids)
            lo, hi = win_start - _MARGIN_US, win_end + _MARGIN_US
            where.append(
                "(e.recurrence_id IS NOT NULL"
                " OR EXISTS (SELECT 1 FROM cal_recurrence r"
                "            WHERE r.item_id = e.id AND r.cal_id = e.cal_id)"
                " OR (e.event_start < ? AND COALESCE(e.event_end, e.event_start) >= ?))"
            )
            params += [hi, lo]
            if event_id:
                where.append("e.id = ?")
                params.append(event_id)
            if query:
                like = _like(query)
                where.append(
                    "(e.cal_id, e.id) IN (SELECT s.cal_id, s.id FROM cal_events s"
                    " WHERE s.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM cal_properties p"
                    "   WHERE p.item_id = s.id AND p.cal_id = s.cal_id"
                    "   AND p.key IN ('DESCRIPTION', 'LOCATION') AND p.value LIKE ? ESCAPE '\\'))"
                )
                params += [like, like]
            if attendee:
                where.append(
                    "(e.cal_id, e.id) IN (SELECT a.cal_id, a.item_id FROM cal_attendees a"
                    " WHERE lower(a.icalString) LIKE ? ESCAPE '\\')"
                )
                params.append(_like(attendee.lower()))
            rows = con.execute(
                f"SELECT e.* FROM cal_events e WHERE {' AND '.join(where)}", params
            ).fetchall()
            if not rows:
                continue
            keys = [(r["cal_id"], r["id"]) for r in rows]
            props = _props(con, keys)
            rec = _lines(con, "cal_recurrence", keys, with_rid=False)
            att = _lines(con, "cal_attendees", keys, with_rid=True)
            for r in rows:
                k = (r["cal_id"], r["id"], r["recurrence_id"])
                if k not in found:  # same item may be in both local and cache dbs
                    found[k] = (
                        r,
                        props.get(k, {}),
                        rec.get((r["cal_id"], r["id"]), []),
                        att.get(k, []) or att.get((r["cal_id"], r["id"], None), []),
                    )
    finally:
        for con in cons:
            con.close()

    exceptions: dict[tuple[str, str], set[int]] = {}
    for cal_id, item_id, rid in found:
        if rid is not None:
            exceptions.setdefault((cal_id, item_id), set()).add(rid)

    occ: list[_Occ] = []
    for (cal_id, item_id, rid), (r, props, rec_lines, people) in found.items():
        all_day = bool(r["flags"] & FLAG_ALLDAY)
        zone = None if all_day else _zone(r["event_start_tz"])
        dur = (r["event_end"] or r["event_start"]) - r["event_start"]
        series = rid is None and bool(rec_lines)
        error = None

        def overlaps(stored: int, zone=zone, dur=dur) -> bool:
            s = _instant(stored, zone, user_tz)
            return s < win_end and (s + dur > win_start or (dur == 0 and s >= win_start))

        if series:
            try:  # one past the limit so callers can tell a series was cut off
                starts = _occurrences(
                    r,
                    rec_lines,
                    exceptions.get((cal_id, item_id), set()),
                    win_start,
                    win_end,
                    limit + 1,
                    user_tz,
                )
            except (ValueError, TypeError, OverflowError) as e:
                # malformed rule: show the first instance, flag it in the output
                starts = [r["event_start"]] if overlaps(r["event_start"]) else []
                error = f"could not expand recurrence: {e}"
        else:
            starts = [r["event_start"]] if overlaps(r["event_start"]) else []
        for s in starts:
            inst = _instant(s, zone, user_tz)
            occ.append(
                _Occ(
                    r,
                    cal_id,
                    item_id,
                    s,
                    inst,
                    inst + dur,
                    zone,
                    all_day,
                    series or rid is not None,
                    props,
                    people,
                    error,
                )
            )

    occ.sort(key=lambda o: (o.start, o.row["title"] or ""))
    return occ[:limit], len(occ) > limit


def _format(o: _Occ, registry: dict, mode: str) -> dict[str, Any] | None:
    private = _is_private(o.row)
    if private and mode == "hide":
        return None
    start_str, end_str = _fmt_span(o.stored_start, o.stored_end, o.zone, o.all_day)
    item: dict[str, Any] = {
        "event_id": o.item_id,
        "calendar": _name(o.cal_id, registry),
        "calendar_id": o.cal_id,
        "title": o.row["title"] or "",
        "start": start_str,
        "end": end_str,
        "all_day": o.all_day,
        "timezone": "floating" if o.zone is None else str(o.zone),
        "location": o.props.get("LOCATION") or "",
        "status": o.row["ical_status"] or o.props.get("STATUS"),
        "recurring": o.recurring,
        "private": private,
        "attendees": sum(1 for line in o.people if line.upper().startswith("ATTENDEE")),
        "description_preview": "",
    }
    if private and mode == "busy":
        item.update(title="Busy", location="", attendees=0)
    else:
        guard.protect(
            item,
            ("title", "location", "description_preview"),
            full_texts={"description_preview": o.props.get("DESCRIPTION") or ""},
            limits={"description_preview": PREVIEW_CHARS},
        )
    if o.error:
        item["error"] = o.error
    return item


# --- public queries --------------------------------------------------------------


def calendars() -> list[dict]:
    """Calendars you allow, from prefs.js, with the number of events each holds."""
    registry = _registry()
    items: set[tuple[str, str]] = set()  # an item may be in both local and cache dbs
    cons = _connections()
    try:
        for con in cons:
            sql = f"SELECT DISTINCT cal_id, id FROM cal_events e WHERE {_LIVE}"
            items.update((r["cal_id"], r["id"]) for r in con.execute(sql))
    finally:
        for con in cons:
            con.close()
    counts: dict[str, int] = {}
    for cal_id, _ in items:
        counts[cal_id] = counts.get(cal_id, 0) + 1
    out = []
    for cal_id in sorted(set(registry) | set(counts), key=lambda c: _name(c, registry).lower()):
        if not guard.calendar_allowed(cal_id, _name(cal_id, registry)):
            continue
        info = registry.get(
            cal_id,
            {
                "name": cal_id,
                "type": None,
                "uri": None,
                "disabled": False,
                "read_only": False,
                "cached": False,
            },
        )
        out.append({"calendar_id": cal_id, **info, "events": counts.get(cal_id, 0)})
    return out


def _window(
    since: str | None, until: str | None, default_days: int = DEFAULT_WINDOW_DAYS
) -> tuple[int, int]:
    user_tz = guard.user_timezone()
    if since:
        start = _parse_date(since)[0]
    elif until:
        start = None
    else:
        start = datetime.combine(datetime.now(user_tz).date(), time.min, tzinfo=user_tz)
    if until:
        end, date_only = _parse_date(until)
        if date_only:
            end += timedelta(days=1)
    else:
        assert start is not None  # no until: start was set above
        end = start + timedelta(days=default_days)
    if start is None:
        start = end - timedelta(days=default_days)
    if (end - start).days > MAX_WINDOW_DAYS:
        end = start + timedelta(days=MAX_WINDOW_DAYS)
    return _dt_to_us(start), _dt_to_us(end)


def events(
    since: str | None = None,
    until: str | None = None,
    query: str | None = None,
    calendar: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Event occurrences overlapping ``[since, until]``, recurring series expanded.

    Defaults to the next 30 days from today (your timezone). A bare ``until``
    date is inclusive. ``query`` matches title, description and location as a
    literal substring. Disabled calendars are skipped unless named in ``calendar``.
    """
    limit = _clamp(limit, 1, MAX_LIMIT)
    win_start, win_end = _window(since, until)
    occ, more = _collect(win_start, win_end, query=query, calendar=calendar, limit=limit)
    registry, mode, user_tz = _registry(), guard.private_events_mode(), guard.user_timezone()
    items = [x for x in (_format(o, registry, mode) for o in occ) if x]
    return {
        "since": _local_iso(win_start, user_tz),
        "until": _local_iso(win_end, user_tz),
        "events": items,
        "more": more,
    }


def events_with(
    email: str, since: str | None = None, until: str | None = None, limit: int = 20
) -> list[dict]:
    """Events where ``email`` is an attendee or the organizer."""
    win_start, win_end = _window(since, until)
    occ, _ = _collect(win_start, win_end, attendee=email, limit=_clamp(limit, 1, MAX_LIMIT))
    registry, mode = _registry(), guard.private_events_mode()
    return [x for x in (_format(o, registry, mode) for o in occ) if x]


def next_occurrence(event_id: str, calendar_id: str | None = None) -> dict | None:
    """The next (or currently running) occurrence of an event within a year."""
    now = _now_us()
    occ, _ = _collect(now, now + 366 * _DAY_US, event_id=event_id, limit=1, calendar=calendar_id)
    if not occ:
        return None
    return _format(occ[0], _registry(), guard.private_events_mode())


def _attendee_says_no(o: _Occ, mine: set[str]) -> bool:
    for line in o.people:
        p = _party(line)
        if p and p.get("status") == "DECLINED" and p.get("email") in mine:
            return True
    return False


def busy_intervals(win_start: int, win_end: int, calendar: str | None = None) -> list[_Occ]:
    """Occurrences that block time: not all-day, not transparent, not cancelled
    or declined by you."""
    occ, _ = _collect(win_start, win_end, calendar=calendar, limit=5000)
    mine = accounts.my_addresses()
    return [
        o
        for o in occ
        if not o.all_day
        and (o.props.get("TRANSP") or "OPAQUE").upper() != "TRANSPARENT"
        and (o.row["ical_status"] or o.props.get("STATUS") or "").upper() != "CANCELLED"
        and not _attendee_says_no(o, mine)
    ]


def _parse_hhmm(s: str) -> time:
    h, _, m = s.strip().partition(":")
    return time(int(h), int(m or 0))


def free_slots(
    since: str | None = None,
    until: str | None = None,
    min_minutes: int = 30,
    work_start: str = "09:00",
    work_end: str = "17:00",
    weekdays_only: bool = True,
    calendar: str | None = None,
) -> dict[str, Any]:
    """Free time within working hours, plus double-bookings, in your timezone.

    Busy time is every timed event that isn't marked free/transparent,
    cancelled, or declined by you. All-day events don't block time.
    Defaults to the next 7 days.
    """
    user_tz = guard.user_timezone()
    win_start, win_end = _window(since, until, default_days=7)
    busy = busy_intervals(win_start, win_end, calendar)
    ws, we = _parse_hhmm(work_start), _parse_hhmm(work_end)
    min_us = max(1, int(min_minutes)) * 60_000_000
    now = _now_us()

    intervals = sorted((o.start, o.end) for o in busy)
    slots = []
    day = _us_to_dt(win_start).astimezone(user_tz).date()
    last_day = _us_to_dt(win_end - 1).astimezone(user_tz).date()
    while day <= last_day:
        if not (weekdays_only and day.weekday() >= 5):
            lo = max(_dt_to_us(datetime.combine(day, ws, tzinfo=user_tz)), win_start, now)
            hi = min(_dt_to_us(datetime.combine(day, we, tzinfo=user_tz)), win_end)
            cursor = lo
            for s, e in intervals:
                if e <= cursor or s >= hi:
                    continue
                if s - cursor >= min_us:
                    slots.append((cursor, s))
                cursor = max(cursor, e)
            if hi - cursor >= min_us:
                slots.append((cursor, hi))
        day += timedelta(days=1)

    registry, mode = _registry(), guard.private_events_mode()
    conflicts = []
    for i, a in enumerate(busy):
        for b in busy[i + 1 :]:
            if b.start >= a.end:
                break
            fa, fb = (
                _format(a, registry, "busy" if mode == "hide" else mode),
                _format(b, registry, "busy" if mode == "hide" else mode),
            )
            conflicts.append(
                {
                    "overlap_start": _local_iso(b.start, user_tz),
                    "overlap_end": _local_iso(min(a.end, b.end), user_tz),
                    "events": [
                        {k: f[k] for k in ("event_id", "title", "start", "end", "calendar")}
                        for f in (fa, fb)
                        if f
                    ],
                }
            )
    return {
        "timezone": str(user_tz),
        "since": _local_iso(win_start, user_tz),
        "until": _local_iso(win_end, user_tz),
        "working_hours": f"{ws:%H:%M}-{we:%H:%M}" + (" Mon-Fri" if weekdays_only else ""),
        "slots": [
            {
                "start": _local_iso(s, user_tz),
                "end": _local_iso(e, user_tz),
                "minutes": (e - s) // 60_000_000,
            }
            for s, e in slots
        ],
        "conflicts": conflicts,
    }


def get_event(
    event_id: str, calendar_id: str | None = None, description_chars: int = 8_000
) -> dict | None:
    """Full details of one event or recurring series."""
    description_chars = _clamp(description_chars, 0, MAX_DESCRIPTION_CHARS)
    registry, mode = _registry(), guard.private_events_mode()
    cons = _connections()
    try:
        for con in cons:
            sql = f"SELECT e.* FROM cal_events e WHERE {_LIVE} AND e.id = ?"
            params: list[Any] = [event_id]
            if calendar_id:
                sql += " AND e.cal_id = ?"
                params.append(calendar_id)
            rows = [
                r
                for r in con.execute(
                    sql + " ORDER BY e.recurrence_id IS NOT NULL, e.recurrence_id", params
                )
                if guard.calendar_allowed(r["cal_id"], _name(r["cal_id"], registry))
            ]
            if not rows:
                continue
            cal_id = rows[0]["cal_id"]
            rows = [r for r in rows if r["cal_id"] == cal_id]
            keys = [(cal_id, event_id)]
            props = _props(con, keys)
            parties = _lines(con, "cal_attendees", keys, with_rid=True)
            rec = _lines(con, "cal_recurrence", keys, with_rid=False).get(keys[0], [])
            break
        else:
            return None
    finally:
        for con in cons:
            con.close()

    cutoff = guard.max_age_cutoff_us()
    master_row = next((r for r in rows if r["recurrence_id"] is None), rows[0])
    if cutoff is not None and not rec and (master_row["event_end"] or 0) < cutoff:
        return None
    if _is_private(master_row) and mode == "hide":
        return None

    def describe(r: sqlite3.Row) -> dict[str, Any]:
        all_day = bool(r["flags"] & FLAG_ALLDAY)
        zone = None if all_day else _zone(r["event_start_tz"])
        end_us = r["event_end"] or r["event_start"]
        start_str, end_str = _fmt_span(r["event_start"], end_us, zone, all_day)
        base = {
            "start": start_str,
            "end": end_str,
            "all_day": all_day,
            "timezone": "floating" if zone is None else str(zone),
            "private": _is_private(r),
        }
        if _is_private(r) and mode == "busy":
            return {"title": "Busy", **base}
        p = props.get((cal_id, event_id, r["recurrence_id"]), {})
        people = [
            x for x in map(_party, parties.get((cal_id, event_id, r["recurrence_id"]), [])) if x
        ]
        organizer = next((x for x in people if "status" not in x), None)
        out = {
            "title": r["title"] or "",
            **base,
            "location": p.get("LOCATION") or "",
            "url": p.get("URL"),
            "status": r["ical_status"] or p.get("STATUS"),
            "organizer": organizer,
            "attendees": [x for x in people if "status" in x],
            "description": "",
        }
        lengths = guard.protect(
            out,
            ("title", "location", "description"),
            full_texts={"description": p.get("DESCRIPTION") or ""},
            limits={"description": description_chars},
        )
        for person in ([organizer] if organizer else []) + out["attendees"]:
            if person.get("name"):
                person["name"] = guard.clean(person["name"])[0]
        out["description_truncated"] = lengths["description"] > description_chars
        return out

    return {
        "event_id": event_id,
        "calendar": _name(cal_id, registry),
        "calendar_id": cal_id,
        **describe(master_row),
        "recurrence": [x.strip() for x in rec],
        "modified_occurrences": [
            {
                "original_start": _fmt(
                    r["recurrence_id"],
                    _zone(r["recurrence_id_tz"]),
                    bool(r["flags"] & FLAG_RECURRENCE_ID_ALLDAY),
                ),
                **describe(r),
            }
            for r in rows
            if r["recurrence_id"] is not None
        ],
    }


def attendee_emails(event: dict) -> list[dict]:
    """Organizer + attendees of a ``get_event`` result, as [{name, email, status}]."""
    people = []
    if event.get("organizer"):
        people.append({**event["organizer"], "status": "ORGANIZER"})
    people += [
        {"name": a.get("name"), "email": a.get("email"), "status": a.get("status")}
        for a in event.get("attendees", [])
    ]
    seen, out = set(), []
    for p in people:
        if p.get("email") and p["email"] not in seen:
            seen.add(p["email"])
            out.append(p)
    return out
