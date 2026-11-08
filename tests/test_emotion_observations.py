from conftest import send_frame
from test_ledger import FakeRPC

from surveillance.extensions import db
from surveillance.models import (
    AuditEntry,
    EmotionObservation,
    LedgerAnchor,
    RoomArrival,
    RuntimeState,
)
from surveillance.services import ledger
from surveillance.services.emotion_observations import latest_emotion, persist_emotions
from surveillance.services.presence import capture_state_lock


def capture_sample(trained, app, clock):
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    with app.app_context():
        model_id = db.session.get(RuntimeState, 1).active_model_id
    assert send_frame(trained, capture).status_code == 200
    return capture, {
        "capture_id": capture["capture_id"],
        "generation": capture["generation"],
        "room": capture["room"],
        "frame_sequence": 0,
        "observed_at": clock(),
        "model_id": model_id,
    }


def estimate(emotion):
    return {1: {"emotion": emotion, "confidence": 0.9012345, "model_version": "test-emotion-v1"}}


def confirm_sample(trained, capture, sample, clock, sequence):
    clock.advance(0.5)
    assert send_frame(trained, capture, sequence=sequence).status_code == 200
    return {**sample, "frame_sequence": sequence, "observed_at": clock()}


def test_emotion_changes_are_audited_without_new_arrivals(trained, app, clock, monkeypatch):
    chain = FakeRPC()
    monkeypatch.setattr(ledger, "rpc", chain)
    capture, sample = capture_sample(trained, app, clock)
    with app.app_context():
        assert persist_emotions(sample, estimate("happy")) == []
    first = confirm_sample(trained, capture, sample, clock, sequence=1)
    with app.app_context():
        (first_id,) = persist_emotions(first, estimate("happy"))
        assert persist_emotions(first, estimate("sad")) == []
    repeated = confirm_sample(trained, capture, sample, clock, sequence=2)
    with app.app_context():
        assert persist_emotions(repeated, estimate("happy")) == []
    change = confirm_sample(trained, capture, sample, clock, sequence=3)
    with app.app_context():
        (change_id,) = persist_emotions(change, estimate("sad"))
        assert persist_emotions(repeated, estimate("surprised")) == []
        assert db.session.query(RoomArrival).count() == 1
        assert db.session.query(EmotionObservation).count() == 2
        assert db.session.query(LedgerAnchor).count() == 3
        first_row = db.session.get(EmotionObservation, first_id)
        assert first_row.emotion == "happy" and first_row.observed_at == first["observed_at"]
        latest = latest_emotion(1, room=1, day="2026-11-04")
        assert latest.id == change_id and latest.observed_at == change["observed_at"]
        assert latest_emotion(1, room=2) is None
        assert latest_emotion(1, day="2026-11-03") is None
        entry = db.session.scalar(
            db.select(AuditEntry).where(
                AuditEntry.source_type == "emotion_observation",
                AuditEntry.source_id == str(change_id),
            )
        )
        assert entry.payload["record"]["emotion"] == "sad"
        db.session.rollback()
        assert ledger.process_anchor() and ledger.process_anchor() and ledger.process_anchor()
        assert ledger.verify_audit(check_chain=True)["ok"]
        assert all(
            len(bytes.fromhex(tx["input"][2:])) == len(ledger.PREFIX) + 32
            for tx in chain.transactions.values()
        )


def test_stale_emotion_samples_and_busy_writers_are_skipped(trained, app, clock):
    capture, sample = capture_sample(trained, app, clock)
    first = confirm_sample(trained, capture, sample, clock, sequence=1)
    with app.app_context():
        assert persist_emotions({**first, "model_id": 999}, estimate("happy")) == []
        assert persist_emotions({**first, "frame_sequence": 2}, estimate("happy")) == []
        with capture_state_lock():
            assert persist_emotions(first, estimate("happy")) == []
        with db.engine.connect() as writer:
            writer.exec_driver_sql("BEGIN IMMEDIATE")
            assert persist_emotions(first, estimate("happy")) == []
        assert persist_emotions(first, estimate("happy"))

    old_room = confirm_sample(trained, capture, sample, clock, sequence=2)
    switched = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
    with app.app_context():
        assert persist_emotions(old_room, estimate("angry")) == []
    second = {
        **first,
        "generation": switched["generation"],
        "room": 2,
    }
    assert send_frame(trained, switched).status_code == 200
    second = confirm_sample(trained, switched, second, clock, sequence=1)
    with app.app_context():
        assert persist_emotions(second, estimate("happy"))
    later = confirm_sample(trained, switched, second, clock, sequence=2)
    clock.advance(4)
    with app.app_context():
        assert persist_emotions(later, estimate("sad")) == []
        assert latest_emotion(1).room == 2
        assert db.session.query(EmotionObservation).count() == 2
