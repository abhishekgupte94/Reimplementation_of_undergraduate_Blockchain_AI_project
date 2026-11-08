from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app

from ..extensions import db
from ..models import PresenceInterval, RecognitionEvent


def day_window(day):
    selected = date.fromisoformat(day)
    zone = ZoneInfo(current_app.config["DISPLAY_TIMEZONE"])
    start = datetime.combine(selected, time.min, zone)
    end = datetime.combine(selected + timedelta(days=1), time.min, zone)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def selected_day():
    return datetime.now(ZoneInfo(current_app.config["DISPLAY_TIMEZONE"])).date().isoformat()


def room_report(user_id, day):
    start, end = day_window(day)
    intervals = list(
        db.session.scalars(
            db.select(PresenceInterval)
            .where(
                PresenceInterval.user_id == user_id,
                PresenceInterval.confirmed.is_(True),
                PresenceInterval.first_seen < end,
                PresenceInterval.last_seen > start,
            )
            .order_by(PresenceInterval.first_seen)
        )
    )
    totals = {1: 0.0, 2: 0.0}
    visits = []
    for interval in intervals:
        first, last = max(start, interval.first_seen), min(end, interval.last_seen)
        seconds = max(0.0, (last - first).total_seconds())
        totals[interval.room] += seconds
        visits.append(
            {
                "room": interval.room,
                "first_seen": first,
                "last_seen": last,
                "seconds": seconds,
                "ongoing": interval.ended_at is None,
            }
        )
    total = sum(totals.values())
    rooms = [
        {
            "room": room,
            "seconds": round(totals[room], 3),
            "percent": round(totals[room] / total * 100, 2) if total else 0,
        }
        for room in (1, 2)
    ]
    last_seen = db.session.scalar(
        db.select(RecognitionEvent)
        .where(
            RecognitionEvent.user_id == user_id,
            RecognitionEvent.observed_at >= start,
            RecognitionEvent.observed_at < end,
        )
        .order_by(RecognitionEvent.observed_at.desc(), RecognitionEvent.id.desc())
    )
    return {
        "day": day,
        "rooms": rooms,
        "total_seconds": round(total, 3),
        "visits": visits,
        "last_seen": last_seen,
    }


def public_report(report):
    def iso(value):
        return value.replace(tzinfo=timezone.utc).isoformat()

    last = report["last_seen"]
    return {
        **report,
        "visits": [
            {**v, "first_seen": iso(v["first_seen"]), "last_seen": iso(v["last_seen"])}
            for v in report["visits"]
        ],
        "last_seen": {"room": last.room, "at": iso(last.observed_at)} if last else None,
    }
