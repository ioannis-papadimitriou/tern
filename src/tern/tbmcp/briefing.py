"""Composite views that join mail, calendar and contacts."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from tern.tbmcp import accounts, calendar_db, contacts, db, guard, textutil

MAX_PEOPLE = 15


def _now() -> datetime:
    return datetime.now(guard.user_timezone())


def _mail_brief(m: dict, mine: set[str], preview: int = 200) -> dict[str, Any]:
    return {
        "message_id": m["message_id"],
        "header_message_id": m["header_message_id"],
        "conversation_id": m["conversation_id"],
        "date": m["date"],
        "direction": "sent" if m["from_me"] else "received",
        "from": m["author"],
        "subject": m["subject"],
        "preview": textutil.strip_quotes(m["body_preview"])[0][:preview],
        **({"suspicious": m["suspicious"]} if m.get("suspicious") else {}),
    }


def _exact_address(messages: list[dict], email: str) -> list[dict]:
    """Drop substring false positives (``ann@x`` vs ``joann@x``)."""
    return [
        m
        for m in messages
        if email in textutil.emails_in(m["author"]) or email in textutil.emails_in(m["recipients"])
    ]


def contact(email: str, threads: int = 5) -> dict[str, Any]:
    """Everything about one correspondent: history, top threads, meetings, address book."""
    email = email.strip().lower()
    msgs: list[dict] = []
    offset: int | None = 0
    while offset is not None and len(msgs) < 1000:
        page = db.recent_by_address(email, limit=db.MAX_LIMIT, offset=offset)
        msgs += page["messages"]
        offset = page["next_offset"]
    msgs = _exact_address(msgs, email)

    received = [m for m in msgs if email in textutil.emails_in(m["author"])]
    sent = [m for m in msgs if m["from_me"] and email in textutil.emails_in(m["recipients"])]
    by_conv: dict[int, list[dict]] = {}
    for m in msgs:
        by_conv.setdefault(m["conversation_id"], []).append(m)
    top = sorted(by_conv.values(), key=lambda ms: max(x["date"] for x in ms), reverse=True)

    name = next((n for m in received if (n := textutil.display_name(m["author"]))), None)
    now = _now()
    return {
        "email": email,
        "name": name,
        "messages": len(msgs),
        "received": len(received),
        "sent": len(sent),
        "first_contact": min((m["date"] for m in msgs), default=None),
        "last_contact": max((m["date"] for m in msgs), default=None),
        "last_received": max((m["date"] for m in received), default=None),
        "last_sent": max((m["date"] for m in sent), default=None),
        "threads": [
            {
                "conversation_id": ms[0]["conversation_id"],
                "subject": max(ms, key=lambda x: x["date"])["subject"],
                "messages": len(ms),
                "last_date": max(x["date"] for x in ms),
            }
            for ms in top[: max(1, min(int(threads), 20))]
        ],
        "upcoming_meetings": [
            {k: e[k] for k in ("event_id", "calendar_id", "title", "start", "end")}
            for e in calendar_db.events_with(
                email,
                since=now.date().isoformat(),
                until=(now + timedelta(days=90)).date().isoformat(),
                limit=10,
            )
        ],
        "address_book": contacts.lookup(email),
    }


def meeting_prep(
    event_id: str, calendar_id: str | None = None, per_person: int = 5, days_back: int = 90
) -> dict[str, Any] | None:
    """Context for a meeting: its details, recent mail with each attendee, related mail."""
    event = calendar_db.get_event(event_id, calendar_id=calendar_id, description_chars=2000)
    if event is None:
        return None
    upcoming = calendar_db.next_occurrence(event_id, event["calendar_id"])
    mine = accounts.my_addresses()
    since = (_now() - timedelta(days=int(days_back))).date()
    per_person = max(1, min(int(per_person), 20))

    people = []
    for p in calendar_db.attendee_emails(event):
        if p["email"] in mine:
            continue
        if len(people) >= MAX_PEOPLE:
            break
        recent = _exact_address(
            db.recent_by_address(p["email"], limit=per_person * 3)["messages"], p["email"]
        )
        recent = [m for m in recent if m["date"] >= since.isoformat()][:per_person]
        card = contacts.lookup(p["email"])
        people.append(
            {
                **p,
                "organization": next((c["organization"] for c in card if c["organization"]), None),
                "last_contact": recent[0]["date"] if recent else None,
                "recent_mail": [_mail_brief(m, mine) for m in recent],
            }
        )

    related = []
    if event.get("title") and event["title"] != "Busy":
        hits = db.search(query=event["title"], since=since.isoformat(), limit=5)["messages"]
        related = [_mail_brief(m, mine) for m in hits]

    return {
        "event": {
            k: event.get(k)
            for k in (
                "event_id",
                "calendar",
                "calendar_id",
                "title",
                "start",
                "end",
                "all_day",
                "timezone",
                "location",
                "url",
                "status",
                "description",
                "description_truncated",
                "organizer",
            )
        },
        "next_occurrence": ({k: upcoming[k] for k in ("start", "end")} if upcoming else None),
        "recurring": bool(event.get("recurrence")),
        "people": people,
        "related_mail": related,
        "you_are": [p for p in calendar_db.attendee_emails(event) if p["email"] in mine],
    }


def daily_brief(date: str | None = None) -> dict[str, Any]:
    """One day at a glance: agenda, free time, new mail from people, follow-ups."""
    user_tz = guard.user_timezone()
    day = datetime.fromisoformat(date).date() if date else _now().date()
    mine = accounts.my_addresses()

    agenda = calendar_db.events(since=day.isoformat(), until=day.isoformat(), limit=50)["events"]
    attendees: dict[str, str] = {}
    for e in agenda:
        if e["attendees"] and e["title"] != "Busy":
            full = calendar_db.get_event(e["event_id"], e["calendar_id"], description_chars=0)
            for p in calendar_db.attendee_emails(full or {}):
                if p["email"] not in mine:
                    attendees.setdefault(p["email"], e["title"])

    start_of_day = datetime.combine(day, time.min, tzinfo=user_tz)
    since = (start_of_day - timedelta(days=1)).isoformat()
    new_mail = db.search(
        since=since, until=(start_of_day + timedelta(days=1)).isoformat(), limit=db.MAX_LIMIT
    )["messages"]
    from_people = [
        m
        for m in new_mail
        if not m["from_me"] and not textutil.is_automated(m["author"], m["mailing_list"])
    ]
    from_attendees = [
        {**_mail_brief(m, mine), "meeting": attendees[a]}
        for m in from_people
        for a in textutil.emails_in(m["author"])[:1]
        if a in attendees
    ]

    free = calendar_db.free_slots(since=day.isoformat(), until=day.isoformat(), weekdays_only=False)
    is_today = day == _now().date()
    return {
        "date": day.isoformat(),
        "timezone": str(user_tz),
        "agenda": [
            {
                k: e[k]
                for k in (
                    "event_id",
                    "calendar_id",
                    "title",
                    "start",
                    "end",
                    "all_day",
                    "location",
                    "attendees",
                )
            }
            for e in agenda
        ],
        "free_slots": free["slots"],
        "conflicts": free["conflicts"],
        "mail_since_yesterday": {
            "total": len(new_mail),
            "from_people": len(from_people),
            "unread_from_people": sum(1 for m in from_people if m["read"] is False),
            "top": [_mail_brief(m, mine) for m in from_people[:10]],
        },
        "mail_from_todays_attendees": from_attendees[:10],
        "awaiting_reply": db.awaiting_reply(days=3, limit=5) if is_today else [],
        "unanswered": db.unanswered(days=1, limit=5) if is_today else [],
        "generated_at": _now().isoformat(timespec="seconds"),
    }
