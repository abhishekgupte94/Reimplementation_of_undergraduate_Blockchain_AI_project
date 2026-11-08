import hashlib
import io
import threading
from pathlib import Path

import cv2
import numpy as np
from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError


class VisionError(ValueError):
    pass


def decode_image(raw, *, frame=False):
    limit = current_app.config["MAX_FRAME_BYTES" if frame else "MAX_IMAGE_BYTES"]
    if not raw or len(raw) > limit:
        raise VisionError(f"Image must be nonempty and smaller than {limit // 1024} KB.")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ("JPEG", "PNG"):
                raise VisionError("Only JPEG and PNG images are supported.")
            if image.width * image.height > current_app.config["MAX_IMAGE_PIXELS"]:
                raise VisionError("Image dimensions are too large.")
            image = ImageOps.exif_transpose(image).convert("RGB")
            rgb = np.array(image)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise VisionError("This file is not a valid JPEG or PNG image.") from error
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    # A shared maximum width keeps Haar detection predictable for stills and frames.
    if bgr.shape[1] > 960:
        bgr = cv2.resize(bgr, (960, round(bgr.shape[0] * 960 / bgr.shape[1])))
    return bgr


class FacePipeline:
    def __init__(self, model_path):
        cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        self.detector = cv2.CascadeClassifier(str(cascade))
        self.model_path = Path(model_path)
        self.net = None
        self.lock = threading.RLock()

    def detect(self, image):
        gray = cv2.equalizeHist(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
        with self.lock:
            boxes = self.detector.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)
            )
        return [tuple(map(int, box)) for box in boxes]

    def embedding(self, image, box):
        x, y, width, height = box
        face = image[y : y + height, x : x + width]
        if face.size == 0:
            raise VisionError("Face crop is empty.")
        blob = cv2.dnn.blobFromImage(face, 1 / 255.0, (96, 96), (0, 0, 0), swapRB=True, crop=False)
        with self.lock:
            if self.net is None:
                if not self.model_path.is_file():
                    raise VisionError(
                        "OpenFace weights are missing. Run: flask --app surveillance download-model"
                    )
                try:
                    self.net = cv2.dnn.readNetFromTorch(str(self.model_path))
                except cv2.error as error:
                    raise VisionError(
                        "Cannot load OpenFace weights. Re-run download-model."
                    ) from error
            self.net.setInput(blob)
            vector = self.net.forward().reshape(-1).astype(np.float32)
        if vector.shape != (128,) or not np.isfinite(vector).all():
            raise VisionError("OpenFace produced an invalid embedding.")
        norm = np.linalg.norm(vector)
        if norm < 1e-8:
            raise VisionError("OpenFace produced a zero embedding.")
        return vector / norm


def pipeline():
    if "face_pipeline" not in current_app.extensions:
        current_app.extensions["face_pipeline"] = FacePipeline(current_app.config["MODEL_PATH"])
    return current_app.extensions["face_pipeline"]


def image_digest(raw):
    return hashlib.sha256(raw).hexdigest()
