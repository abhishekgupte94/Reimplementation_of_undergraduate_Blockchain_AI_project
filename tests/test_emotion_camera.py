import hashlib
import threading

import numpy as np
import pytest
from conftest import send_frame

from surveillance import emotion_cli
from surveillance.services.emotions import MODEL_FILES, AsyncEmotionDetector, EmotionModelError
from surveillance.services.emotions import FacialExpressionRecognizer as Recognizer


def test_expression_classifier_abstains_on_uncertain_and_unsupported_labels(tmp_path):
    recognizer = Recognizer(tmp_path)
    scores = np.zeros(7, dtype=np.float32)
    assert recognizer.estimate(scores) is None
    scores[1] = 10  # Disgust is outside the five demo labels.
    assert recognizer.estimate(scores) is None
    scores[3] = 20
    assert recognizer.estimate(scores)["emotion"] == "happy"


def test_expression_model_requires_verified_weights(tmp_path):
    recognizer = Recognizer(tmp_path)
    with pytest.raises(EmotionModelError, match="weights missing"):
        recognizer.load()
    (tmp_path / next(iter(MODEL_FILES))).write_bytes(b"invalid")
    with pytest.raises(EmotionModelError, match="integrity verification"):
        recognizer.load()


def test_expression_cli_installs_both_verified_files(app, monkeypatch):
    files = {}

    def download(url, destination, checksum):
        assert url.endswith(destination.name)
        assert checksum == hashlib.sha256(destination.name.encode()).hexdigest()
        files[destination.name] = destination.name.encode()
        destination.write_bytes(files[destination.name])

    for name in MODEL_FILES:
        monkeypatch.setitem(MODEL_FILES, name, hashlib.sha256(name.encode()).hexdigest())
    monkeypatch.setattr(emotion_cli, "download_verified", download)
    monkeypatch.setattr(emotion_cli, "smoke_models", lambda recognizer: None)
    result = app.test_cli_runner().invoke(args=["download-emotions"])
    assert result.exit_code == 0, result.output
    assert files.keys() == MODEL_FILES.keys()
    installed = app.config["PRIVATE_DIR"] / "models" / "emotions"
    assert all((installed / name).read_bytes() == data for name, data in files.items())


def test_expression_sampling_does_not_block_camera_or_cross_room(trained, app, clock):
    began = threading.Event()
    release = threading.Event()
    calls = []

    class SlowRecognizer:
        def predict(self, crop):
            calls.append(crop.shape)
            began.set()
            assert release.wait(timeout=5)
            return {"emotion": "happy", "confidence": 0.9, "model_version": "test"}

    detector = AsyncEmotionDetector(app, recognizer=SlowRecognizer())
    app.config["EMOTION_RECOGNITION_ENABLED"] = True
    app.extensions["emotion_detector"] = detector
    try:
        capture = trained.post("/api/camera/start", json={"room": 1}).json
        first = send_frame(trained, capture).json
        assert first["emotion"]["status"] == "waiting"
        assert detector.future is None
        clock.advance(0.5)
        assert send_frame(trained, capture, sequence=1).json["emotion"]["status"] == "sampling"
        assert began.wait(timeout=2)
        pending = detector.future
        clock.advance(0.5)
        while_pending = send_frame(trained, capture, sequence=2).json
        assert while_pending["faces"][0]["name"] == "Alice"
        assert while_pending["emotion"]["status"] == "sampling"
        assert detector.future is pending
        release.set()
        pending.result(timeout=2)
        clock.advance(0.5)
        after = send_frame(trained, capture, sequence=3).json
        assert after["faces"][0]["emotion"] == "happy"
        detector.future.result(timeout=2)

        room_two = trained.post("/api/camera/switch", json={**capture, "room": 2}).json
        clock.advance(0.5)
        assert "emotion" not in send_frame(trained, room_two).json["faces"][0]
        clock.advance(0.5)
        second = send_frame(trained, room_two, sequence=1).json
        assert "emotion" not in second["faces"][0]
        detector.future.result(timeout=2)
        clock.advance(0.5)
        result = send_frame(trained, room_two, sequence=2).json
        assert result["faces"][0]["emotion"] == "happy"
        assert all(shape == (160, 160, 3) for shape in calls)
    finally:
        release.set()
        detector.executor.shutdown(wait=True)


def test_missing_expression_model_does_not_interrupt_face_tracking(trained, app, clock):
    app.config["EMOTION_RECOGNITION_ENABLED"] = True
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    send_frame(trained, capture)
    clock.advance(0.5)
    send_frame(trained, capture, sequence=1)
    detector = app.extensions["emotion_detector"]
    try:
        detector.future.result(timeout=2)
        clock.advance(0.5)
        response = send_frame(trained, capture, sequence=2)
        assert response.status_code == 200
        assert response.json["emotion"]["status"] == "unavailable"
        assert response.json["faces"][0]["name"] == "Alice"
        assert "emotion" not in response.json["faces"][0]
    finally:
        detector.executor.shutdown(wait=True)
