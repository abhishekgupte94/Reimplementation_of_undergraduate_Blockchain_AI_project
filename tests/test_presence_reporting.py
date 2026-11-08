from datetime import datetime

import pytest
from conftest import login, send_frame, upload

from surveillance.extensions import db
from surveillance.models import CaptureSession, PresenceInterval, RecognitionEvent
from surveillance.services.presence import reap_capture
from surveillance.services.reporting import day_window


def start(client, room=1):
    response = client.post("/api/camera/start", json={"room": room})
    assert response.status_code == 200, response.json
    return response.json


def test_confirmed_presence_short_gap_absence_and_switch(trained, app, clock):
    capture = start(trained)
    assert send_frame(trained, capture).status_code == 200
    clock.advance(0.5)
    assert send_frame(trained, capture, sequence=1).status_code == 200
    clock.advance(0.5)
    send_frame(trained, capture, marker=0, sequence=2)
    clock.advance(1)
    send_frame(trained, capture, sequence=3)
    clock.advance(3.1)
    send_frame(trained, capture, marker=0, sequence=4)
    with app.app_context():
        interval = db.session.scalar(db.select(PresenceInterval))
        assert interval.confirmed and interval.ended_at == interval.last_seen
        assert (interval.last_seen - interval.first_seen).total_seconds() == 2
    # Reacquire presence; switching closes this interval and resets confirmation.
    clock.advance(0.5)
    send_frame(trained, capture, sequence=5)
    clock.advance(0.5)
    send_frame(trained, capture, sequence=6)
    switched = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
    assert send_frame(trained, capture, sequence=7).status_code == 409
    clock.advance(0.5)
    send_frame(trained, switched, sequence=0)
    clock.advance(1)
    send_frame(trained, switched, sequence=1)
    trained.post("/api/camera/stop", json=switched)
    login(trained, "admin")
    report = trained.get("/api/admin/report?user_id=1&day=2026-11-04").json
    assert [room["seconds"] for room in report["rooms"]] == [2.5, 1]
    assert report["total_seconds"] == 3.5
    chat = trained.post(
        "/api/admin/chat", json={"user_id": 1, "day": "2026-11-04", "question": "time in each room"}
    ).json
    assert chat["report"] == report
    assert "Room 1: 2.5s" in chat["answer"]
    for question, expected in (
        ("When was the participant LAST  SEEN?", "Alice was last seen in Room 2"),
        ("Show visits", "Alice has 3 confirmed visits"),
        ("Compare the rooms", "Room 2: 1.0s"),
        ("What time is it?", "I can answer questions about the selected participant and date."),
    ):
        response = trained.post(
            "/api/admin/chat", json={"user_id": 1, "day": "2026-11-04", "question": question}
        )
        assert response.status_code == 200
        assert expected in response.json["answer"]
        assert response.json["report"] == report


def test_unconfirmed_candidate_needs_consecutive_frames(trained, app, clock):
    capture = start(trained)
    send_frame(trained, capture)
    clock.advance(0.5)
    send_frame(trained, capture, marker=0, sequence=1)
    clock.advance(0.5)
    send_frame(trained, capture, sequence=2)
    with app.app_context():
        assert not db.session.scalar(db.select(PresenceInterval)).confirmed
    clock.advance(0.5)
    send_frame(trained, capture, sequence=3)
    with app.app_context():
        interval = db.session.scalar(db.select(PresenceInterval))
        assert interval.confirmed
        assert (interval.last_seen - interval.first_seen).total_seconds() == 0.5


def test_duplicate_out_of_order_and_lease_exclusion(trained, app, clock):
    capture = start(trained)
    assert trained.post("/api/camera/start", json={"room": 2}).status_code == 409
    assert send_frame(trained, capture, sequence=2).status_code == 200
    assert send_frame(trained, capture, sequence=2).status_code == 409
    assert send_frame(trained, capture, sequence=1).status_code == 409
    other = app.test_client()
    login(other, "bob")
    assert send_frame(other, capture, sequence=3).status_code == 403
    assert trained.post("/api/camera/stop", json={**capture, "token": "bad"}).status_code == 403
    clock.advance(6)
    assert send_frame(trained, capture, sequence=3).status_code == 409
    # A subsequent start reaps and commits expiration even after an expired-frame error.
    new_capture = start(trained, 2)
    assert new_capture["capture_id"] != capture["capture_id"]


def test_disconnect_and_restart_do_not_extrapolate(trained, app, clock):
    capture = start(trained)
    send_frame(trained, capture)
    clock.advance(1)
    send_frame(trained, capture, sequence=1)
    clock.advance(60)
    with app.app_context():
        reap_capture()
        interval = db.session.scalar(db.select(PresenceInterval))
        assert (interval.ended_at - interval.first_seen).total_seconds() == 1
        assert db.session.get(CaptureSession, capture["capture_id"]).ended_at is not None
    login(trained, "admin")
    report = trained.get("/api/admin/report?user_id=1&day=2026-11-04").json
    assert report["total_seconds"] == 1


def test_logout_closes_capture(trained, app):
    capture = start(trained)
    trained.post("/logout")
    with app.app_context():
        assert db.session.get(CaptureSession, capture["capture_id"]).ended_at is not None


