from __future__ import annotations

from pathlib import Path
import json

from .camera_calibration import CameraCalibration


class CalibrationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, calibration: CameraCalibration) -> Path:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(calibration.to_dict(), f, ensure_ascii=False, indent=2)

        return self.path