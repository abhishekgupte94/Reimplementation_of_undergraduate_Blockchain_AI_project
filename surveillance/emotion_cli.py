"""Install and smoke-test the pinned OpenCV Zoo expression models."""

import os
import tempfile
from pathlib import Path

import click
import cv2
import numpy as np
from filelock import FileLock, Timeout

from .services.emotions import (
    CROP_SIZE,
    FER_FILE,
    INPUT_SIZE,
    LABELS,
    MODEL_FILES,
    YUNET_FILE,
    FacialExpressionRecognizer,
    face_crop,
    model_directory,
)
from .services.vision import pipeline
from .services.weapons import sha256
from .weapon_cli import download_verified

ZOO_URL = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
MODEL_URLS = {
    FER_FILE: f"{ZOO_URL}/facial_expression_recognition/{FER_FILE}",
    YUNET_FILE: f"{ZOO_URL}/face_detection_yunet/{YUNET_FILE}",
}


def smoke_models(recognizer):
    recognizer.logits(np.zeros((*INPUT_SIZE, 3), dtype=np.uint8))
    recognizer.align(np.zeros((*CROP_SIZE, 3), dtype=np.uint8))


def register_emotion_cli(app):
    @app.cli.command("download-emotions")
    @click.option("--force", is_flag=True, help="Download both verified files again.")
    def download(force):
        """Install the OpenCV Zoo expression classifier and YuNet landmark model."""
        directory = model_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with FileLock(str(directory / ".install.lock"), timeout=0):
                if force or not all(
                    (directory / name).is_file() and sha256(directory / name) == expected
                    for name, expected in MODEL_FILES.items()
                ):
                    with tempfile.TemporaryDirectory(
                        prefix=".emotion-install-", dir=directory
                    ) as temp:
                        staging = Path(temp)
                        for filename, expected in MODEL_FILES.items():
                            download_verified(MODEL_URLS[filename], staging / filename, expected)
                        smoke_models(FacialExpressionRecognizer(staging))
                        for filename in MODEL_FILES:
                            os.replace(staging / filename, directory / filename)
            click.echo("Verified OpenCV Zoo expression and landmark models installed.")
        except Timeout as error:
            raise click.ClickException(
                "An expression model installation is already running."
            ) from error
        except (OSError, ValueError, cv2.error) as error:
            raise click.ClickException(f"Expression model installation failed: {error}") from error

    @app.cli.command("emotion-smoke")
    @click.option("--image", type=click.Path(exists=True, dir_okay=False), help="Local face image.")
    @click.option("--expect", type=click.Choice([label for label in LABELS if label]))
    def smoke(image, expect):
        """Check both OpenCV models without writing observation or ledger records."""
        if expect and not image:
            raise click.UsageError("--expect requires --image.")
        try:
            recognizer = FacialExpressionRecognizer(
                model_directory(), app.config["EMOTION_CONFIDENCE"]
            )
            smoke_models(recognizer)
            estimates = []
            if image:
                frame = cv2.imread(image)
                if frame is None:
                    raise click.ClickException("Could not read the test image.")
                for box in pipeline().detect(frame):
                    crop = face_crop(frame, box)
                    if crop is not None and (estimate := recognizer.predict(crop)):
                        estimates.append(estimate["emotion"])
        except (OSError, ValueError, cv2.error) as error:
            raise click.ClickException(str(error)) from error
        click.echo("Expression classifier and landmark inference passed.")
        if image:
            click.echo("Estimated expressions: " + (", ".join(estimates) or "none"))
        if expect and expect not in estimates:
            raise click.ClickException(f"Expected {expect} was not estimated.")
