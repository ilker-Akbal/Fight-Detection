from __future__ import annotations

import argparse
import time
from pathlib import Path

from HizTespiti.calibration.src.calibration_config import load_config
from HizTespiti.calibration.src.camera_calibration import (
    CameraCalibration,
    MeasurementConfig,
    RoadRoi,
    ScaleConfig,
)
from HizTespiti.calibration.src.calibration_store import CalibrationStore
from HizTespiti.calibration.src.scale_estimator import (
    estimate_scale_from_vehicle_boxes,
    load_track_samples,
)
from HizTespiti.calibration.src.utils import ensure_dir


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config",
        default="HizTespiti/calibration/configs/calibration.yaml",
    )
    p.add_argument("--camera-id", default=None)
    p.add_argument("--tracks", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--speed-limit", type=float, default=None)
    p.add_argument("--tolerance", type=float, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config)

    if args.camera_id is not None:
        cfg.camera.camera_id = args.camera_id

    if args.tracks is not None:
        cfg.input.yolo_tracks_path = args.tracks

    if args.out is not None:
        cfg.output.calibration_path = args.out

    if args.speed_limit is not None:
        cfg.calibration_defaults.speed_limit_kmh = args.speed_limit

    if args.tolerance is not None:
        cfg.calibration_defaults.tolerance_kmh = args.tolerance

    ensure_dir(cfg.output.output_dir)

    samples = load_track_samples(
        path=cfg.input.yolo_tracks_path,
        cfg=cfg.scale_estimation,
    )

    estimate = estimate_scale_from_vehicle_boxes(
        samples=samples,
        cfg=cfg.scale_estimation,
    )

    scale = ScaleConfig(
        source=estimate.method,
        meter_per_pixel=estimate.meter_per_pixel,
        confidence=estimate.confidence,
        user_corrected=False,
    )

    road_roi_raw = cfg.calibration_defaults.road_roi
    measurement_raw = cfg.calibration_defaults.measurement

    calibration = CameraCalibration(
        camera_id=cfg.camera.camera_id,
        speed_limit_kmh=cfg.calibration_defaults.speed_limit_kmh,
        tolerance_kmh=cfg.calibration_defaults.tolerance_kmh,
        road_roi=RoadRoi(
            enabled=bool(road_roi_raw.get("enabled", False)),
            polygon=list(road_roi_raw.get("polygon", [])),
        ),
        measurement=MeasurementConfig(
            mode=str(measurement_raw.get("mode", "pixel_scale")),
            direction=str(measurement_raw.get("direction", "AUTO")),
            line_a=list(measurement_raw.get("line_a", [])),
            line_b=list(measurement_raw.get("line_b", [])),
            distance_m=measurement_raw.get("distance_m", None),
        ),
        scale=scale,
        updated_at=time.time(),
        meta={
            "scale_estimation": {
                "sample_count": estimate.sample_count,
                "confidence": estimate.confidence,
                "notes": estimate.notes,
            },
            "input_tracks": str(Path(cfg.input.yolo_tracks_path)),
        },
    )

    saved_path = CalibrationStore(cfg.output.calibration_path).save(calibration)

    print("=" * 90)
    print("HIZ TESPITI - CAMERA CALIBRATION")
    print(f"camera_id       : {calibration.camera_id}")
    print(f"tracks          : {cfg.input.yolo_tracks_path}")
    print(f"samples         : {estimate.sample_count}")
    print(f"meter_per_pixel : {estimate.meter_per_pixel}")
    print(f"confidence      : {estimate.confidence:.3f}")
    print(f"saved           : {saved_path}")
    print("=" * 90)

    if estimate.meter_per_pixel is None:
        print("UYARI: Otomatik scale üretilemedi.")
        print("Çözüm: Daha uzun video çalıştır veya kullanıcı dashboard’dan scale düzeltsin.")


if __name__ == "__main__":
    main()