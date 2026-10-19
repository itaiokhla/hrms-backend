"""Face encoding and matching.

Uses the optional `face_recognition` (dlib) package. When it is not
installed the engine reports itself unavailable and callers return a clear
503 instead of failing with internal errors.
"""

import base64
import logging
from typing import Iterable, Optional, Tuple

import numpy as np

try:  # pragma: no cover - depends on optional native dependency
    import face_recognition as fr
except Exception:  # ImportError or dlib load failures
    fr = None

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

logger = logging.getLogger(__name__)


class FaceEngineUnavailable(RuntimeError):
    pass


class FaceNotDetected(ValueError):
    pass


def available() -> bool:
    return fr is not None and cv2 is not None


def _decode_image(data: bytes) -> np.ndarray:
    if not available():
        raise FaceEngineUnavailable()
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise FaceNotDetected("Upload a valid image.")
    # OpenCV decodes as BGR; face_recognition expects RGB.
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def encode(data: bytes) -> str:
    """Return a storable encoding for the single face in the image."""
    rgb = _decode_image(data)
    locations = fr.face_locations(rgb, model="hog")
    if len(locations) != 1:
        raise FaceNotDetected(
            "No face detected. Face the camera in good light." if not locations
            else "More than one face detected. Only one person should be in frame."
        )
    encodings = fr.face_encodings(rgb, locations)
    if not encodings:
        raise FaceNotDetected("Could not read facial features. Try again.")
    return base64.b64encode(np.asarray(encodings[0], dtype=np.float64).tobytes()).decode("ascii")


def _decode_encoding(value: str) -> np.ndarray:
    raw = base64.b64decode(value.encode("ascii"))
    # Encodings are stored as float64 (128 values); accept legacy float32 too.
    dtype = np.float64 if len(raw) == 128 * 8 else np.float32
    return np.frombuffer(raw, dtype=dtype).astype(np.float64)


def best_match(probe: str, candidates: Iterable[Tuple[int, str]], threshold: float) -> Optional[Tuple[int, float]]:
    """Return (candidate_id, distance) of the closest face within threshold."""
    probe_vec = _decode_encoding(probe)
    best: Optional[Tuple[int, float]] = None
    for candidate_id, encoded in candidates:
        try:
            distance = float(np.linalg.norm(_decode_encoding(encoded) - probe_vec))
        except (ValueError, TypeError):
            logger.warning("Skipping unreadable face encoding for candidate %s", candidate_id)
            continue
        if best is None or distance < best[1]:
            best = (candidate_id, distance)
    if best is not None and best[1] <= threshold:
        return best
    return None


def similarity(distance: float) -> float:
    return round(max(0.0, 1.0 - distance), 4)
