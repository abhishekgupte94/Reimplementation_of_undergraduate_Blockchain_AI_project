"""Optional facial expression inference using the existing OpenCV runtime."""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from flask import current_app

from .emotion_observations import persist_emotions
from .weapons import sha256

MODEL_TAG = "opencv-zoo-mobilefacenet-2022july"
FER_FILE = "facial_expression_recognition_mobilefacenet_2022july.onnx"
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
MODEL_FILES = {
    FER_FILE: "4f61307602fc089ce20488a31d4e4614e3c9753a7d6c41578c854858b183e1a9",
    YUNET_FILE: "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
}
LABELS = ("angry", None, None, "happy", "neutral", "sad", "surprised")
INPUT_SIZE = (112, 112)
CROP_SIZE = (160, 160)
MAX_FACES = 4
SAMPLE_INTERVAL = 1
RETRY_INTERVAL = 5
SCOPE_KEYS = ("capture_id", "generation", "room", "model_id")
LANDMARKS = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)
detector_init_lock = threading.Lock()


class EmotionModelError(ValueError):
    pass


def model_directory():
    return Path(
        current_app.config["EMOTION_MODEL_DIR"]
        or (current_app.config["PRIVATE_DIR"] / "models" / "emotions")
    )


def face_crop(image, box):
    """Copy only a bounded, padded crop of an already recognised Haar face."""
    x, y, width, height = box
    pad_x, pad_y = width // 8, height // 8
    left, top = max(0, x - pad_x), max(0, y - pad_y)
    right = min(image.shape[1], x + width + pad_x)
    bottom = min(image.shape[0], y + height + pad_y)
    if right <= left or bottom <= top:
        return None
    return cv2.resize(image[top:bottom, left:right], CROP_SIZE)


class FacialExpressionRecognizer:
    """Each instance is owned by one worker or one CLI invocation."""

    def __init__(self, directory, confidence=0.6):
        self.directory = Path(directory)
        self.confidence = float(confidence)
        if not 0.5 <= self.confidence < 1:
            raise EmotionModelError("Expression confidence must be at least 0.5 and below 1.")
        self.net = None
        self.landmark_detector = None

    def load(self):
        if self.net is not None:
            return
        for filename, expected in MODEL_FILES.items():
            path = self.directory / filename
            if not path.is_file():
                raise EmotionModelError(
                    "Expression weights missing. Run flask --app surveillance download-emotions."
                )
            if sha256(path) != expected:
                raise EmotionModelError("Expression weights failed integrity verification.")
        detector = cv2.FaceDetectorYN.create(str(self.directory / YUNET_FILE), "", CROP_SIZE, 0.85)
        net = cv2.dnn.readNetFromONNX(str(self.directory / FER_FILE))
        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.landmark_detector, self.net = detector, net

    def align(self, crop):
        self.load()
        _, faces = self.landmark_detector.detect(crop)
        if faces is None or len(faces) != 1 or not np.isfinite(faces).all():
            return None
        x, y, width, height = faces[0, :4]
        center = np.array([x + width / 2, y + height / 2])
        size = np.asarray(CROP_SIZE)
        if min(width, height) < 64 or not ((size / 4 <= center) & (center <= 3 * size / 4)).all():
            return None
        # YuNet's five landmarks are ordered as the upstream FER template expects.
        points = faces[0, 4:14].reshape(5, 2)
        transform, inliers = cv2.estimateAffinePartial2D(points, LANDMARKS, method=cv2.LMEDS)
        if transform is None or np.count_nonzero(inliers) < 4:
            return None
        return cv2.warpAffine(crop, transform, INPUT_SIZE)

    def logits(self, aligned):
        self.load()
        blob = cv2.dnn.blobFromImage(
            aligned, 1 / 127.5, INPUT_SIZE, (127.5, 127.5, 127.5), swapRB=True
        )
        self.net.setInput(blob, "data")
        scores = self.net.forward("label").reshape(-1)
        if scores.shape != (len(LABELS),) or not np.isfinite(scores).all():
            raise EmotionModelError("The expression model returned invalid scores.")
        return scores

    def estimate(self, scores):
        winner = int(np.argmax(scores))
        label = LABELS[winner]
        confidence = float(1 / np.exp(scores - scores[winner]).sum())
        if label is None or confidence < self.confidence:
            return None
        return {"emotion": label, "confidence": confidence, "model_version": MODEL_TAG}

    def predict(self, crop):
        aligned = self.align(crop)
        return self.estimate(self.logits(aligned)) if aligned is not None else None


