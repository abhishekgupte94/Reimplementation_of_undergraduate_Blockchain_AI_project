"""Weapon observations are independent of face identities."""

from .extensions import db
from .models import UTC_DATETIME, utcnow


class WeaponAlert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    capture_id = db.Column(db.String(36), db.ForeignKey("capture_session.id"), nullable=False)
    generation = db.Column(db.Integer, nullable=False)
    frame_sequence = db.Column(db.Integer, nullable=False)
    room = db.Column(db.Integer, nullable=False)
    label = db.Column(db.String(32), nullable=False)
    confidence = db.Column(db.Float, nullable=False)
    box = db.Column(db.JSON, nullable=False)
    model_tag = db.Column(db.String(80), nullable=False)
    observed_at = db.Column(UTC_DATETIME, nullable=False, index=True)
    created_at = db.Column(UTC_DATETIME, nullable=False, default=utcnow)
    acknowledged_at = db.Column(UTC_DATETIME)


def weapon_snapshot(alert):
    return {
        "id": alert.id,
        "capture_id": alert.capture_id,
        "generation": alert.generation,
        "frame_sequence": alert.frame_sequence,
        "room": alert.room,
        "label": alert.label,
        "confidence": alert.confidence,
        "box": alert.box,
        "model_tag": alert.model_tag,
        "observed_at": alert.observed_at.isoformat(timespec="microseconds") + "Z",
    }