def test_quick_server_restart_closes_previous_lease(trained, app, clock):
    from surveillance import create_app

    capture = start(trained)
    send_frame(trained, capture)
    clock.advance(0.5)
    send_frame(trained, capture, sequence=1)
    clock.advance(0.5)
    restarted = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-secret",
            "SQLALCHEMY_DATABASE_URI": app.config["SQLALCHEMY_DATABASE_URI"],
            "PRIVATE_DIR": app.config["PRIVATE_DIR"],
            "CLOCK": clock,
        }
    )
    assert restarted.test_client().get("/login").status_code == 200
    with restarted.app_context():
        interval = db.session.scalar(db.select(PresenceInterval))
        assert interval.ended_at == interval.last_seen
        assert (interval.ended_at - interval.first_seen).total_seconds() == 0.5
        assert db.session.get(CaptureSession, capture["capture_id"]).ended_at is not None
        db.session.remove()
        db.engine.dispose()


def test_camera_frame_rate_limit_and_invalid_json(trained, app):
    assert trained.post("/api/camera/start", json=[1, 2]).status_code == 400
    capture = start(trained)
    app.config["MIN_FRAME_SECONDS"] = 0.3
    assert send_frame(trained, capture).status_code == 200
    assert send_frame(trained, capture, sequence=1).status_code == 429


def test_unknown_face_creates_no_identified_events(trained, app):
    capture = start(trained)
    response = send_frame(trained, capture, marker=180)
    assert response.json["faces"][0]["name"] == "Unknown"
    with app.app_context():
        assert db.session.query(RecognitionEvent).count() == 0


def test_identity_is_from_recognition_not_login(trained, app, clock):
    trained.post("/logout")
    login(trained, "bob")
    upload(trained, (180, 181, 182))
    with app.app_context():
        from surveillance.services.training import process_next_job

        process_next_job()
    capture = start(trained)
    send_frame(trained, capture, marker=30)
    clock.advance(0.5)
    send_frame(trained, capture, marker=30, sequence=1)
    with app.app_context():
        events = list(db.session.scalars(db.select(RecognitionEvent)))
        assert all(event.user_id == 1 for event in events)


def test_daily_clipping_and_dublin_dst(app):
    with app.app_context():
        start_time, end_time = day_window("2026-10-25")
        assert (end_time - start_time).total_seconds() == 25 * 3600
        start_time, end_time = day_window("2026-03-29")
        assert (end_time - start_time).total_seconds() == 23 * 3600
        assert day_window("2026-11-04")[0] == datetime(2026, 11, 4)


def test_report_clips_cross_midnight_visit(trained, app, clock):
    clock.value = datetime(2026, 11, 3, 23, 59, 59)
    capture = start(trained)
    send_frame(trained, capture)
    clock.advance(2)
    send_frame(trained, capture, sequence=1)
    trained.post("/api/camera/stop", json=capture)
    login(trained, "admin")
    for day in ("2026-11-03", "2026-11-04"):
        report = trained.get(f"/api/admin/report?user_id=1&day={day}").json
        assert report["total_seconds"] == 1


def test_admin_empty_unsupported_and_invalid_inputs(client):
    login(client, "admin")
    assert "No observed room time" in client.get("/admin/analytics").text
    assert client.get("/api/admin/report?day=invalid").status_code == 400
    assert client.get("/api/admin/report?user_id=bad").status_code == 400
    assert client.get("/api/admin/report?user_id=999").status_code == 404
    reply = client.post("/api/admin/chat", json={"question": "write a poem"}).json
    assert "time in each room" in reply["answer"]
    reply = client.post("/api/admin/chat", json={"question": "last seen"}).json
    assert "No sightings" in reply["answer"]


@pytest.mark.parametrize("room", [None, 0, 3, True, "bad"])
def test_invalid_room(trained, room):
    assert trained.post("/api/camera/start", json={"room": room}).status_code == 400


def test_repeated_face_is_reused_only_in_current_room(trained, app, clock, monkeypatch):
    engine = app.extensions["face_pipeline"]
    embedding = engine.embedding
    calls = []

    def count_embeddings(image, box):
        calls.append(box)
        return embedding(image, box)

    monkeypatch.setattr(engine, "embedding", count_embeddings)
    capture = start(trained)
    first = send_frame(trained, capture)
    clock.advance(0.5)
    repeated = send_frame(trained, capture, sequence=1)
    assert repeated.json["faces"] == first.json["faces"]
    assert first.json["faces"][0]["box"] == [0, 0, 80, 80]
    assert len(calls) == 1

    clock.advance(0.5)
    replacement = send_frame(trained, capture, marker=180, sequence=2)
    assert replacement.json["faces"][0]["user_id"] is None
    assert len(calls) == 2
    clock.advance(0.5)
    assert send_frame(trained, capture, sequence=3).status_code == 200
    assert len(calls) == 3

    switched = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
    clock.advance(0.5)
    assert send_frame(trained, switched).json["faces"] == first.json["faces"]
    assert len(calls) == 4
    with app.app_context():
        rooms = db.session.scalars(db.select(RecognitionEvent.room).order_by(RecognitionEvent.id))
        assert list(rooms) == [1, 1, 1, 2]
