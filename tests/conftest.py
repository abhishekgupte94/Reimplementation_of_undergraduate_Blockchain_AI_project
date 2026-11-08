import io
from datetime import datetime, timedelta

import numpy as np
import pytest
from PIL import Image

from surveillance import create_app
from surveillance.extensions import db
from surveillance.models import User


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 4, 12)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakePipeline:
    """Deterministic test double. Production always uses Haar/OpenFace."""

    def detect(self, image):
        marker = int(image[0, 0, 2])
        if marker == 0:
            return []
        box = (0, 0, image.shape[1], image.shape[0])
        return [box, box] if marker == 255 else [box]

    def embedding(self, image, box):
        marker = int(image[0, 0, 2])
        vector = np.zeros(128, dtype=np.float32)
        vector[0 if marker < 128 else 1] = 1
        vector[2] = marker / 1000
        return vector / np.linalg.norm(vector)


def photo_bytes(marker=30):
    out = io.BytesIO()
    Image.new("RGB", (80, 80), (marker, 60, 70)).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(tmp_path, clock):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.sqlite'}",
            "PRIVATE_DIR": tmp_path / "private",
            "WTF_CSRF_ENABLED": False,
            "CLOCK": clock,
            "MIN_FRAME_SECONDS": 0,
        }
    )
    app.extensions["face_pipeline"] = FakePipeline()
    with app.app_context():
        db.create_all()
        for username, role in (
            ("alice", "participant"),
            ("bob", "participant"),
            ("admin", "admin"),
        ):
            user = User(username=username, display_name=username.title(), role=role)
            user.set_password("demo-password-123")
            db.session.add(user)
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username="alice"):
    return client.post("/login", data={"username": username, "password": "demo-password-123"})


def upload(client, markers=(30, 31, 32)):
    return client.post(
        "/api/enrollment/photos",
        data={
            "photos": [
                (io.BytesIO(photo_bytes(marker)), f"face-{marker}.png") for marker in markers
            ]
        },
    )


def send_frame(client, capture, marker=30, sequence=0):
    return client.post(
        "/api/camera/frame",
        data={
            **{key: str(value) for key, value in capture.items()},
            "sequence": str(sequence),
            "frame": (io.BytesIO(photo_bytes(marker)), "frame.png"),
        },
    )


@pytest.fixture
def trained(app, client):
    from surveillance.services.training import process_next_job

    login(client)
    assert upload(client).status_code == 202
    with app.app_context():
        assert process_next_job()
    return client
