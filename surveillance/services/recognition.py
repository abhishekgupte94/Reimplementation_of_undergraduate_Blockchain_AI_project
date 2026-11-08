import joblib
import numpy as np
from flask import current_app
from sklearn.svm import SVC

from ..extensions import db
from ..models import ModelVersion, RuntimeState, User
from .vision import VisionError, pipeline


def build_classifier(vectors, labels):
    vectors = np.asarray(vectors, dtype=np.float32)
    labels = np.asarray(labels, dtype=np.int64)
    identities = sorted(map(int, np.unique(labels)))
    if not identities:
        raise ValueError("No eligible enrollment images were found.")
    classifier = None
    if len(identities) >= 2:
        classifier = SVC(C=1.0, kernel="linear", probability=False, random_state=42)
        classifier.fit(vectors, labels)
    return {
        "schema": 1,
        "mode": "svc" if classifier else "distance",
        "vectors": vectors,
        "labels": labels,
        "classifier": classifier,
        "user_ids": identities,
    }


def match(vector, artifact, threshold):
    # Nearest enrolled sample supports the required three-photo minimum. The same
    # unknown rejection gate is used in solo and SVC modes; SVC chooses the class.
    differences = artifact["vectors"] - vector
    np.square(differences, out=differences)
    distances = differences.sum(axis=1)
    np.sqrt(distances, out=distances)
    nearest = int(np.argmin(distances))
    score = float(distances[nearest])
    identity = int(artifact["labels"][nearest])
    if score > threshold:
        return None, score
    if artifact["classifier"] is not None:
        predicted = int(artifact["classifier"].predict(np.asarray([vector]))[0])
        if predicted != identity:
            return None, score
    return identity, score


def active_model():
    state = db.session.get(RuntimeState, 1)
    if not state or not state.active_model_id:
        raise VisionError("No trained model is available. Upload photos and run the trainer.")
    version = db.session.get(ModelVersion, state.active_model_id)
    cached = current_app.extensions.get("recognizer")
    if not cached or cached[0] != version.id:
        filename = current_app.config["PRIVATE_DIR"] / "models" / version.filename
        try:
            artifact = joblib.load(filename)
        except (OSError, ValueError, EOFError) as error:
            raise VisionError(
                "The trained model cannot be read. Queue another training job."
            ) from error
        cached = (version.id, artifact)
        current_app.extensions["recognizer"] = cached
    return version, cached[1]


def clear_recent_face():
    current_app.extensions.pop("recent_face", None)


def recognize(image, *, frame_key=None):
    """Recognize faces. The camera supplies a frame key under its capture lock."""
    version, artifact = active_model()
    threshold = current_app.config["MATCH_DISTANCE"]
    key = (frame_key, version.id, threshold)
    recent = current_app.extensions.get("recent_face") if frame_key is not None else None
    if recent is not None and recent[0] == key:
        matches = [recent[1]]
    else:
        engine = pipeline()
        matches = [
            (box, *match(engine.embedding(image, box), artifact, threshold))
            for box in engine.detect(image)
        ]
        if frame_key is not None:
            # Keep a single recognised result. The key contains a digest, no image data.
            if len(matches) == 1 and matches[0][1] is not None:
                current_app.extensions["recent_face"] = (key, matches[0])
            else:
                clear_recent_face()

    identities = {identity for _, identity, _ in matches if identity is not None}
    names = {}
    if identities:
        names = dict(
            db.session.execute(
                db.select(User.id, User.display_name).where(User.id.in_(identities))
            ).all()
        )
    faces = []
    # One observation per identity per frame, even if the detector returns repeats.
    observations = {}
    for box, identity, distance in matches:
        if identity is not None:
            observations[identity] = min(distance, observations.get(identity, float("inf")))
        faces.append(
            {
                "box": list(box),
                "user_id": identity,
                "name": names.get(identity, "Unknown"),
                "distance": round(distance, 4),
            }
        )
    return version, faces, observations
