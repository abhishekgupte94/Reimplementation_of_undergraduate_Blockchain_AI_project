from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app

from ..extensions import db
from ..models import PresenceInterval, RoomArrival


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


def _legacy_visits(user_id):
    return db.select(PresenceInterval.room, PresenceInterval.first_seen).where(
        PresenceInterval.user_id == user_id, PresenceInterval.confirmed.is_(True)
    )


def _latest_legacy_room(visits):
    return db.session.scalar(
        visits.order_by(PresenceInterval.first_seen.desc(), PresenceInterval.id.desc()).limit(1)
    )


def last_known_room(user_id):
    room = db.session.scalar(
        db.select(RoomArrival.room)
        .where(RoomArrival.user_id == user_id)
        .order_by(RoomArrival.id.desc())
        .limit(1)
    )
    return room if room is not None else _latest_legacy_room(_legacy_visits(user_id))


def _legacy_arrivals(user_id, window):
    visits = _legacy_visits(user_id)
    previous_room = None
    if window:
        start, end = window
        previous_room = _latest_legacy_room(visits.where(PresenceInterval.first_seen < start))
        visits = visits.where(
            PresenceInterval.first_seen >= start, PresenceInterval.first_seen < end
        )
    for room, at in db.session.execute(
        visits.order_by(PresenceInterval.first_seen, PresenceInterval.id)
    ):
        # Legacy gaps could reopen a visit in the same room. Keep only transitions.
        if room != previous_room:
            yield {"room": room, "at": at}
            previous_room = room


def room_report(user_id, day=None):
    window = day_window(day) if day is not None else None
    query = db.select(RoomArrival.room, RoomArrival.arrived_at).where(
        RoomArrival.user_id == user_id
    )
    if window:
        start, end = window
        query = query.where(RoomArrival.arrived_at >= start, RoomArrival.arrived_at < end)
    arrivals = list(_legacy_arrivals(user_id, window))
    arrivals.extend(
        {"room": room, "at": at}
        for room, at in db.session.execute(query.order_by(RoomArrival.arrived_at, RoomArrival.id))
    )
    arrivals.sort(key=lambda arrival: arrival["at"])
    totals = {1: 0, 2: 0}
    for arrival in arrivals:
        totals[arrival["room"]] += 1
    total = sum(totals.values())
    rooms = [
        {
            "room": room,
            "count": totals[room],
            "percent": round(totals[room] / total * 100, 2) if total else 0,
        }
        for room in (1, 2)
    ]
    return {
        "day": day,
        "rooms": rooms,
        "total_arrivals": total,
        "arrivals": arrivals,
        "last_arrival": arrivals[-1] if arrivals else None,
        "current_room": last_known_room(user_id),
    }


def public_report(report):
    def serialize(arrival):
        return {**arrival, "at": arrival["at"].replace(tzinfo=timezone.utc).isoformat()}

    last = report["last_arrival"]
    return {
        **report,
        "arrivals": [serialize(arrival) for arrival in report["arrivals"]],
        "last_arrival": serialize(last) if last else None,
    }
