"""Install and smoke-test the Open Images SSD used by the pen demo."""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

import click
import cv2
import numpy as np
from filelock import FileLock, Timeout

from .services.weapons import (
    INPUT_SIZE,
    MODEL_FILES,
    TARGET_LABEL,
    SSDWeaponDetector,
    model_directory,
    sha256,
)

ARCHIVE_ROOT = "ssd_mobilenet_v2_oid_v4_2018_12_12"
ARCHIVE_URL = (
    "https://storage.googleapis.com/download.tensorflow.org/models/object_detection/"
    f"{ARCHIVE_ROOT}.tar.gz"
)
ARCHIVE_SHA256 = "8dd82cc52625eb9b43c6ed8050868fe1e78f878c6f028519fc731b3c7e9035f7"
OPENCV_SOURCE = "https://raw.githubusercontent.com/opencv/opencv/4.12.0/samples/dnn"
CONVERTER_FILES = {
    "tf_text_graph_ssd.py": "8ce49e940837017b23ed3f2195e3c4b5a71ada6c0f66d351e293a012ee533909",
    "tf_text_graph_common.py": "cb4ff58a824e9d4613c11a4995c18f5e083509b17af5f8889be1e135b9cf8b7f",
}
SAMPLE_NAME = "pen-test.jpg"
SAMPLE_URL = "https://open-images-dataset.s3.amazonaws.com/validation/0a8fc50cd3e3df1a.jpg"
SAMPLE_SHA256 = "560fd16f771c693fb380d93c944cded6f5d6d492b072b1c851eb99760d485556"


def download_verified(url, destination, expected):
    temporary = destination.with_suffix(destination.suffix + ".download")
    try:
        with urllib.request.urlopen(url, timeout=60) as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if sha256(temporary) != expected:
            raise click.ClickException(f"Checksum mismatch for {destination.name}; file rejected.")
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def install_model(directory, staging):
    archive = directory / "upstream.tar.gz"
    if not archive.is_file() or sha256(archive) != ARCHIVE_SHA256:
        archive = staging / "upstream.tar.gz"
        click.echo("Downloading the TensorFlow Open Images SSD…")
        download_verified(ARCHIVE_URL, archive, ARCHIVE_SHA256)

    graph_name = "frozen_inference_graph.pb"
    with tarfile.open(archive) as package:
        for name in (graph_name, "pipeline.config"):
            with package.extractfile(f"{ARCHIVE_ROOT}/{name}") as source:
                with (staging / name).open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)

    for name, expected in CONVERTER_FILES.items():
        cached = directory / name
        if cached.is_file() and sha256(cached) == expected:
            shutil.copyfile(cached, staging / name)
        else:
            download_verified(f"{OPENCV_SOURCE}/{name}", staging / name, expected)

    conversion = subprocess.run(
        [
            sys.executable,
            str(staging / "tf_text_graph_ssd.py"),
            "--input",
            str(staging / graph_name),
            "--config",
            str(staging / "pipeline.config"),
            "--output",
            str(staging / "opencv.pbtxt"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if conversion.returncode:
        raise click.ClickException(f"SSD graph conversion failed: {conversion.stderr[-1500:]}")

    SSDWeaponDetector(staging).detect(np.zeros((*INPUT_SIZE, 3), dtype=np.uint8))
    for name in MODEL_FILES:
        (staging / name).chmod(0o600)
        os.replace(staging / name, directory / name)


def register_weapon_cli(app):
    @app.cli.command("download-weapons")
    @click.option("--with-sample", is_flag=True, help="Also install the labeled pen example.")
    @click.option("--force", is_flag=True, help="Rebuild the verified model files.")
    def download(with_sample, force):
        """Install the verified Open Images SSD for the pen simulation."""
        directory = model_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with FileLock(str(directory / ".install.lock"), timeout=0):
                if force or not all(
                    (directory / name).is_file() and sha256(directory / name) == expected
                    for name, expected in MODEL_FILES.items()
                ):
                    with tempfile.TemporaryDirectory(prefix=".ssd-install-", dir=directory) as temp:
                        install_model(directory, directory / temp)
                if with_sample:
                    sample = directory / SAMPLE_NAME
                    if not sample.is_file() or sha256(sample) != SAMPLE_SHA256:
                        download_verified(SAMPLE_URL, sample, SAMPLE_SHA256)
            click.echo("Verified SSD model installed for Pen detection.")
        except Timeout as error:
            raise click.ClickException("An SSD installation is already running.") from error
        except (
            OSError,
            KeyError,
            ValueError,
            cv2.error,
            tarfile.TarError,
            subprocess.TimeoutExpired,
        ) as error:
            raise click.ClickException(f"SSD installation failed: {error}") from error

    @app.cli.command("weapon-smoke")
    @click.option(
        "--image",
        type=click.Path(exists=True, dir_okay=False),
        help="Optional local image. The default is an all-black frame.",
    )
    @click.option("--expect", type=click.Choice([TARGET_LABEL]), help="Require a pen detection.")
    def smoke(image, expect):
        """Run actual SSD inference without touching presence or alert records."""
        frame = cv2.imread(image) if image else np.zeros((480, 640, 3), dtype=np.uint8)
        if frame is None:
            raise click.ClickException("Could not read the test image.")
        try:
            detector = SSDWeaponDetector(model_directory(), app.config["WEAPON_CONFIDENCE"])
            results = detector.detect(frame)
        except (OSError, ValueError, cv2.error) as error:
            raise click.ClickException(str(error)) from error
        click.echo(f"SSD inference OK. {len(results)} pen detection(s): {results}")
        if expect and not results:
            raise click.ClickException(f"Expected {expect} was not detected.")
