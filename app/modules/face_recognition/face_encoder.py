import face_recognition as fr
import numpy as np
import json
import base64
import logging

logger = logging.getLogger(__name__)


class FaceEncoder:
    @staticmethod
    def encode_face(image_array: np.ndarray) -> dict:
        """
        Generate face encoding from image array

        Args:
            image_array: numpy array of image

        Returns:
            dict with encoding and quality score
        """
        try:
            rgb_array = image_array if len(image_array.shape) == 3 else np.stack([image_array] * 3, axis=-1)

            face_locations = fr.face_locations(rgb_array, model="hog")
            if len(face_locations) != 1:
                return {"error": f"Expected 1 face, found {len(face_locations)}"}

            encodings = fr.face_encodings(rgb_array, face_locations)
            if not encodings:
                return {"error": "Could not generate face encoding"}

            encoding = encodings[0]
            encoded_str = FaceEncoder._encode_to_string(encoding)

            return {
                "encoding": encoded_str,
                "location": face_locations[0],
            }
        except Exception as e:
            logger.error(f"Error encoding face: {e}")
            return {"error": str(e)}

    @staticmethod
    def _encode_to_string(encoding: np.ndarray) -> str:
        """Convert numpy array to base64 string for storage"""
        return base64.b64encode(encoding.astype(np.float32).tobytes()).decode('utf-8')

    @staticmethod
    def _decode_from_string(encoded_str: str) -> np.ndarray:
        """Decode base64 string back to numpy array"""
        decoded_bytes = base64.b64decode(encoded_str.encode('utf-8'))
        return np.frombuffer(decoded_bytes, dtype=np.float32)
