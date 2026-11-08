from conftest import login, send_frame

from surveillance.services.ledger import verify_audit
from surveillance.services.weapons import persist_alerts


def test_two_room_face_weapon_report_and_audit_flow(trained, app, clock):
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    assert send_frame(trained, capture).status_code == 200
    clock.advance(0.5)
    assert send_frame(trained, capture, sequence=1).status_code == 200
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
        first_alert = persist_alerts(weapon_metadata(capture, 1), detection)[0]
    room_two = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
    with app.app_context():
        assert persist_alerts(weapon_metadata(capture, 2), detection) == []
    clock.advance(0.5)
    assert send_frame(trained, room_two).status_code == 200
    clock.advance(1)
    assert send_frame(trained, room_two, sequence=1).status_code == 200
    with app.app_context():
        assert len(persist_alerts(weapon_metadata(room_two, 1), detection)) == 1
    assert trained.post("/api/camera/stop", json=room_two).status_code == 200
    trained.post("/logout")
    login(trained, "admin")
    report = trained.get("/api/admin/report?user_id=1&day=2026-11-04").json
    assert [room["seconds"] for room in report["rooms"]] == [0.5, 1]
    answer = trained.post(
        "/api/admin/chat", json={"user_id": 1, "day": "2026-11-04", "question": "time in each room"}
    ).json
    assert answer["report"] == report
    alerts = trained.get("/api/admin/weapon-alerts").json["alerts"]
    assert len(alerts) == 2 and {alert["room"] for alert in alerts} == {1, 2}
    assert all("user_id" not in alert for alert in alerts)
    assert (
        trained.post(f"/api/admin/weapon-alerts/{first_alert}/acknowledge", json={}).status_code
        == 200
    )
    with app.app_context():
        assert verify_audit()["ok"]
