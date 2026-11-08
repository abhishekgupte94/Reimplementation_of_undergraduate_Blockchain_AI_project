import os
import sqlite3
import threading
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_wtf.csrf import CSRFError
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.exceptions import HTTPException

from .extensions import csrf, db, login_manager, migrate


@event.listens_for(Engine, "connect")
def configure_sqlite(connection, _):
    if isinstance(connection, sqlite3.Connection):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    profile = os.getenv("APP_PROFILE", "")
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY"),
        APP_PROFILE=profile,
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        MAX_IMAGE_BYTES=5 * 1024 * 1024,
        MAX_FRAME_BYTES=1024 * 1024,
        MAX_IMAGE_PIXELS=20_000_000,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SURVEILLANCE_HTTPS") == "1",
        DISPLAY_TIMEZONE=os.getenv("DISPLAY_TIMEZONE", "Europe/Dublin"),
        MATCH_DISTANCE=float(os.getenv("MATCH_DISTANCE", "0.8")),
        PRESENCE_GRACE_SECONDS=3,
        CAPTURE_TIMEOUT_SECONDS=5,
        MIN_FRAME_SECONDS=0.3,
        MIN_ENROLLMENT_IMAGES=3,
        MODEL_PATH=os.getenv("OPENFACE_MODEL_PATH"),
        LEDGER_RPC_URL=os.getenv("LEDGER_RPC_URL", "http://127.0.0.1:8545"),
        LEDGER_CHAIN_ID=int(os.getenv("LEDGER_CHAIN_ID", "31337")),
        WEAPON_CONFIDENCE=float(os.getenv("WEAPON_CONFIDENCE", "0.55")),
        WEAPON_COOLDOWN_SECONDS=10,
        WEAPON_MODEL_DIR=os.getenv("WEAPON_MODEL_DIR"),
        WEAPON_RESULT_MAX_AGE_SECONDS=3,
        EMOTION_MODEL_DIR=os.getenv("EMOTION_MODEL_DIR"),
        EMOTION_CONFIDENCE=os.getenv("EMOTION_CONFIDENCE", "0.60"),
        EMOTION_RESULT_MAX_AGE_SECONDS=3,
    )
    if profile == "demo" and not app.config["SQLALCHEMY_DATABASE_URI"]:
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///surveillance.sqlite"
    if test_config:
        app.config.update(test_config)
    app.config.setdefault(
        "WEAPON_DETECTION_ENABLED",
        not app.testing and os.getenv("WEAPON_DETECTION_ENABLED", "true").lower() == "true",
    )
    app.config.setdefault(
        "EMOTION_RECOGNITION_ENABLED",
        not app.testing and os.getenv("EMOTION_RECOGNITION_ENABLED", "true").lower() == "true",
    )
    if not app.config["SQLALCHEMY_DATABASE_URI"]:
        raise RuntimeError("Select APP_PROFILE=demo, or configure DATABASE_URL for MySQL.")
    if not app.config["SECRET_KEY"] or app.config["SECRET_KEY"] == "REPLACE_WITH_A_RANDOM_SECRET":
        raise RuntimeError("Set SECRET_KEY to a unique random value. See README.md.")
    Path(app.instance_path).mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(app.instance_path).chmod(0o700)
    private = Path(app.config.get("PRIVATE_DIR", Path(app.instance_path) / "private"))
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("photos", "models", "locks", "chain", "backups", "mysql"):
        (private / name).mkdir(exist_ok=True, mode=0o700)
    app.config["PRIVATE_DIR"] = private
    if not app.config["MODEL_PATH"]:
        app.config["MODEL_PATH"] = private / "models" / "nn4.small2.v1.t7"

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)

    from . import models, weapon_models  # noqa: F401
    from .cli import register_cli
    from .emotion_cli import register_emotion_cli
    from .routes import admin, auth, camera, enrollment
    from .routes.weapons import bp as weapons_bp
    from .weapon_cli import register_weapon_cli

    for blueprint in (auth.bp, enrollment.bp, camera.bp, admin.bp):
        app.register_blueprint(blueprint)
    app.register_blueprint(weapons_bp)
    register_cli(app)
    register_emotion_cli(app)
    register_weapon_cli(app)
    from .ledger_cli import register_ledger_cli

    register_ledger_cli(app)

    startup_lock = threading.Lock()
    startup_cleaned = False

    @app.before_request
    def recover_previous_web_process():
        nonlocal startup_cleaned
        if not startup_cleaned:
            with startup_lock:
                if not startup_cleaned:
                    from .services.presence import recover_capture_on_server_start

                    recover_capture_on_server_start()
                    startup_cleaned = True

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return db.session.get(models.User, int(user_id))
        except (TypeError, ValueError):
            return None

    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify(error="Please log in."), 401
        return redirect(url_for("auth.login"))

    @app.get("/")
    def index():
        from flask_login import current_user

        if current_user.is_authenticated and current_user.role == "admin":
            return redirect(url_for("admin.analytics"))
        if current_user.is_authenticated:
            return redirect(url_for("enrollment.index"))
        return render_template("home.html")

    @app.template_filter("localtime")
    def localtime(value):
        if not value:
            return "—"
        return (
            value.replace(tzinfo=timezone.utc)
            .astimezone(ZoneInfo(app.config["DISPLAY_TIMEZONE"]))
            .strftime("%d %b %Y %H:%M:%S %Z")
        )

    @app.context_processor
    def context():
        return {
            "profile": app.config["APP_PROFILE"],
            "display_timezone": app.config["DISPLAY_TIMEZONE"],
        }

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error="Your form token expired. Reload this page and try again."), 400
        return "Your form token expired. Reload the page and try again.", 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return error

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    return app
