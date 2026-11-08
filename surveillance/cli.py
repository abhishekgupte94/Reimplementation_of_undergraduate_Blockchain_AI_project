import hashlib
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import click
import numpy as np
from filelock import FileLock, Timeout
from flask import current_app
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import User
from .security import validate_account
from .services.training import process_next_job, queue_training, recover_jobs
from .services.vision import FacePipeline

MODEL_URL = "https://storage.cmusatyalab.org/openface-models/nn4.small2.v1.t7"
# Published by CMU in openface/models/get-models.sh. Used for upstream integrity,
# over HTTPS; this is not a password hash or an authentication mechanism.
MODEL_MD5 = "c95bfd8cc1adf05210e979ff623013b6"


def register_cli(app):
    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--display-name", prompt=True)
    @click.password_option(confirmation_prompt=True)
    def create_admin(username, display_name, password):
        """Create an administrator. Password is prompted, never a CLI argument."""
        username = username.strip().lower()
        error = validate_account(username, display_name.strip(), password)
        if error:
            raise click.ClickException(error)
        user = User(username=username, display_name=display_name.strip(), role="admin")
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise click.ClickException("That username already exists.") from error
        click.echo(f"Administrator {username} created.")

    @app.cli.command("reset-password")
    @click.argument("username")
    @click.password_option(confirmation_prompt=True)
    def reset_password(username, password):
        """Reset a local account's password."""
        user = db.session.scalar(db.select(User).where(User.username == username.lower()))
        if not user:
            raise click.ClickException("Account not found.")
        error = validate_account(user.username, user.display_name, password)
        if error:
            raise click.ClickException(error)
        user.set_password(password)
        db.session.commit()
        click.echo("Password updated.")

    @app.cli.command("download-model")
    def download_model():
        """Download only the pretrained OpenFace embedding weights and verify them."""
        destination = Path(current_app.config["MODEL_PATH"])
        destination.parent.mkdir(parents=True, exist_ok=True)

        def verified(path):
            return path.is_file() and hashlib.md5(path.read_bytes()).hexdigest() == MODEL_MD5

        with FileLock(str(destination) + ".lock"):
            if verified(destination):
                click.echo("OpenFace weights already verified.")
                return
            temporary = destination.with_suffix(".download")
            try:
                click.echo("Downloading OpenFace weights from Carnegie Mellon University…")
                with urllib.request.urlopen(MODEL_URL, timeout=60) as source:
                    with temporary.open("wb") as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
                if not verified(temporary):
                    raise click.ClickException("OpenFace checksum mismatch; download rejected.")
                os.replace(temporary, destination)
                destination.chmod(0o600)
            except (OSError, urllib.error.URLError) as error:
                raise click.ClickException(f"Model download failed: {error}") from error
            finally:
                temporary.unlink(missing_ok=True)
        click.echo(f"Verified OpenFace weights: {destination}")

    @app.cli.command("model-smoke")
    def model_smoke():
        """Run real OpenFace inference on a synthetic crop, independent of a webcam."""
        engine = FacePipeline(current_app.config["MODEL_PATH"])
        crop = np.random.default_rng(42).integers(0, 256, (96, 96, 3), dtype=np.uint8)
        vector = engine.embedding(crop, (0, 0, 96, 96))
        click.echo(
            f"OpenFace inference OK: {vector.size} finite values; "
            f"L2 norm {np.linalg.norm(vector):.6f}."
        )

    @app.cli.command("queue-training")
    @click.argument("username")
    def queue(username):
        """Retry an enrolled participant's shared-model training."""
        user = db.session.scalar(db.select(User).where(User.username == username.lower()))
        if not user or user.role != "participant":
            raise click.ClickException("Participant not found.")
        job = queue_training(user.id)
        db.session.commit()
        click.echo(f"Queued job {job.id}.")

    @app.cli.command("trainer")
    @click.option("--once", is_flag=True, help="Process at most one queued job, then exit.")
    def trainer(once):
        """Run the separate, single-instance database-backed training worker."""
        lock = FileLock(str(current_app.config["PRIVATE_DIR"] / "locks" / "trainer.lock"))
        try:
            with lock.acquire(timeout=0):
                recover_jobs()
                click.echo("Trainer ready. Press Ctrl+C to stop.")
                while True:
                    processed = process_next_job()
                    db.session.remove()
                    if once:
                        break
                    if not processed:
                        time.sleep(1)
        except Timeout as error:
            raise click.ClickException("Another trainer is already running.") from error
        except KeyboardInterrupt:
            click.echo("Trainer stopped; interrupted jobs will recover on the next start.")
