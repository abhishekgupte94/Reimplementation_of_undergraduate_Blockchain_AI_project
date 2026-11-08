from datetime import datetime, timedelta

import pytest
from conftest import login, send_frame, upload

from surveillance.extensions import db
from surveillance.models import CaptureSession, PresenceInterval, RecognitionEvent, RoomArrival
from surveillance.services.presence import reap_capture
from surveillance.services.reporting import day_window, room_report


def start(client, room=1):
    response = client.post("/api/camera/start", json={"room": room})
    assert response.status_code == 200, response.json
    return response.json


def test_arrivals_only_count_confirmed_room_changes(trained, app, clock):
    capture = start(trained)
    first_arrival = clock()
    assert send_frame(trained, capture).status_code == 200
    clock.advance(0.5)
    assert send_frame(trained, capture, sequence=1).status_code == 200
    clock.advance(0.5)
    send_frame(trained, capture, marker=0, sequence=2)
    clock.advance(1)
    send_frame(trained, capture, sequence=3)
    clock.advance(3.1)
    send_frame(trained, capture, marker=0, sequence=4)
    clock.advance(0.5)
    send_frame(trained, capture, sequence=5)
    clock.advance(0.5)
    send_frame(trained, capture, sequence=6)
    switched = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
    assert send_frame(trained, capture, sequence=7).status_code == 409
    with app.app_context():
        assert room_report(1)["current_room"] == 1
    clock.advance(0.5)
    second_arrival = clock()
    send_frame(trained, switched, sequence=0)
    with app.app_context():
        assert room_report(1)["current_room"] == 1
    clock.advance(1)
    send_frame(trained, switched, sequence=1)
    with app.app_context():
        assert room_report(1)["current_room"] == 2
    returned = trained.post("/api/camera/switch", json={**switched, "room": 1}).json
    clock.advance(0.5)
    third_arrival = clock()
    send_frame(trained, returned)
    clock.advance(0.5)
    send_frame(trained, returned, sequence=1)
    trained.post("/api/camera/stop", json=returned)
    with app.app_context():
        arrivals = db.session.scalars(db.select(RoomArrival).order_by(RoomArrival.id)).all()
        assert [arrival.arrived_at for arrival in arrivals] == [
            first_arrival,
            second_arrival,
            third_arrival,
        ]
        assert db.session.query(RecognitionEvent).count() == 0
        assert db.session.query(PresenceInterval).count() == 0
    login(trained, "admin")
    report = trained.get("/api/admin/report?user_id=1&day=2026-11-04").json
    assert [room["count"] for room in report["rooms"]] == [2, 1]
    assert [room["percent"] for room in report["rooms"]] == [66.67, 33.33]
    assert report["total_arrivals"] == 3
    assert report["current_room"] == 1
    assert report["last_arrival"] == report["arrivals"][-1]
    page = trained.get("/admin/analytics?user_id=1&day=2026-11-04").text
    assert 'data-room-one-count="2"' in page and 'data-room-two-count="1"' in page
    assert "Confirmed arrivals" in page


def test_unconfirmed_candidate_needs_consecutive_frames(trained, app, clock):
    capture = start(trained)
    send_frame(trained, capture)
    clock.advance(0.5)
    send_frame(trained, capture, marker=0, sequence=1)
    clock.advance(0.5)
    send_frame(trained, capture, sequence=2)
    expected_at = clock()
    with app.app_context():
        assert db.session.query(RoomArrival).count() == 0
    clock.advance(0.5)
    send_frame(trained, capture, sequence=3)
    with app.app_context():
        arrival = db.session.scalars(db.select(RoomArrival)).one()
        assert arrival.arrived_at == expected_at


def test_confirmation_does_not_bridge_room_generations_or_long_gaps(trained, app, clock):
    capture = start(trained)
    assert send_frame(trained, capture).status_code == 200
    switched = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
    clock.advance(0.5)
    assert send_frame(trained, switched).status_code == 200
    with app.app_context():
        assert room_report(1)["current_room"] is None
    clock.advance(3.5)
    expected_at = clock()
    assert send_frame(trained, switched, sequence=1).status_code == 200
    with app.app_context():
        assert room_report(1)["current_room"] is None
    clock.advance(0.5)
    assert send_frame(trained, switched, sequence=2).status_code == 200
    with app.app_context():
        arrival = db.session.scalars(db.select(RoomArrival)).one()
        assert arrival.room == 2 and arrival.arrived_at == expected_at


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


