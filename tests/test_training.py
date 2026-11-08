from pathlib import Path

import joblib
import numpy as np
import pytest
from conftest import login, upload

from surveillance.extensions import db
from surveillance.models import ModelVersion, RuntimeState, TrainingJob
from surveillance.services.recognition import build_classifier, match
from surveillance.services.training import process_next_job, recover_jobs
from surveillance.services.vision import FacePipeline, VisionError, decode_image


def test_solo_svc_and_unknown_rejection():
    one, two, unknown = np.eye(128, dtype=np.float32)[:3]
    solo = build_classifier([one, one, one], [1, 1, 1])
    assert solo["mode"] == "distance"
    assert match(one, solo, 0.8)[0] == 1
    assert match(unknown, solo, 0.8)[0] is None
    svc = build_classifier([one] * 3 + [two] * 3, [1] * 3 + [2] * 3)
    assert svc["mode"] == "svc"
    assert match(one, svc, 0.8)[0] == 1
    assert match(two, svc, 0.8)[0] == 2
    assert match(unknown, svc, 0.8)[0] is None


def test_training_transition_and_atomic_publication(trained, app):
    assert trained.get("/api/enrollment/status").json["mode"] == "distance"
    trained.post("/logout")
    login(trained, "bob")
    upload(trained, (180, 181, 182))
    with app.app_context():
        previous_id = db.session.get(RuntimeState, 1).active_model_id
        assert process_next_job()
        active_id = db.session.get(RuntimeState, 1).active_model_id
        assert active_id != previous_id
        version = db.session.get(ModelVersion, active_id)
        assert version.mode == "svc" and len(version.user_ids) == 2
        artifact = joblib.load(app.config["PRIVATE_DIR"] / "models" / version.filename)
        assert artifact["mode"] == "svc"
        assert not list((app.config["PRIVATE_DIR"] / "models").glob("*.tmp"))


def test_failed_retraining_keeps_working_model(trained, app, monkeypatch):
    trained.post("/api/enrollment/retrain")
    with app.app_context():
        previous_id = db.session.get(RuntimeState, 1).active_model_id

        def fail(*args):
            raise VisionError("Simulated embedding failure")

        monkeypatch.setattr(app.extensions["face_pipeline"], "embedding", fail)
        process_next_job()
        assert db.session.get(RuntimeState, 1).active_model_id == previous_id
        job = db.session.scalar(db.select(TrainingJob).order_by(TrainingJob.id.desc()))
        assert job.status == "failed" and "Simulated" in job.message
    assert trained.get("/api/enrollment/status").json["ready"]


def test_restart_recovers_interrupted_jobs(app):
    with app.app_context():
        db.session.add(TrainingJob(user_id=1, status="extracting", message="Interrupted"))
        db.session.commit()
        recover_jobs()
        job = db.session.scalar(db.select(TrainingJob))
        assert job.status == "queued" and "recovery" in job.message


def test_real_haar_rejects_blank_image(app):
    from conftest import photo_bytes

    with app.app_context():
        engine = FacePipeline(app.config["MODEL_PATH"])
        assert engine.detect(decode_image(photo_bytes(0))) == []


@pytest.mark.model
def test_real_openface_smoke(app):
    path = Path(__file__).resolve().parents[1] / "instance/private/models/nn4.small2.v1.t7"
    if not path.is_file():
        pytest.skip("Run flask --app surveillance download-model first.")
    crop = np.random.default_rng(42).integers(0, 256, (96, 96, 3), dtype=np.uint8)
    vector = FacePipeline(path).embedding(crop, (0, 0, 96, 96))
    assert vector.shape == (128,) and np.isfinite(vector).all()
    assert np.linalg.norm(vector) == pytest.approx(1, abs=1e-6)
