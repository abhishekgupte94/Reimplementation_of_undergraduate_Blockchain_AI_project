import io
import re

from conftest import login, photo_bytes, upload

from surveillance.extensions import db
from surveillance.models import EnrollmentImage, TrainingJob, User


def test_registration_cannot_set_role(client, app):
    response = client.post(
        "/register",
        data={
            "username": "charlie",
            "display_name": "Charlie",
            "password": "long-password-123",
            "role": "admin",
        },
    )
    assert response.status_code == 302
    assert response.location.endswith("/login") and "/admin/" not in response.location
    with app.app_context():
        user = db.session.scalar(db.select(User).where(User.username == "charlie"))
        assert user.role == "participant"
        assert user.password_hash != "long-password-123"
        assert user.check_password("long-password-123")


def test_authentication_and_role_boundaries(client):
    assert client.get("/api/admin/report").status_code == 401
    assert client.get("/enrollment").status_code == 302
    assert client.get("/enrollment").location.endswith("/login")
    homepage = client.get("/")
    assert homepage.status_code == 200
    assert "Participant login" in homepage.text
    background = client.get("/static/homepage-background.png")
    assert background.status_code == 200 and background.mimetype == "image/png"
    assert client.post("/login", data={"username": "alice", "password": "bad"}).status_code == 200
    assert login(client).status_code == 302
    assert client.get("/api/admin/report").status_code == 403
    assert client.get("/admin/chatbot").status_code == 403
    assert client.get("/enrollment").status_code == 200
    assert client.get("/").location.endswith("/enrollment")
    client.post("/logout")
    login(client, "admin")
    assert client.get("/admin/analytics").status_code == 200
    assert client.get("/").location.endswith("/admin/analytics")
    assert client.get("/cameras").status_code == 403


def test_csrf_rejects_missing_token_and_accepts_valid_form(app):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    response = client.get("/login")
    token = re.search(r'name="csrf-token" content="([^"]+)"', response.text).group(1)
    assert client.post("/login", data={"username": "alice"}).status_code == 400
    assert (
        client.post(
            "/login",
            data={"username": "alice", "password": "demo-password-123", "csrf_token": token},
        ).status_code
        == 302
    )
    assert client.post("/api/enrollment/retrain").status_code == 400


def test_minimum_duplicates_and_background_queue(client, app):
    login(client)
    assert client.post("/api/camera/start", json={"room": 1}).status_code == 409
    assert upload(client, (30, 31)).json["count"] == 2
    with app.app_context():
        assert db.session.query(TrainingJob).count() == 0
    duplicate = upload(client, (30,))
    assert duplicate.json["accepted"] == []
    assert "Duplicate" in duplicate.json["rejected"][0]["reason"]
    assert upload(client, (32,)).status_code == 202
    with app.app_context():
        assert db.session.query(TrainingJob).count() == 1
    status = client.get("/api/enrollment/status").json
    assert status["count"] == 3 and not status["ready"]


def test_upload_rejects_bad_content_missing_and_multiple_faces(client):
    login(client)
    response = client.post(
        "/api/enrollment/photos",
        data={
            "photos": [
                (io.BytesIO(b"not an image"), "fake.jpg"),
                (io.BytesIO(photo_bytes(0)), "blank.png"),
                (io.BytesIO(photo_bytes(255)), "group.png"),
            ]
        },
    )
    assert len(response.json["rejected"]) == 3
    assert response.json["count"] == 0
    assert client.post("/api/enrollment/photos").status_code == 400


def test_private_photo_ownership_and_mimetype(client, app):
    login(client)
    upload(client, (30,))
    with app.app_context():
        image_id = db.session.scalar(db.select(EnrollmentImage.id))
    assert client.get(f"/enrollment/photos/{image_id}").status_code == 200
    assert client.get(f"/enrollment/photos/{image_id}").mimetype == "image/png"
    client.post("/logout")
    login(client, "bob")
    assert client.get(f"/enrollment/photos/{image_id}").status_code == 404


def test_image_size_limits(client, app):
    login(client)
    app.config["MAX_IMAGE_BYTES"] = 10
    assert "smaller" in upload(client, (30,)).json["rejected"][0]["reason"]


def test_admin_cli_and_password_reset(app):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=["create-admin", "--username", "manager", "--display-name", "Manager"],
        input="strong-demo-password\nstrong-demo-password\n",
    )
    assert result.exit_code == 0, result.output
    with app.app_context():
        user = db.session.scalar(db.select(User).where(User.username == "manager"))
        assert user.role == "admin" and user.check_password("strong-demo-password")
    result = runner.invoke(
        args=["reset-password", "manager"], input="another-long-password\nanother-long-password\n"
    )
    assert result.exit_code == 0
