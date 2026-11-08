import pytest
from conftest import login, send_frame

from surveillance.services.emotions import AsyncEmotionDetector
from surveillance.services.ledger import verify_audit
from surveillance.services.weapons import persist_alerts


@pytest.fixture
def expression_worker(app):
    class Recognizer:
        def predict(self, crop):
            label = "happy" if int(crop[0, 0, 2]) == 30 else "surprised"
            return {"emotion": label, "confidence": 0.94, "model_version": "test-expressions"}

    app.config["EMOTION_RECOGNITION_ENABLED"] = True
    worker = AsyncEmotionDetector(app, recognizer=Recognizer())
    app.extensions["emotion_detector"] = worker
    yield worker
    worker.executor.shutdown(wait=True)


def test_two_room_face_expression_weapon_report_and_audit_flow(
    trained, app, clock, expression_worker
):
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    assert send_frame(trained, capture).status_code == 200
    clock.advance(0.5)
    assert send_frame(trained, capture, sequence=1).status_code == 200
    expression_worker.future.result(timeout=5)
    clock.advance(0.5)
    first_face = send_frame(trained, capture, sequence=2).json["faces"][0]
    assert first_face["name"] == "Alice"
    assert first_face["emotion"] == "happy"
    detection = [{"label": "Pen", "confidence": 0.91, "box": [10, 20, 30, 40]}]

    def weapon_metadata(slot, sequence):
        return {
            "capture_id": slot["capture_id"],
            "generation": slot["generation"],
            "frame_sequence": sequence,
            "room": slot["room"],
            "observed_at": clock(),
        }

    with app.app_context():
        first_alert = persist_alerts(weapon_metadata(capture, 2), detection)[0]
    room_two = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
    with app.app_context():
        assert persist_alerts(weapon_metadata(capture, 2), detection) == []
    clock.advance(0.5)
    fresh_room = send_frame(trained, room_two, marker=31)
    assert fresh_room.status_code == 200
    assert not fresh_room.json["faces"][0].get("emotion")
    clock.advance(1)
    assert send_frame(trained, room_two, marker=31, sequence=1).status_code == 200
    expression_worker.future.result(timeout=5)
    clock.advance(0.5)
    assert send_frame(trained, room_two, marker=31, sequence=2).json["faces"][0][
        "emotion"
    ] == "surprised"
    with app.app_context():
        assert len(persist_alerts(weapon_metadata(room_two, 1), detection)) == 1
    assert trained.post("/api/camera/stop", json=room_two).status_code == 200
    trained.post("/logout")
    login(trained, "admin")
    report = trained.get("/api/admin/report?user_id=1&day=2026-11-04").json
    assert [room["count"] for room in report["rooms"]] == [1, 1]
    assert report["current_room"] == 2
    answer = trained.post(
        "/api/admin/chat",
        json={"question": "How many times did Alice appear in each room on 2026-11-04?"},
    ).json
    assert answer["report"] == report
    expression_answer = trained.post(
        "/api/admin/chat",
        json={"question": "What was Alice's latest expression in Room 2?"},
    ).json["answer"]
    assert "surprised" in expression_answer and "Room 2" in expression_answer
    alerts = trained.get("/api/admin/weapon-alerts").json["alerts"]
    assert len(alerts) == 2 and {alert["room"] for alert in alerts} == {1, 2}
    assert all("user_id" not in alert for alert in alerts)
    assert (
        trained.post(f"/api/admin/weapon-alerts/{first_alert}/acknowledge", json={}).status_code
        == 200
    )
    with app.app_context():
        assert verify_audit()["ok"]
