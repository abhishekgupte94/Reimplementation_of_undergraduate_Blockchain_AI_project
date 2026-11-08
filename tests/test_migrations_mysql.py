import os
from datetime import datetime
from pathlib import Path

import pytest
from conftest import FakePipeline, login, send_frame, upload
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from surveillance import create_app
from surveillance.extensions import db
from surveillance.models import LedgerState, RuntimeState, User
from surveillance.services.emotion_observations import latest_emotion, persist_emotions
from surveillance.services.ledger import verify_audit
from surveillance.services.reporting import room_report
from surveillance.services.training import process_next_job
from surveillance.services.weapons import persist_alerts

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


def test_sqlite_migration_upgrade_and_downgrade(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "migration-test",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'migration.sqlite'}",
            "PRIVATE_DIR": tmp_path / "private",
        }
    )
    with application.app_context():
        upgrade(directory=MIGRATIONS)
        assert "recognition_event" in inspect(db.engine).get_table_names()
        assert "room_arrival" in inspect(db.engine).get_table_names()
        assert "emotion_observation" in inspect(db.engine).get_table_names()
        assert "runtime_state" in inspect(db.engine).get_table_names()
        downgrade(directory=MIGRATIONS, revision="base")
        assert inspect(db.engine).get_table_names() == ["alembic_version"]
        db.session.remove()
        db.engine.dispose()


@pytest.mark.mysql
def test_mysql_migration_training_arrivals_and_expressions(tmp_path, clock):
    uri = os.environ.get("TEST_MYSQL_URL")
    if not uri:
        pytest.skip("Set TEST_MYSQL_URL to a dedicated empty database ending in _test.")
    url = make_url(uri)
    assert url.drivername == "mysql+pymysql" and url.database.endswith("_test"), (
        "Integration testing requires a dedicated mysql+pymysql database ending in _test."
    )
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "mysql-integration-test",
            "SQLALCHEMY_DATABASE_URI": uri,
            "PRIVATE_DIR": tmp_path / "private",
            "WTF_CSRF_ENABLED": False,
            "CLOCK": clock,
        }
    )
    application.extensions["face_pipeline"] = FakePipeline()
    with application.app_context():
        assert not inspect(db.engine).get_table_names(), "MySQL test database must be empty."
        try:
            upgrade(directory=MIGRATIONS)
            user = User(username="alice", display_name="Alice", role="participant")
            user.set_password("demo-password-123")
            db.session.add(user)
            db.session.commit()
            user_id = user.id
            client = application.test_client()
            login(client)
            assert upload(client).status_code == 202
            assert process_next_job()
            assert client.get("/api/enrollment/status").json["ready"]
            timestamp = datetime(2026, 11, 4, 12, 0, 0, 123456)
            clock.value = timestamp
            payload = client.post("/api/camera/start", json={"room": 1}).json
            sample = {
                "capture_id": payload["capture_id"],
                "generation": payload["generation"],
                "frame_sequence": 0,
                "room": 1,
                "observed_at": timestamp,
            }
            detections = [{"label": "Pen", "confidence": 0.91, "box": [10, 20, 30, 40]}]
            with db.engine.connect() as writer:
                writer.execute(
                    db.select(LedgerState.id).where(LedgerState.id == 1).with_for_update()
                )
                assert persist_alerts(sample, detections) == []
            assert len(persist_alerts(sample, detections)) == 1
            assert send_frame(client, payload).status_code == 200
            clock.advance(0.5)
            assert send_frame(client, payload, sequence=1).status_code == 200
            expression_at = clock()
            model_id = db.session.get(RuntimeState, 1).active_model_id
            db.session.rollback()
            assert persist_emotions(
                {**sample, "frame_sequence": 1, "observed_at": expression_at, "model_id": model_id},
                {
                    user_id: {
                        "emotion": "happy",
                        "confidence": 0.94,
                        "model_version": "test-expressions",
                    }
                },
            )
            expression = latest_emotion(user_id, day="2026-11-04", room=1)
            assert expression.emotion == "happy"
            assert expression.observed_at == expression_at
            assert expression.confidence == pytest.approx(0.94)
            assert client.post("/api/camera/stop", json=payload).status_code == 200
            report = room_report(user_id, "2026-11-04")
            assert report["total_arrivals"] == 1
            assert report["last_arrival"] == {"room": 1, "at": timestamp}
            assert verify_audit()["ok"]
        finally:
            db.session.rollback()
            downgrade(directory=MIGRATIONS, revision="base")
            with db.engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE alembic_version")
            db.session.remove()
            db.engine.dispose()
