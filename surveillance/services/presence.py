import secrets
import uuid
from contextlib import contextmanager

from filelock import FileLock
from flask import abort, current_app

from ..extensions import db
from ..models import CaptureSession, PresenceInterval, RoomArrival, RuntimeState, utcnow
from .ledger import audit_source
from .reporting import last_known_room


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


def finish_capture(state, capture, timestamp):
    capture.arrival_candidates = None
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
    # Keep unconfirmed matches only for the next frame in this capture generation.
    previous = capture.arrival_candidates or {}
    gap = (timestamp - capture.last_frame_at).total_seconds()
    max_gap = current_app.config["PRESENCE_GRACE_SECONDS"]
    scores = previous.get("scores", {}) if previous.get("model_id") == model_id else {}
    if not 0 <= gap <= max_gap:
        scores = {}

    candidates = {}
    confirmed = set()
    for user_id, distance in observations.items():
        if last_known_room(user_id) == capture.room:
            confirmed.add(user_id)
            continue
        key = str(user_id)
        if key in scores:
            arrival = RoomArrival(
                user_id=user_id,
                capture_id=capture.id,
                model_id=model_id,
                room=capture.room,
                arrived_at=capture.last_frame_at,
                score=scores[key],
            )
            db.session.add(arrival)
            audit_source(arrival)
            confirmed.add(user_id)
        else:
            candidates[key] = distance

    capture.arrival_candidates = (
        {"model_id": model_id, "scores": candidates} if candidates else None
    )
    capture.last_frame_at = timestamp
    return confirmed


def stop_user_capture(user_id):
    with locked_state() as state:
        capture = expire_capture(state, now())
        if capture and capture.user_id == user_id:
            finish_capture(state, capture, now())


def reap_capture():
    with locked_state() as state:
        expire_capture(state, now())


def recover_capture_on_server_start():
    """Release a previous process's lease and finalise any pre-upgrade intervals."""
    with locked_state() as state:
        capture = (
            db.session.get(CaptureSession, state.active_capture_id)
            if (state.active_capture_id)
            else None
        )
        if capture:
            intervals = db.session.scalars(
                db.select(PresenceInterval).where(
                    PresenceInterval.capture_id == capture.id, PresenceInterval.ended_at.is_(None)
                )
            ).all()
            for interval in intervals:
                interval.ended_at = interval.last_seen
                audit_source(interval)
            finish_capture(state, capture, now())
