import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from conftest import login, send_frame

from surveillance.extensions import db
from surveillance.models import AuditEntry, CaptureSession, RoomArrival
from surveillance.services.ledger import audit_source
from surveillance.services.presence import capture_state_lock
from surveillance.services.weapons import (
    MODEL_FILES,
    AsyncWeaponDetector,
    SSDWeaponDetector,
    WeaponModelError,
    persist_alerts,
)
from surveillance.weapon_models import WeaponAlert


def camera(client):
    return client.post("/api/camera/start", json={"room": 1}).json


def metadata(capture, clock, sequence=0):
    return {
        "capture_id": capture["capture_id"],
        "generation": capture["generation"],
        "frame_sequence": sequence,
        "room": capture["room"],
        "observed_at": clock(),
    }


def test_ssd_filters_for_pen_and_clips_boxes(tmp_path):
    detector = SSDWeaponDetector(tmp_path)
    output = np.array(
        [
            [
                [
                    [0, 569, 0.95, -0.2, 0.2, 0.5, 0.8],
                    [0, 313, 0.99, 0, 0, 1, 1],
                    [0, 569, 0.3, 0.7, 0.1, 1.1, 0.4],
                    [0, 569, 0.9, 1.1, 0, 1.2, 1],
                ]
            ]
        ]
    )
    assert detector.decode(output, 100, 200) == [
        {"label": "Pen", "confidence": 0.95, "box": [0, 40, 50, 120]}
    ]


def test_ssd_rejects_invalid_output_and_missing_weights(tmp_path):
    detector = SSDWeaponDetector(tmp_path)
    with pytest.raises(WeaponModelError, match="unavailable"):
        detector.load()
    with pytest.raises(WeaponModelError, match="invalid detection"):
        detector.decode(np.full((1, 1, 4, 7), np.nan), 100, 100)
    with pytest.raises(WeaponModelError, match="at least 0.3"):
        SSDWeaponDetector(tmp_path, 0)
    (tmp_path / "frozen_inference_graph.pb").write_bytes(b"untrained-random")
    with pytest.raises(WeaponModelError, match="integrity check failed"):
        detector.load()


def test_disabled_weapons_preserve_arrivals(trained, app):
    capture = camera(trained)
    assert send_frame(trained, capture).status_code == 200
    response = send_frame(trained, capture, sequence=1)
    assert response.status_code == 200
    assert response.json["weapons"]["status"] == "disabled"
    assert "alert_ids" not in response.json["weapons"]
    with app.app_context():
        assert db.session.query(RoomArrival).count() == 1


def test_weapon_queue_failure_preserves_committed_arrivals(trained, app):
    class BrokenQueue:
        def submit(self, image, data):
            raise RuntimeError("inference worker stopped")

    app.config["WEAPON_DETECTION_ENABLED"] = True
    app.extensions["weapon_detector"] = BrokenQueue()
    capture = camera(trained)
    assert send_frame(trained, capture).status_code == 200
    response = send_frame(trained, capture, sequence=1)
    assert response.status_code == 200
    assert response.json["weapons"]["status"] == "unavailable"
    with app.app_context():
        assert db.session.query(RoomArrival).count() == 1


def test_slow_cold_load_does_not_block_face_request(trained, app):
    release = threading.Event()
    began = threading.Event()
    samples = []

    class SlowDetector:
        def detect(self, image, *, frame_size):
            samples.append((image.shape, frame_size))
            began.set()
            release.wait(timeout=5)
            return []

    app.config["WEAPON_DETECTION_ENABLED"] = True
    with app.app_context():
        service = AsyncWeaponDetector(app)
        service.detector = SlowDetector()
    app.extensions["weapon_detector"] = service
    try:
        capture = camera(trained)
        started = time.perf_counter()
        response = send_frame(trained, capture)
        assert response.status_code == 200
        assert time.perf_counter() - started < 1
        assert response.json["weapons"]["status"] == "loading"
        assert began.wait(timeout=1)
        assert samples == [((300, 300, 3), (response.json["width"], response.json["height"]))]
        first_job = service.future
        service.submit(
            np.zeros((480, 640, 3), dtype=np.uint8), metadata(capture, app.config["CLOCK"])
        )
        assert service.future is first_job
    finally:
        release.set()
        service.executor.shutdown(wait=True)


def test_persist_alerts_cooldown_and_room_generation(trained, app, clock):
    capture = camera(trained)
    detection = [{"label": "Pen", "confidence": 0.9, "box": [10, 20, 30, 40]}]
    with app.app_context():
        assert len(persist_alerts(metadata(capture, clock), detection)) == 1
        assert persist_alerts(metadata(capture, clock), detection) == []
        assert db.session.query(WeaponAlert).count() == 1
    switched = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
    with app.app_context():
        assert persist_alerts(metadata(capture, clock), detection) == []
        assert len(persist_alerts(metadata(switched, clock, 1), detection)) == 1
        assert db.session.query(WeaponAlert).count() == 2


