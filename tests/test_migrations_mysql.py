import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakePipeline, login, upload
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from surveillance import create_app
from surveillance.extensions import db
from surveillance.models import LedgerState, User
from surveillance.services.presence import (
    finish_capture,
    locked_state,
    now,
    record_observations,
    start_capture,
)
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
        assert "runtime_state" in inspect(db.engine).get_table_names()
        downgrade(directory=MIGRATIONS, revision="base")
        assert inspect(db.engine).get_table_names() == ["alembic_version"]
        db.session.remove()
        db.engine.dispose()


@pytest.mark.mysql
def test_mysql_migration_training_and_subsecond_reporting(tmp_path):
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
            payload = start_capture(user_id, 1)
            sample = {
                "capture_id": payload["capture_id"],
                "generation": payload["generation"],
                "frame_sequence": 0,
                "room": 1,
                "observed_at": now(),
            }
            detections = [{"label": "Pen", "confidence": 0.91, "box": [10, 20, 30, 40]}]
            with db.engine.connect() as writer:
                writer.execute(
                    db.select(LedgerState.id).where(LedgerState.id == 1).with_for_update()
                )
                assert persist_alerts(sample, detections) == []
            assert len(persist_alerts(sample, detections)) == 1
            from surveillance.models import CaptureSession, RuntimeState

            timestamp = datetime(2026, 11, 4, 12, 0, 0, 123456)
            with locked_state() as state:
                capture = db.session.get(CaptureSession, payload["capture_id"])
                model_id = db.session.get(RuntimeState, 1).active_model_id
                record_observations(capture, model_id, {user_id: 0.1}, timestamp)
                db.session.flush()
                record_observations(
                    capture, model_id, {user_id: 0.2}, timestamp + timedelta(seconds=0.5)
                )
                finish_capture(state, capture, timestamp + timedelta(seconds=0.5))
            assert room_report(user_id, "2026-11-04")["total_seconds"] == 0.5
            from surveillance.services.ledger import verify_audit

            assert verify_audit()["ok"]
        finally:
            db.session.rollback()
            downgrade(directory=MIGRATIONS, revision="base")
            with db.engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE alembic_version")
            db.session.remove()
            db.engine.dispose()
