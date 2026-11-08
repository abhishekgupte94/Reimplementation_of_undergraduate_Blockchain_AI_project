import uuid

from filelock import FileLock
from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from flask_login import current_user
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import EnrollmentImage, ModelVersion, RuntimeState, TrainingJob
from ..security import role_required
from ..services.training import queue_training
from ..services.vision import VisionError, decode_image, image_digest, pipeline

bp = Blueprint("enrollment", __name__)


@bp.get("/enrollment")
@role_required("participant")
def index():
    images = list(
        db.session.scalars(
            db.select(EnrollmentImage)
            .where(EnrollmentImage.user_id == current_user.id)
            .order_by(EnrollmentImage.id)
        )
    )
    return render_template("enrollment.html", images=images)


@bp.get("/enrollment/photos/<int:image_id>")
@role_required("participant")
def photo(image_id):
    image = db.session.get(EnrollmentImage, image_id)
    if image is None or image.user_id != current_user.id:
        abort(404)
    return send_file(current_app.config["PRIVATE_DIR"] / "photos" / image.filename)


@bp.post("/api/enrollment/photos")
@role_required("participant")
def upload():
    files = request.files.getlist("photos")
    if not files or len(files) > 20:
        abort(400, "Select between 1 and 20 photos per upload.")
    accepted, rejected = [], []
    with FileLock(str(current_app.config["PRIVATE_DIR"] / "locks" / "enrollment.lock")):
        for file in files:
            raw = file.read(current_app.config["MAX_IMAGE_BYTES"] + 1)
            original_name = secure_filename(file.filename or "photo")[:255] or "photo"
            try:
                decoded = decode_image(raw)
                digest = image_digest(raw)
                if db.session.scalar(
                    db.select(EnrollmentImage.id).where(
                        EnrollmentImage.user_id == current_user.id, EnrollmentImage.digest == digest
                    )
                ):
                    raise VisionError("Duplicate photograph; upload a different image.")
                faces = pipeline().detect(decoded)
                if len(faces) != 1:
                    raise VisionError(
                        f"Found {len(faces)} faces; each photo must show exactly one."
                    )
                extension = "png" if raw.startswith(b"\x89PNG") else "jpg"
                filename = f"{uuid.uuid4().hex}.{extension}"
                path = current_app.config["PRIVATE_DIR"] / "photos" / filename
                path.write_bytes(raw)
                path.chmod(0o600)
                image = EnrollmentImage(
                    user_id=current_user.id,
                    filename=filename,
                    digest=digest,
                    original_name=original_name,
                )
                db.session.add(image)
                db.session.flush()
                accepted.append(original_name)
            except VisionError as error:
                rejected.append({"name": original_name, "reason": str(error)})
        count = db.session.query(EnrollmentImage).filter_by(user_id=current_user.id).count()
        job = None
        if accepted and count >= current_app.config["MIN_ENROLLMENT_IMAGES"]:
            job = queue_training(current_user.id)
        db.session.commit()
    return jsonify(
        accepted=accepted, rejected=rejected, count=count, job_id=job.id if job else None
    ), 202 if job else 200


@bp.get("/api/enrollment/status")
@role_required("participant")
def status():
    images = list(
        db.session.scalars(
            db.select(EnrollmentImage).where(EnrollmentImage.user_id == current_user.id)
        )
    )
    job = db.session.scalar(
        db.select(TrainingJob)
        .where(TrainingJob.user_id == current_user.id)
        .order_by(TrainingJob.id.desc())
    )
    state = db.session.get(RuntimeState, 1)
    version = (
        db.session.get(ModelVersion, state.active_model_id)
        if (state and state.active_model_id)
        else None
    )
    ready = bool(version and current_user.id in version.user_ids)
    return jsonify(
        count=len(images),
        minimum=current_app.config["MIN_ENROLLMENT_IMAGES"],
        ready=ready,
        mode=version.mode if ready else None,
        model_current=bool(ready and all(i.id in version.image_ids for i in images)),
        job={"id": job.id, "status": job.status, "message": job.message} if job else None,
    )


@bp.post("/api/enrollment/retrain")
@role_required("participant")
def retrain():
    count = db.session.query(EnrollmentImage).filter_by(user_id=current_user.id).count()
    if count < current_app.config["MIN_ENROLLMENT_IMAGES"]:
        abort(400, "Upload at least three valid photographs first.")
    job = queue_training(current_user.id)
    db.session.commit()
    return jsonify(job_id=job.id), 202
