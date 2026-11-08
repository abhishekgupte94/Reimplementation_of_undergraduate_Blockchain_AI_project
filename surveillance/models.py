from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import event
from sqlalchemy.dialects.mysql import DATETIME
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db

UTC_DATETIME = db.DateTime().with_variant(DATETIME(fsp=6), "mysql")


def utcnow():
    """Naive UTC at the persistence boundary, portable across MySQL and SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="participant")
    created_at = db.Column(UTC_DATETIME, nullable=False, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class EnrollmentImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    filename = db.Column(db.String(80), nullable=False)
    digest = db.Column(db.String(64), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(UTC_DATETIME, nullable=False, default=utcnow)
    __table_args__ = (db.UniqueConstraint("user_id", "digest"),)


class TrainingJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="queued")
    message = db.Column(db.String(500), nullable=False, default="Waiting for the trainer.")
    created_at = db.Column(UTC_DATETIME, nullable=False, default=utcnow)
    started_at = db.Column(UTC_DATETIME)
    finished_at = db.Column(UTC_DATETIME)


class ModelVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(100), nullable=False, unique=True)
    mode = db.Column(db.String(20), nullable=False)
    user_ids = db.Column(db.JSON, nullable=False)
    image_ids = db.Column(db.JSON, nullable=False)
    created_at = db.Column(UTC_DATETIME, nullable=False, default=utcnow)


class CaptureSession(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    token = db.Column(db.String(64), nullable=False)
    room = db.Column(db.Integer, nullable=False)
    generation = db.Column(db.Integer, nullable=False, default=1)
    last_sequence = db.Column(db.Integer, nullable=False, default=-1)
    started_at = db.Column(UTC_DATETIME, nullable=False, default=utcnow)
    last_frame_at = db.Column(UTC_DATETIME, nullable=False, default=utcnow)
    ended_at = db.Column(UTC_DATETIME)


class RuntimeState(db.Model):
    """Singleton row; file locks also serialize local SQLite/MySQL mutations."""

    id = db.Column(db.Integer, primary_key=True)
    active_model_id = db.Column(db.Integer, db.ForeignKey("model_version.id"))
    active_capture_id = db.Column(db.String(36), db.ForeignKey("capture_session.id"))


class RecognitionEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    capture_id = db.Column(db.String(36), db.ForeignKey("capture_session.id"), nullable=False)
    model_id = db.Column(db.Integer, db.ForeignKey("model_version.id"), nullable=False)
    room = db.Column(db.Integer, nullable=False)
    observed_at = db.Column(UTC_DATETIME, nullable=False, index=True)
    score = db.Column(db.Float, nullable=False)  # L2 distance, smaller is closer
    emotion = db.Column(db.String(32), nullable=True)


class PresenceInterval(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    capture_id = db.Column(db.String(36), db.ForeignKey("capture_session.id"), nullable=False)
    room = db.Column(db.Integer, nullable=False)
    first_seen = db.Column(UTC_DATETIME, nullable=False)
    last_seen = db.Column(UTC_DATETIME, nullable=False)
    ended_at = db.Column(UTC_DATETIME)
    confirmed = db.Column(db.Boolean, nullable=False, default=False)
    consecutive = db.Column(db.Integer, nullable=False, default=1)


class LedgerState(db.Model):
    """Transactionally locked sequence/head and pinned local blockchain identity."""

    id = db.Column(db.Integer, primary_key=True)
    sequence = db.Column(db.Integer, nullable=False, default=0)
    head_hash = db.Column(db.String(64), nullable=False, default="0" * 64)
    chain_id = db.Column(db.String(32))
    genesis_hash = db.Column(db.String(66))


class AuditEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    kind = db.Column(db.String(64), nullable=False)
    source_type = db.Column(db.String(64), index=True)
    source_id = db.Column(db.String(64))
    payload = db.Column(db.JSON, nullable=False)
    previous_hash = db.Column(db.String(64), nullable=False)
    entry_hash = db.Column(db.String(64), nullable=False, unique=True)
    created_at = db.Column(UTC_DATETIME, nullable=False, default=utcnow)


class LedgerAnchor(db.Model):
    """Transactional outbox: network transport fields are deliberately mutable."""

    entry_id = db.Column(db.Integer, db.ForeignKey("audit_entry.id"), primary_key=True)
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    transaction_hash = db.Column(db.String(66))
    block_number = db.Column(db.Integer)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(UTC_DATETIME, nullable=False, default=utcnow)
    last_error = db.Column(db.String(500))
    anchored_at = db.Column(UTC_DATETIME)


@event.listens_for(AuditEntry, "before_update")
@event.listens_for(AuditEntry, "before_delete")
def immutable_audit_entry(mapper, connection, target):
    raise ValueError("Audit entries are append-only; updates and deletes are prohibited.")
