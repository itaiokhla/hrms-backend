import cv2
import numpy as np
try:
    import face_recognition as fr
except (ImportError, Exception):
    fr = None
from typing import Optional, Tuple, List
import logging

logger = logging.getLogger(__name__)


class FaceProcessor:
    def __init__(self, confidence_threshold: float = 0.6):
        self.confidence_threshold = confidence_threshold
        self.face_detector = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )

    def process_image(self, image_path: str) -> Optional[Tuple[np.ndarray, float]]:
        try:
            image = fr.load_image_file(image_path)
            return self.process_frame(image)
        except Exception as e:
            logger.error(f"Error processing image: {e}")
            return None

    def process_frame(self, frame: np.ndarray) -> Optional[Tuple[np.ndarray, float]]:
        try:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if len(frame.shape) == 3 else frame

            face_locations = fr.face_locations(rgb_frame, model="hog")

            if len(face_locations) != 1:
                logger.warning(f"Expected 1 face, found {len(face_locations)}")
                return None

            face_encodings = fr.face_encodings(rgb_frame, face_locations)

            if not face_encodings:
                logger.warning("Could not generate face encoding")
                return None

            encoding = face_encodings[0]
            quality_score = self._calculate_quality_score(rgb_frame, face_locations[0])

            return encoding, quality_score

        except Exception as e:
            logger.error(f"Error processing frame: {e}")
            return None

    def _calculate_quality_score(self, frame: np.ndarray, face_location: Tuple) -> float:
        top, right, bottom, left = face_location

        face_height = bottom - top
        face_width = right - left

        frame_height, frame_width = frame.shape[:2]

        face_area_ratio = (face_height * face_width) / (frame_height * frame_width)

        min_face_size = 0.05
        max_face_size = 0.9

        if face_area_ratio < min_face_size or face_area_ratio > max_face_size:
            return 0.0

        aspect_ratio = face_width / face_height
        if aspect_ratio < 0.7 or aspect_ratio > 1.3:
            return 0.0

        face_region = frame[top:bottom, left:right]
        gray_face = cv2.cvtColor(face_region, cv2.COLOR_RGB2GRAY)
        laplacian_var = cv2.Laplacian(gray_face, cv2.CV_64F).var()

        if laplacian_var < 100:
            return min(0.5, laplacian_var / 200)

        quality_score = min(1.0, max(0.6, laplacian_var / 1000))
        return quality_score

    def compare_faces(self, known_encoding: np.ndarray, test_encoding: np.ndarray) -> float:
        try:
            distance = np.linalg.norm(known_encoding - test_encoding)

            similarity = 1 - (distance / 2.0)

            return max(0.0, min(1.0, similarity))
        except Exception as e:
            logger.error(f"Error comparing faces: {e}")
            return 0.0
