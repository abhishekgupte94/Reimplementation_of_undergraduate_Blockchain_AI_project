"""Store and query the last recorded facial-expression estimates."""

from datetime import timedelta

from filelock import Timeout
from flask import current_app
from sqlalchemy.exc import OperationalError

from ..extensions import db
from ..models import CaptureSession, EmotionObservation, RuntimeState
from . import presence
from .ledger import audit_source, audit_write_is_busy, reserve_audit_write
from .reporting import day_window, last_known_room

EMOTIONS = frozenset({"neutral", "happy", "sad", "angry", "surprised"})


def latest_emotion(user_id, *, room=None, day=None):
    query = db.select(EmotionObservation).where(EmotionObservation.user_id == user_id)
    if room is not None:
        query = query.where(EmotionObservation.room == room)
    if day is not None:
        start, end = day_window(day)
        query = query.where(
            EmotionObservation.observed_at >= start, EmotionObservation.observed_at < end
        )
    return db.session.scalar(
        query.order_by(EmotionObservation.observed_at.desc(), EmotionObservation.id.desc()).limit(1)
    )


def _sample_is_current(metadata, timestamp):
    timeout = timedelta(seconds=current_app.config["CAPTURE_TIMEOUT_SECONDS"])
    return (
        db.session.scalar(
            db.select(CaptureSession.id)
            .join(RuntimeState, RuntimeState.active_capture_id == CaptureSession.id)
            .where(
                RuntimeState.id == 1,
                RuntimeState.active_model_id == metadata["model_id"],
                CaptureSession.id == metadata["capture_id"],
                CaptureSession.generation == metadata["generation"],
                CaptureSession.room == metadata["room"],
                CaptureSession.ended_at.is_(None),
                CaptureSession.last_sequence >= metadata["frame_sequence"],
                CaptureSession.started_at <= metadata["observed_at"],
                CaptureSession.last_frame_at >= metadata["observed_at"],
                CaptureSession.last_frame_at >= timestamp - timeout,
            )
        )
        is not None
    )


def _store_changes(metadata, estimates):
    observation_ids = []
    for user_id, estimate in estimates.items():
        emotion = estimate["emotion"]
        confidence = float(estimate["confidence"])
        model_version = estimate["model_version"]
        if emotion not in EMOTIONS or not 0 <= confidence <= 1 or not 0 < len(model_version) <= 96:
            continue
        if last_known_room(user_id) != metadata["room"]:
            continue
        previous = db.session.scalar(
            db.select(EmotionObservation)
            .where(
                EmotionObservation.capture_id == metadata["capture_id"],
                EmotionObservation.generation == metadata["generation"],
                EmotionObservation.user_id == user_id,
            )
            .order_by(EmotionObservation.frame_sequence.desc())
            .limit(1)
        )
        if previous and (
            previous.frame_sequence >= metadata["frame_sequence"]
            or (previous.emotion == emotion and previous.model_version == model_version)
        ):
            continue
        observation = EmotionObservation(
            **metadata,
            user_id=user_id,
            emotion=emotion,
            confidence=confidence,
            model_version=model_version,
        )
        db.session.add(observation)
        audit_source(observation)
        observation_ids.append(observation.id)
    return observation_ids


def persist_emotions(metadata, estimates):
    """Commit changed estimates and hash outbox entries for an accepted camera sample."""
    if not estimates or metadata["frame_sequence"] < 0:
        return []
    try:
        with presence.capture_state_lock(timeout=0), db.session.begin():
            timestamp = presence.now()
            maximum_age = current_app.config.get("EMOTION_RESULT_MAX_AGE_SECONDS", 3)
            if not 0 <= (timestamp - metadata["observed_at"]).total_seconds() <= maximum_age:
                return []
            reserve_audit_write()
            if not _sample_is_current(metadata, timestamp):
                return []
            return _store_changes(metadata, estimates)
    except Timeout:
        return []
    except OperationalError as error:
        if audit_write_is_busy(error):
            return []
        raise
