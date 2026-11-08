import os
import uuid

import joblib
from filelock import FileLock
from flask import current_app

from ..extensions import db
from ..models import EnrollmentImage, ModelVersion, RuntimeState, TrainingJob, utcnow
from .recognition import build_classifier
from .vision import VisionError, decode_image, pipeline


def queue_training(user_id):
    existing = db.session.scalar(
        db.select(TrainingJob).where(TrainingJob.user_id == user_id, TrainingJob.status == "queued")
    )
    if existing:
        return existing
    job = TrainingJob(user_id=user_id)
    db.session.add(job)
    db.session.flush()
    return job


def recover_jobs():
    """Called only by the process holding the exclusive trainer lock."""
    for job in db.session.scalars(
        db.select(TrainingJob).where(TrainingJob.status.in_(["extracting", "training"]))
    ):
        job.status = "queued"
        job.message = "Previous trainer stopped; queued for recovery."
        job.started_at = None
    db.session.commit()


def process_next_job():
    job = db.session.scalar(
        db.select(TrainingJob).where(TrainingJob.status == "queued").order_by(TrainingJob.id)
    )
    if job is None:
        return False
    job_id = job.id
    job.status = "extracting"
    job.started_at = utcnow()
    job.message = "Extracting OpenFace embeddings."
    db.session.commit()
    temporary = None
    try:
        all_images = list(
            db.session.scalars(db.select(EnrollmentImage).order_by(EnrollmentImage.id))
        )
        grouped = {}
        for image in all_images:
            grouped.setdefault(image.user_id, []).append(image)
        eligible = {
            user_id: images
            for user_id, images in grouped.items()
            if len(images) >= current_app.config["MIN_ENROLLMENT_IMAGES"]
        }
        vectors, labels, image_ids = [], [], []
        engine = pipeline()
        for user_id, images in eligible.items():
            for image in images:
                raw = (current_app.config["PRIVATE_DIR"] / "photos" / image.filename).read_bytes()
                decoded = decode_image(raw)
                boxes = engine.detect(decoded)
                if len(boxes) != 1:
                    raise VisionError(
                        f"An enrolled photo for participant {user_id} no longer "
                        "contains exactly one detectable face."
                    )
                vectors.append(engine.embedding(decoded, boxes[0]))
                labels.append(user_id)
                image_ids.append(image.id)
        job = db.session.get(TrainingJob, job_id)
        job.status = "training"
        job.message = "Fitting the shared recognizer."
        db.session.commit()
        artifact = build_classifier(vectors, labels)
        filename = f"recognizer-{uuid.uuid4().hex}.joblib"
        destination = current_app.config["PRIVATE_DIR"] / "models" / filename
        temporary = destination.with_suffix(".tmp")
        joblib.dump(artifact, temporary)
        os.replace(temporary, destination)
        # The immutable artifact exists before its database pointer becomes visible.
        with FileLock(str(current_app.config["PRIVATE_DIR"] / "locks" / "state.lock")):
            db.session.expire_all()
            state = db.session.get(RuntimeState, 1)
            if state is None:
                state = RuntimeState(id=1)
                db.session.add(state)
            version = ModelVersion(
                filename=filename,
                mode=artifact["mode"],
                user_ids=artifact["user_ids"],
                image_ids=image_ids,
            )
            db.session.add(version)
            db.session.flush()
            state.active_model_id = version.id
            job = db.session.get(TrainingJob, job_id)
            job.status = "ready"
            job.finished_at = utcnow()
            job.message = f"Ready: {artifact['mode']} model with {len(eligible)} participant(s)."
            db.session.commit()
    except Exception as error:
        db.session.rollback()
        current_app.logger.exception("Training job %s failed", job_id)
        job = db.session.get(TrainingJob, job_id)
        job.status = "failed"
        job.message = str(error)[:500] or "Training failed. See trainer output and retry."
        job.finished_at = utcnow()
        db.session.commit()
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return True