def matching_scope(first, second):
    return all(first[key] == second[key] for key in SCOPE_KEYS)


class AsyncEmotionDetector:
    def __init__(self, app, *, recognizer=None):
        self.app = app
        self.recognizer = recognizer or FacialExpressionRecognizer(
            model_directory(), app.config["EMOTION_CONFIDENCE"]
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="face-expression")
        self.future = None
        self.latest = None
        self.submitted = None
        self.lock = threading.Lock()

    def _infer(self, samples, metadata):
        try:
            estimates = {}
            for user_id, crop in samples.items():
                estimate = self.recognizer.predict(crop)
                if estimate:
                    estimates[user_id] = estimate
        except (EmotionModelError, OSError, cv2.error) as error:
            message = str(error) if isinstance(error, EmotionModelError) else "Model unavailable."
            return metadata, {"status": "unavailable", "estimates": {}, "message": message}
        except Exception as error:
            self.app.logger.warning("Expression inference failed: %s", type(error).__name__)
            return metadata, {"status": "unavailable", "estimates": {}}
        if estimates:
            try:
                with self.app.app_context():
                    persist_emotions(metadata, estimates)
            except Exception as error:
                self.app.logger.warning("Expression audit write failed: %s", type(error).__name__)
        return metadata, {"status": "ready", "estimates": estimates}

    def submit(self, image, faces, metadata):
        counts = Counter(face["user_id"] for face in faces)
        unique = [face for face in faces if counts[face["user_id"]] == 1]
        with self.lock:
            if self.future and self.future.done():
                self.latest = self.future.result()
                self.future = None
            if not unique:
                return {"status": "waiting", "estimates": {}}

            result = {"status": "sampling", "estimates": {}}
            interval = SAMPLE_INTERVAL
            if self.latest and matching_scope(self.latest[0], metadata):
                previous, cached = self.latest
                age = (metadata["observed_at"] - previous["observed_at"]).total_seconds()
                if 0 <= age <= self.app.config["EMOTION_RESULT_MAX_AGE_SECONDS"]:
                    result = {
                        **cached,
                        "estimates": {
                            user_id: estimate
                            for user_id, estimate in cached["estimates"].items()
                            if counts[user_id] == 1
                        },
                    }
                if cached["status"] == "unavailable":
                    interval = RETRY_INTERVAL
                    if age >= 0:
                        result = cached

            if self.future is None and (
                self.submitted is None
                or not matching_scope(self.submitted, metadata)
                or (metadata["observed_at"] - self.submitted["observed_at"]).total_seconds()
                >= interval
            ):
                candidates = sorted(unique, key=lambda face: face["distance"])[:MAX_FACES]
                samples = {}
                for face in candidates:
                    crop = face_crop(image, face["box"])
                    if crop is not None:
                        samples[face["user_id"]] = crop
                if samples:
                    self.submitted = dict(metadata)
                    self.future = self.executor.submit(self._infer, samples, self.submitted)
            return result


def submit_frame(image, faces, metadata):
    if not current_app.config["EMOTION_RECOGNITION_ENABLED"]:
        return {"status": "disabled", "estimates": {}}
    try:
        if "emotion_detector" not in current_app.extensions:
            with detector_init_lock:
                if "emotion_detector" not in current_app.extensions:
                    current_app.extensions["emotion_detector"] = AsyncEmotionDetector(
                        current_app._get_current_object()
                    )
        return current_app.extensions["emotion_detector"].submit(image, faces, metadata)
    except Exception as error:
        current_app.logger.warning("Cannot queue expression inference: %s", type(error).__name__)
        return {"status": "unavailable", "estimates": {}}
