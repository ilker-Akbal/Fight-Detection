from __future__ import annotations

import numpy as np
import cv2


class RoiMask:
    def __init__(self, enabled: bool, polygon: list[list[int]]):
        self.enabled = enabled
        self.polygon = polygon
        self._mask_cache = None
        self._shape_cache = None

    def get_mask(self, frame_shape) -> np.ndarray:
        h, w = frame_shape[:2]

        if (
            self._mask_cache is not None
            and self._shape_cache == (h, w)
        ):
            return self._mask_cache

        mask = np.zeros((h, w), dtype=np.uint8)

        if not self.enabled or not self.polygon:
            mask[:, :] = 255
        else:
            pts = np.array(self.polygon, dtype=np.int32)
            cv2.fillPoly(mask, [pts], 255)

        self._mask_cache = mask
        self._shape_cache = (h, w)
        return mask

    def apply(self, frame):
        mask = self.get_mask(frame.shape)
        return cv2.bitwise_and(frame, frame, mask=mask)

    def draw(self, frame):
        if not self.enabled or not self.polygon:
            return frame

        pts = np.array(self.polygon, dtype=np.int32)
        cv2.polylines(frame, [pts], isClosed=True, color=(0, 255, 0), thickness=2)
        return frame