from flask import Blueprint, abort, current_app, jsonify, render_template, request
from flask_login import current_user

from ..extensions import db
from ..models import ModelVersion, RuntimeState
from ..security import json_object, role_required
from ..services import presence
from ..services.emotions import submit_frame as submit_emotions
from ..services.recognition import clear_recent_face, recognize
from ..services.vision import VisionError, decode_image, image_digest
from ..services.weapons import submit_frame

bp = Blueprint("camera", __name__)


def room_number(value):
    if isinstance(value, bool) or str(value) not in ("1", "2"):
        abort(400, "Room must be 1 or 2.")
    return int(value)


@bp.get("/cameras")
@role_required("participant")
def index():
    return render_template("cameras.html")


@bp.post("/api/camera/start")
@role_required("participant")
def start():
    data = json_object()
    room = room_number(data.get("room"))
    state = db.session.get(RuntimeState, 1)
    version = (
        db.session.get(ModelVersion, state.active_model_id)
        if (state and state.active_model_id)
        else None
    )
    if not version or current_user.id not in version.user_ids:
        abort(409, "Your face profile is not trained yet. Complete enrollment first.")
    return jsonify(presence.start_capture(current_user.id, room))


@bp.post("/api/camera/switch")
@role_required("participant")
def switch():
    user_id = current_user.id
    data = json_object()
    room = room_number(data.get("room"))
    with presence.locked_state() as state:
        capture = presence.require_capture(state, user_id, data)
        capture.arrival_candidates = None
        capture.room = room
        capture.generation += 1
        capture.last_sequence = -1
        capture.last_frame_at = presence.now()
        clear_recent_face()
        payload = presence.capture_payload(capture)
    return jsonify(payload)


@bp.post("/api/camera/frame")
@role_required("participant")
def frame():
    user_id = current_user.id
    data = request.form
    upload = request.files.get("frame")
    if upload is None:
        abort(400, "A JPEG or PNG frame is required.")
    raw = upload.read(current_app.config["MAX_FRAME_BYTES"] + 1)
    try:
        image = decode_image(raw, frame=True)
    except VisionError as error:
        abort(400, str(error))
    try:
        sequence = int(data.get("sequence", ""))
    except (TypeError, ValueError):
        abort(400, "A nonnegative frame sequence is required.")
    if sequence < 0 or sequence > 2**31 - 1:
        abort(400, "Invalid frame sequence.")
    with presence.locked_state() as state:
        capture = presence.require_capture(state, user_id, data)
        if sequence <= capture.last_sequence:
            abort(409, "Duplicate or out-of-order frame discarded.")
        timestamp = presence.now()
        if (
            capture.last_sequence >= 0
            and (timestamp - capture.last_frame_at).total_seconds()
            < current_app.config["MIN_FRAME_SECONDS"]
        ):
            abort(429, "Frames are arriving too quickly.")
        try:
            if (timestamp - capture.last_frame_at).total_seconds() > current_app.config[
                "PRESENCE_GRACE_SECONDS"
            ]:
                clear_recent_face()
            frame_key = (capture.id, capture.generation, image_digest(raw))
            version, faces, observations = recognize(image, frame_key=frame_key)
        except VisionError as error:
            abort(503, str(error))
        capture.last_sequence = sequence
        confirmed = presence.record_observations(capture, version.id, observations, timestamp)
        payload = {
            "faces": faces,
            "room": capture.room,
            "generation": capture.generation,
            "sequence": sequence,
            "mode": version.mode,
            "width": image.shape[1],
            "height": image.shape[0],
            "status": "running",
        }
        weapon_metadata = {
            "capture_id": capture.id,
            "generation": capture.generation,
            "frame_sequence": sequence,
            "room": capture.room,
            "observed_at": timestamp,
        }
        emotion_metadata = {**weapon_metadata, "model_id": version.id}
    # Optional inference runs outside the camera lock, after arrivals commit.
    payload["weapons"] = submit_frame(image, weapon_metadata)
    emotion = submit_emotions(
        image, [face for face in faces if face["user_id"] in confirmed], emotion_metadata
    )
    estimates = emotion["estimates"]
    for face in faces:
        if estimate := estimates.get(face["user_id"]):
            face["emotion"] = estimate["emotion"]
    payload["emotion"] = {key: value for key, value in emotion.items() if key != "estimates"}
    return jsonify(payload)


@bp.post("/api/camera/stop")
@role_required("participant")
def stop():
    user_id = current_user.id
    data = json_object() if request.is_json else request.form
    with presence.locked_state() as state:
        capture = presence.expire_capture(state, presence.now())
        if capture is not None:
            capture = presence.require_capture(state, user_id, data)
            presence.finish_capture(state, capture, presence.now())
            clear_recent_face()
    return jsonify(status="stopped")
