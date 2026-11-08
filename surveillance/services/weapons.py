"""Genuine SSD inference, isolated from the face-recognition request path."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import cv2
import numpy as np
from filelock import Timeout
from flask import current_app
from sqlalchemy.exc import OperationalError

from ..extensions import db
from ..models import CaptureSession, RuntimeState
from ..weapon_models import WeaponAlert
from . import presence
from .ledger import audit_source, audit_write_is_busy, reserve_audit_write

MODEL_TAG = "ssd-mobilenet-v2-openimages-v4-pen-2018-12-12"
TARGET_LABEL = "Pen"
PEN_CLASS_ID = 569  # Open Images V4 label /m/0k1tl
INPUT_SIZE = (300, 300)
MODEL_FILES = {
    "frozen_inference_graph.pb": "150f3eb77b741ed24e1a19559589205ccd616b059fc976c8b8d5cefc126bf86b",
    "opencv.pbtxt": "9a4e67d6a7c3169cec22bb7fc27c776265d6c3dc522b8aa2afa222a3c99320ef",
}
detector_init_lock = threading.Lock()


def model_directory():
    return Path(
        current_app.config.get("WEAPON_MODEL_DIR")
        or (current_app.config["PRIVATE_DIR"] / "models" / "weapons")
    )


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WeaponModelError(ValueError):
    pass


class SSDWeaponDetector:
    def __init__(self, directory, confidence=0.55):
        self.directory = Path(directory)
        self.confidence = float(confidence)
        if not 0.3 <= self.confidence < 1:
            raise WeaponModelError("SSD confidence must be at least 0.3 and below 1.")
        self.net = None
        self.lock = threading.Lock()

    def load(self):
        if self.net is not None:
            return
        for name, expected in MODEL_FILES.items():
            path = self.directory / name
            if not path.is_file():
                raise WeaponModelError(
                    "SSD weights unavailable; run flask --app surveillance download-weapons."
                )
            if sha256(path) != expected:
                raise WeaponModelError(f"SSD file integrity check failed: {name}.")
        graph, config = (str(self.directory / name) for name in MODEL_FILES)
        net = cv2.dnn.readNetFromTensorflow(graph, config)
        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.net = net

    def detect(self, image, *, frame_size=None):
        with self.lock:
            self.load()
            # The imported graph applies normalization to RGB pixels internally.
            blob = cv2.dnn.blobFromImage(image, size=INPUT_SIZE, swapRB=True)
            self.net.setInput(blob)
            output = self.net.forward()
        return self.decode(output, *(frame_size or (image.shape[1], image.shape[0])))

    def decode(self, output, width, height):
        if (
            output.ndim != 4
            or output.shape[:2] != (1, 1)
            or output.shape[3] != 7
            or not np.isfinite(output).all()
        ):
            raise WeaponModelError("SSD returned invalid detection output.")
        # OpenCV's DetectionOutput already applies per-class NMS.
        detections = []
        for batch, class_id, confidence, x1, y1, x2, y2 in output[0, 0]:
            if batch != 0 or class_id != PEN_CLASS_ID or not self.confidence <= confidence <= 1:
                continue
            left = max(0, min(width, round(float(x1) * width)))
            top = max(0, min(height, round(float(y1) * height)))
            right = max(0, min(width, round(float(x2) * width)))
            bottom = max(0, min(height, round(float(y2) * height)))
            if right > left and bottom > top:
                detections.append(
                    {
                        "label": TARGET_LABEL,
                        "confidence": round(float(confidence), 5),
                        "box": [left, top, right - left, bottom - top],
                    }
                )
        return detections


def persist_alerts(metadata, detections):
    """Record only results still belonging to the active room/capture generation."""
    strongest = max(
        (detection for detection in detections if detection["label"] == TARGET_LABEL),
        key=lambda detection: detection["confidence"],
        default=None,
    )
    if strongest is None:
        return []
    try:
        # Serialize only the capture check and alert commit with camera changes.
        # A busy face request takes priority over this optional sample.
        with presence.capture_state_lock(timeout=0), db.session.begin():
            timestamp = presence.now()
            maximum_age = current_app.config["WEAPON_RESULT_MAX_AGE_SECONDS"]
            if not 0 <= (timestamp - metadata["observed_at"]).total_seconds() <= maximum_age:
                return []
            reserve_audit_write()
            capture_timeout = timedelta(seconds=current_app.config["CAPTURE_TIMEOUT_SECONDS"])
            active_capture = db.session.scalar(
                db.select(CaptureSession.id)
                .join(RuntimeState, RuntimeState.active_capture_id == CaptureSession.id)
                .where(
                    RuntimeState.id == 1,
                    CaptureSession.id == metadata["capture_id"],
                    CaptureSession.generation == metadata["generation"],
                    CaptureSession.room == metadata["room"],
                    CaptureSession.ended_at.is_(None),
                    CaptureSession.last_frame_at >= timestamp - capture_timeout,
                )
            )
            if active_capture is None:
                return []
            cooldown = timedelta(seconds=current_app.config["WEAPON_COOLDOWN_SECONDS"])
            previous = db.session.scalar(
                db.select(WeaponAlert.id)
                .where(
                    WeaponAlert.capture_id == active_capture,
                    WeaponAlert.generation == metadata["generation"],
                    WeaponAlert.label == TARGET_LABEL,
                    WeaponAlert.observed_at > metadata["observed_at"] - cooldown,
                )
                .limit(1)
            )
            if previous is not None:
                return []
            alert = WeaponAlert(**metadata, **strongest, model_tag=MODEL_TAG)
            db.session.add(alert)
            audit_source(alert, "weapon.detected")
            return [alert.id]
    except Timeout:
        return []
    except OperationalError as error:
        if audit_write_is_busy(error):
            return []
        raise


class AsyncWeaponDetector:
    def __init__(self, app):
        self.app = app
        self.detector = SSDWeaponDetector(
            model_directory(), app.config.get("WEAPON_CONFIDENCE", 0.55)
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="weapon-ssd")
        self.future = None
        self.latest = None
        self.lock = threading.Lock()

    def _infer(self, image, metadata, frame_size):
        try:
            detections = self.detector.detect(image, frame_size=frame_size)
            with self.app.app_context():
                persist_alerts(metadata, detections)
            result = {"status": "ready", "detections": detections}
        except Exception as error:
            self.app.logger.warning("SSD sample failed: %s", error)
            result = {"status": "unavailable", "detections": []}
            if isinstance(error, WeaponModelError):
                result["message"] = str(error)
        return {**result, "metadata": metadata, "frame_size": frame_size}

    def submit(self, image, metadata):
        frame_size = (image.shape[1], image.shape[0])
        with self.lock:
            if self.future and self.future.done():
                self.latest = self.future.result()
                self.future = None
            latest = self.latest
            matching = bool(
                latest
                and latest["frame_size"] == frame_size
                and all(
                    latest["metadata"][key] == metadata[key]
                    for key in ("capture_id", "generation", "room")
                )
            )
            if matching:
                age = (metadata["observed_at"] - latest["metadata"]["observed_at"]).total_seconds()
                matching = 0 <= age <= self.app.config["WEAPON_RESULT_MAX_AGE_SECONDS"]
            if self.future is None:
                # Retain only one SSD-sized buffer even if a client uploads a large frame.
                sample = cv2.resize(image, INPUT_SIZE)
                self.future = self.executor.submit(self._infer, sample, dict(metadata), frame_size)
            if not matching:
                return {"status": "loading", "detections": [], "model": MODEL_TAG}
            return {
                key: value for key, value in latest.items() if key not in ("metadata", "frame_size")
            } | {
                "model": MODEL_TAG,
                "frame_sequence": latest["metadata"]["frame_sequence"],
                "observed_at": latest["metadata"]["observed_at"].isoformat() + "Z",
            }


def submit_frame(image, metadata):
    if not current_app.config.get("WEAPON_DETECTION_ENABLED", not current_app.testing):
        return {"status": "disabled", "detections": []}
    try:
        if "weapon_detector" not in current_app.extensions:
            # Factory creation is cheap; heavyweight loading happens only in its executor.
            with detector_init_lock:
                if "weapon_detector" not in current_app.extensions:
                    current_app.extensions["weapon_detector"] = AsyncWeaponDetector(
                        current_app._get_current_object()
                    )
        return current_app.extensions["weapon_detector"].submit(image, metadata)
    except Exception as error:
        current_app.logger.warning("Cannot queue SSD inference: %s", error)
        return {
            "status": "unavailable",
            "detections": [],
            "message": "SSD inference could not be queued; face tracking continues.",
        }
