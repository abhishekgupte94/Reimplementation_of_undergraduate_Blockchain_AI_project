import secrets
import uuid
from contextlib import contextmanager

from filelock import FileLock
from flask import abort, current_app

from ..extensions import db
from ..models import CaptureSession, PresenceInterval, RecognitionEvent, RuntimeState, utcnow
from .ledger import audit_source


def now():
    clock = current_app.config.get("CLOCK")
    return clock() if clock else utcnow()


def capture_state_lock(timeout=30):
    # A single-host demo uses an interprocess lock, including SQLite where
    # SELECT FOR UPDATE cannot serialize a webcam lease.
    path = current_app.config["PRIVATE_DIR"] / "locks" / "state.lock"
    return FileLock(str(path), timeout=timeout)


@contextmanager
def locked_state():
    with capture_state_lock():
        db.session.rollback()  # begin a fresh read after acquiring the lock
        state = db.session.get(RuntimeState, 1)
        if state is None:
            state = RuntimeState(id=1)
            db.session.add(state)
            db.session.flush()
        try:
            yield state
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise


def intervals_for(capture_id):
    return list(
        db.session.scalars(
            db.select(PresenceInterval).where(
                PresenceInterval.capture_id == capture_id, PresenceInterval.ended_at.is_(None)
            )
        )
    )


def close_intervals(capture):
    for interval in intervals_for(capture.id):
        interval.ended_at = interval.last_seen
        audit_source(interval)


def finish_capture(state, capture, timestamp):
    close_intervals(capture)
    capture.ended_at = timestamp
    state.active_capture_id = None


def expire_capture(state, timestamp):
    capture = (
        db.session.get(CaptureSession, state.active_capture_id)
        if (state.active_capture_id)
        else None
    )
    if capture:
        if (timestamp - capture.last_frame_at).total_seconds() > current_app.config[
            "CAPTURE_TIMEOUT_SECONDS"
        ]:
            finish_capture(state, capture, timestamp)
            return None
        for interval in intervals_for(capture.id):
            if (timestamp - interval.last_seen).total_seconds() > current_app.config[
                "PRESENCE_GRACE_SECONDS"
            ]:
                interval.ended_at = interval.last_seen
                audit_source(interval)
    return capture


def start_capture(user_id, room):
    with locked_state() as state:
        timestamp = now()
        existing = expire_capture(state, timestamp)
        if existing:
            abort(409, "The webcam is active in another page. Stop it there or wait five seconds.")
        capture = CaptureSession(
            id=str(uuid.uuid4()),
            user_id=user_id,
            token=secrets.token_hex(32),
            room=room,
            started_at=timestamp,
            last_frame_at=timestamp,
        )
        db.session.add(capture)
        db.session.flush()
        state.active_capture_id = capture.id
        return capture_payload(capture)


def capture_payload(capture):
    return {
        "capture_id": capture.id,
        "token": capture.token,
        "room": capture.room,
        "generation": capture.generation,
    }


def require_capture(state, user_id, data):
    capture = expire_capture(state, now())
    if not capture:
        abort(409, "Camera session expired. Start the webcam again.")
    token = data.get("token", "")
    if (
        capture.id != data.get("capture_id")
        or capture.user_id != user_id
        or not isinstance(token, str)
        or not secrets.compare_digest(capture.token, token)
    ):
        abort(403, "This camera session belongs to another page.")
    if str(capture.generation) != str(data.get("generation")):
        abort(409, "Stale room generation; this frame was discarded.")
    return capture


def record_observations(capture, model_id, observations, timestamp):
    active = {interval.user_id: interval for interval in intervals_for(capture.id)}
    grace = current_app.config["PRESENCE_GRACE_SECONDS"]
    for user_id, interval in active.items():
        if (timestamp - interval.last_seen).total_seconds() > grace:
            interval.ended_at = interval.last_seen
            audit_source(interval)
        elif user_id not in observations:
            interval.consecutive = 0
    for user_id, distance in observations.items():
        event = RecognitionEvent(
            user_id=user_id,
            capture_id=capture.id,
            model_id=model_id,
            room=capture.room,
            observed_at=timestamp,
            score=distance,
        )
        db.session.add(event)
        interval = active.get(user_id)
        if interval is None or interval.ended_at is not None:
            interval = PresenceInterval(
                user_id=user_id,
                capture_id=capture.id,
                room=capture.room,
                first_seen=timestamp,
                last_seen=timestamp,
                consecutive=1,
                confirmed=False,
            )
            db.session.add(interval)
        else:
            if not interval.confirmed and interval.consecutive == 0:
                interval.first_seen = timestamp
            interval.consecutive += 1
            interval.last_seen = timestamp
            if interval.consecutive >= 2:
                interval.confirmed = True
        audit_source(event)
        audit_source(interval)
    for user_id, interval in active.items():
        if user_id not in observations:
            audit_source(interval)


def stop_user_capture(user_id):
    with locked_state() as state:
        capture = expire_capture(state, now())
        if capture and capture.user_id == user_id:
            finish_capture(state, capture, now())


def reap_capture():
    with locked_state() as state:
        expire_capture(state, now())


def recover_capture_on_server_start():
    """The first web request closes a previous process's lease at its last sighting."""
    with locked_state() as state:
        capture = (
            db.session.get(CaptureSession, state.active_capture_id)
            if (state.active_capture_id)
            else None
        )
        if capture:
            finish_capture(state, capture, now())