def test_late_or_stopped_weapon_results_are_discarded(trained, app, clock):
    capture = camera(trained)
    original = metadata(capture, clock)
    detection = [{"label": "Pen", "confidence": 0.9, "box": [10, 20, 30, 40]}]
    clock.advance(4)
    with app.app_context():
        assert persist_alerts(original, detection) == []
    trained.post("/api/camera/stop", json=capture)
    with app.app_context():
        assert persist_alerts(metadata(capture, clock), detection) == []


def test_ssd_yields_to_busy_camera_and_never_expires_presence(trained, app, clock):
    capture = camera(trained)
    detection = [{"label": "Pen", "confidence": 0.9, "box": [10, 20, 30, 40]}]
    with app.app_context():
        with capture_state_lock():
            assert persist_alerts(metadata(capture, clock), detection) == []
        clock.advance(app.config["CAPTURE_TIMEOUT_SECONDS"] + 1)
        assert persist_alerts(metadata(capture, clock), detection) == []
        assert db.session.get(CaptureSession, capture["capture_id"]).ended_at is None
        assert db.session.query(WeaponAlert).count() == 0


def test_ssd_yields_to_busy_database_writer(trained, app, clock):
    capture = camera(trained)
    sample = metadata(capture, clock)
    detection = [{"label": "Pen", "confidence": 0.9, "box": [10, 20, 30, 40]}]
    with app.app_context():
        with db.engine.connect() as other_writer:
            other_writer.exec_driver_sql("BEGIN IMMEDIATE")
            started = time.perf_counter()
            assert persist_alerts(sample, detection) == []
            assert time.perf_counter() - started < 1
        assert len(persist_alerts(sample, detection)) == 1


def test_admin_alert_permissions_acknowledgement_and_audit(trained, app, clock):
    feed = "/api/admin/weapon-alerts"
    pending = f"{feed}?pending=1"
    anonymous = app.test_client()
    assert anonymous.get(pending).status_code == 401
    assert "weapons.js" not in anonymous.get("/admin/login").text
    capture = camera(trained)
    assert "weapons.js" not in trained.get("/cameras").text
    admin = app.test_client()
    login(admin, "admin")
    assert "weapons.js" in admin.get("/admin/analytics").text
    assert admin.get(pending).json == {"pending": None}
    with app.app_context():
        alert_id = persist_alerts(
            metadata(capture, clock),
            [{"label": "Pen", "confidence": 0.9, "box": [10, 20, 30, 40]}],
        )[0]
        legacy = WeaponAlert(
            **metadata(capture, clock, sequence=1),
            label="Handgun",
            confidence=0.9,
            box=[1, 2, 3, 4],
            model_tag="legacy-firearms",
        )
        db.session.add(legacy)
        audit_source(legacy, "weapon.detected")
        db.session.commit()
    assert trained.get("/admin/alerts").status_code == 403
    assert trained.get(pending).status_code == 403
    action = f"{feed}/{alert_id}/acknowledge"
    assert trained.post(action, json={}).status_code == 403
    assert admin.get(pending).json["pending"]["id"] == alert_id
    assert admin.post(action, json={}).status_code == 200
    assert admin.post(action, json={}).status_code == 200
    assert admin.post(f"{feed}/9999/acknowledge", json={}).status_code == 404
    assert admin.get(pending).json == {"pending": None}
    history = admin.get(feed).json["alerts"]
    assert {alert["label"]: alert["acknowledged"] for alert in history} == {
        "Handgun": False,
        "Pen": True,
    }
    with app.app_context():
        assert db.session.query(AuditEntry).filter_by(kind="weapon.acknowledged").count() == 1
    admin.post("/logout")
    assert admin.get(pending).status_code == 401


@pytest.mark.model
def test_real_ssd_positive_pen_and_blank_frame():
    directory = Path(__file__).resolve().parents[1] / "instance/private/models/weapons"
    if not all((directory / name).is_file() for name in (*MODEL_FILES, "pen-test.jpg")):
        pytest.skip("Run download-weapons --with-sample first.")
    detector = SSDWeaponDetector(directory)
    image = cv2.imread(str(directory / "pen-test.jpg"))
    detections = detector.detect(image)
    assert any(item["label"] == "Pen" and item["confidence"] > 0.7 for item in detections)
    assert detector.detect(np.zeros((480, 640, 3), dtype=np.uint8)) == []
