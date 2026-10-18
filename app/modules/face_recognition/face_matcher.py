import numpy as np
import logging
from typing import List, Tuple, Optional
import face_recognition as fr

logger = logging.getLogger(__name__)


class FaceMatcher:
    def __init__(self, threshold: float = 0.45):
        """
        Initialize face matcher

        Args:
            threshold: confidence threshold for matching (0-1)
        """
        self.threshold = threshold

    @staticmethod
    def _decode_encoding(encoded_str: str) -> np.ndarray:
        """Decode base64 string to numpy array"""
        import base64
        decoded_bytes = base64.b64decode(encoded_str.encode('utf-8'))
        return np.frombuffer(decoded_bytes, dtype=np.float32)

    def compare_encodings(
        self,
        encoding1: np.ndarray,
        encoding2: np.ndarray,
    ) -> float:

        try:

            distance = fr.face_distance(
                [encoding2],
                encoding1
            )[0]

            return float(distance)

        except Exception as e:
            logger.error(f"Error comparing encodings: {e}")
            return 999.0
    def find_best_match(
        self,
        test_encoding: np.ndarray,
        known_encodings: List[Tuple[int, str]],
    ) -> Optional[Tuple[int, float]]:
        """
        Find best match from list of known encodings

        Args:
            test_encoding: Face encoding to test
            known_encodings: List of (id, encoded_string) tuples

        Returns:
            Tuple of (employee_id, confidence_score) or None if no match
        """
        best_match = None
        best_distance = 999.0

        for employee_id, encoded_str in known_encodings:
            try:
                known_encoding = self._decode_encoding(encoded_str)
                distance = self.compare_encodings(test_encoding,known_encoding)
                print("=" * 50)
                print(f"Employee ID : {employee_id}")
                print("=" * 50)

                if distance < best_distance:

                    best_distance = distance
                    best_match = (employee_id, distance)

            except Exception as e:
                logger.error(f"Error processing encoding for employee {employee_id}: {e}")
                continue

        if best_match and best_distance <= self.threshold:
            return best_match

        return None

    def find_all_matches(
        self,
        test_encoding: np.ndarray,
        known_encodings: List[Tuple[int, str]],
        top_n: int = 5,
    ) -> List[Tuple[int, float]]:
        """
        Find all matches above threshold

        Args:
            test_encoding: Face encoding to test
            known_encodings: List of (id, encoded_string) tuples
            top_n: Return top N matches

        Returns:
            List of (employee_id, confidence_score) tuples sorted by score
        """
        matches = []

        for employee_id, encoded_str in known_encodings:
            try:
                known_encoding = self._decode_encoding(encoded_str)
                score = self.compare_encodings(test_encoding, known_encoding)

                if score >= self.threshold:
                    matches.append((employee_id, score))

            except Exception as e:
                logger.error(f"Error processing encoding for employee {employee_id}: {e}")
                continue

        matches.sort(key=lambda x: x[1], reverse=True)
        return matches[:top_n]