def test_disconnect_preserves_room_across_capture_leases(trained, app, clock):
    capture = start(trained)
    send_frame(trained, capture)
    clock.advance(1)
    send_frame(trained, capture, sequence=1)
    clock.advance(60)
    with app.app_context():
        reap_capture()
        assert db.session.get(CaptureSession, capture["capture_id"]).ended_at is not None
        assert room_report(1)["current_room"] == 1
    restarted = start(trained)
    send_frame(trained, restarted)
    clock.advance(0.5)
    send_frame(trained, restarted, sequence=1)
    login(trained, "admin")
    report = trained.get("/api/admin/report?user_id=1&day=2026-11-04").json
    assert report["total_arrivals"] == 1
    assert report["current_room"] == 1


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
        assert room_report(1)["current_room"] == 1
        assert room_report(1)["total_arrivals"] == 1
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
        assert db.session.query(RoomArrival).count() == 0


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
        arrival = db.session.scalars(db.select(RoomArrival)).one()
        assert arrival.user_id == 1


def test_daily_clipping_and_dublin_dst(app):
    with app.app_context():
        start_time, end_time = day_window("2026-10-25")
        assert (end_time - start_time).total_seconds() == 25 * 3600
        start_time, end_time = day_window("2026-03-29")
        assert (end_time - start_time).total_seconds() == 23 * 3600
        assert day_window("2026-11-04")[0] == datetime(2026, 11, 4)


def test_arrival_counts_only_on_the_day_of_first_sighting(trained, clock):
    clock.value = datetime(2026, 11, 3, 23, 59, 59)
    capture = start(trained)
    send_frame(trained, capture)
    clock.advance(2)
    send_frame(trained, capture, sequence=1)
    trained.post("/api/camera/stop", json=capture)
    login(trained, "admin")
    for day, count in (("2026-11-03", 1), ("2026-11-04", 0)):
        report = trained.get(f"/api/admin/report?user_id=1&day={day}").json
        assert report["total_arrivals"] == count
        assert report["current_room"] == 1


def test_analytics_empty_and_invalid_filters(client):
    login(client, "admin")
    assert "No room arrivals" in client.get("/admin/analytics").text
    assert client.get("/api/admin/report?day=invalid").status_code == 400
    assert client.get("/api/admin/report?user_id=bad").status_code == 400
    assert client.get("/api/admin/report?user_id=999").status_code == 404


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
        rooms = db.session.scalars(db.select(RoomArrival.room).order_by(RoomArrival.id))
        assert list(rooms) == [1]


def test_legacy_confirmed_visits_seed_arrivals_and_room(trained, app, clock):
    capture = start(trained)
    with app.app_context():
        from surveillance.services.ledger import audit_source, verify_audit

        for offset, room, confirmed in ((-86400, 1, True), (-60, 1, True), (-10, 2, False)):
            seen_at = clock() + timedelta(seconds=offset)
            interval = PresenceInterval(
                capture_id=capture["capture_id"],
                user_id=1,
                room=room,
                first_seen=seen_at,
                last_seen=seen_at,
                ended_at=seen_at,
                confirmed=confirmed,
            )
            db.session.add(interval)
            audit_source(interval)
        db.session.commit()
        assert room_report(1, "2026-11-04")["total_arrivals"] == 0
        assert room_report(1)["current_room"] == 1
        assert room_report(1)["total_arrivals"] == 1
    assert send_frame(trained, capture).status_code == 200
    clock.advance(0.5)
    assert send_frame(trained, capture, sequence=1).status_code == 200
    switched = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
    clock.advance(0.5)
    assert send_frame(trained, switched).status_code == 200
    clock.advance(0.5)
    assert send_frame(trained, switched, sequence=1).status_code == 200
    with app.app_context():
        assert room_report(1)["current_room"] == 2
        assert [entry["room"] for entry in room_report(1)["arrivals"]] == [1, 2]
        assert db.session.query(PresenceInterval).count() == 3
        assert verify_audit()["ok"]
