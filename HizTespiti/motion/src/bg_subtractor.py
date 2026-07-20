from __future__ import annotations

import cv2
import numpy as np

from .motion_config import MotionConfig


class BackgroundMotionDetector:
    def __init__(self, cfg: MotionConfig):
        self.cfg = cfg
        self.subtractor = cv2.createBackgroundSubtractorMOG2(
            history=cfg.history,
            varThreshold=cfg.var_threshold,
            detectShadows=cfg.detect_shadows,
        )

        self.open_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (cfg.morph_open_kernel, cfg.morph_open_kernel),
        )
        self.close_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (cfg.morph_close_kernel, cfg.morph_close_kernel),
        )

    def detect(self, frame_bgr, roi_mask: np.ndarray):
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

        if self.cfg.blur_kernel > 1:
            k = self.cfg.blur_kernel
            if k % 2 == 0:
                k += 1
            gray = cv2.GaussianBlur(gray, (k, k), 0)

        fg = self.subtractor.apply(gray)

        _, mask = cv2.threshold(
            fg,
            self.cfg.threshold,
            255,
            cv2.THRESH_BINARY,
        )

        mask = cv2.bitwise_and(mask, mask, mask=roi_mask)

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.open_kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.close_kernel)

        if self.cfg.dilate_iterations > 0:
            mask = cv2.dilate(mask, None, iterations=self.cfg.dilate_iterations)

        contours, _ = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )

        h, w = mask.shape[:2]
        frame_area = h * w

        boxes = []
        total_area = 0

        for c in contours:
            area = cv2.contourArea(c)

            if area < self.cfg.min_area:
                continue

            if area > frame_area * self.cfg.max_area_ratio:
                continue

            x, y, bw, bh = cv2.boundingRect(c)
            boxes.append((x, y, bw, bh, float(area)))
            total_area += area

        motion_score = float(total_area) / float(frame_area)

        return {
            "mask": mask,
            "boxes": boxes,
            "motion_score": motion_score,
        }