from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from statistics import median

from .calibration_config import ScaleEstimationConfig


@dataclass
class TrackBoxSample:
    vehicle_class: str
    conf: float
    box_w: float
    box_h: float
    track_id: int
    frame_idx: int


@dataclass
class ScaleEstimate:
    meter_per_pixel: float | None
    confidence: float
    sample_count: int
    method: str
    notes: list[str]


def _trimmed(values: list[float], trim_ratio: float) -> list[float]:
    if not values:
        return []

    values = sorted(values)
    n = len(values)
    k = int(n * trim_ratio)

    if k <= 0:
        return values

    if n - 2 * k <= 0:
        return values

    return values[k:n - k]


def load_track_samples(path: str | Path, cfg: ScaleEstimationConfig) -> list[TrackBoxSample]:
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"YOLO track event dosyası bulunamadı: {path}")

    samples: list[TrackBoxSample] = []

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            conf = float(row.get("conf", 0.0))

            if conf < cfg.min_conf:
                continue

            box = row.get("box_xyxy", [])

            if not isinstance(box, list) or len(box) != 4:
                continue

            x1, y1, x2, y2 = [float(v) for v in box]
            w = abs(x2 - x1)
            h = abs(y2 - y1)

            if w < cfg.min_box_width_px or h < cfg.min_box_height_px:
                continue

            if w > cfg.max_box_width_px or h > cfg.max_box_height_px:
                continue

            vehicle_class = str(row.get("vehicle_class", "")).lower()

            if (
                vehicle_class not in cfg.reference_vehicle_lengths_m
                and vehicle_class not in cfg.reference_vehicle_widths_m
            ):
                continue

            samples.append(
                TrackBoxSample(
                    vehicle_class=vehicle_class,
                    conf=conf,
                    box_w=w,
                    box_h=h,
                    track_id=int(row.get("track_id", -1)),
                    frame_idx=int(row.get("frame_idx", -1)),
                )
            )

            if len(samples) >= cfg.max_samples:
                break

    return samples


def estimate_scale_from_vehicle_boxes(
    samples: list[TrackBoxSample],
    cfg: ScaleEstimationConfig,
) -> ScaleEstimate:
    notes: list[str] = []

    if not cfg.enabled:
        return ScaleEstimate(
            meter_per_pixel=None,
            confidence=0.0,
            sample_count=0,
            method="disabled",
            notes=["scale estimation disabled"],
        )

    if len(samples) < cfg.min_samples:
        return ScaleEstimate(
            meter_per_pixel=None,
            confidence=0.0,
            sample_count=len(samples),
            method="auto_vehicle_size",
            notes=[
                f"Yetersiz örnek: {len(samples)} / {cfg.min_samples}",
                "Daha uzun video/akış ile YOLO track üret.",
            ],
        )

    estimates: list[float] = []

    for s in samples:
        cls_name = s.vehicle_class

        if cfg.use_width and cls_name in cfg.reference_vehicle_widths_m and s.box_w > 0:
            ref_w = float(cfg.reference_vehicle_widths_m[cls_name])
            estimates.append(ref_w / s.box_w)

        if cfg.use_height and cls_name in cfg.reference_vehicle_lengths_m and s.box_h > 0:
            ref_l = float(cfg.reference_vehicle_lengths_m[cls_name])
            estimates.append(ref_l / s.box_h)

    estimates = [v for v in estimates if 0.0005 <= v <= 0.5]

    if len(estimates) < cfg.min_samples:
        return ScaleEstimate(
            meter_per_pixel=None,
            confidence=0.0,
            sample_count=len(samples),
            method="auto_vehicle_size",
            notes=[
                "Geçerli scale tahmini üretilemedi.",
                f"Geçerli tahmin sayısı: {len(estimates)}",
            ],
        )

    stable = _trimmed(estimates, cfg.trim_ratio)
    mpp = float(median(stable))

    if not stable:
        return ScaleEstimate(
            meter_per_pixel=None,
            confidence=0.0,
            sample_count=len(samples),
            method="auto_vehicle_size",
            notes=["Trim sonrası geçerli tahmin kalmadı."],
        )

    spread = max(stable) - min(stable)
    rel_spread = spread / max(mpp, 1e-6)

    confidence = max(0.05, min(0.95, 1.0 - rel_spread))
    confidence *= min(1.0, len(samples) / max(cfg.min_samples * 3, 1))

    notes.append(f"raw_estimates={len(estimates)}")
    notes.append(f"trimmed_estimates={len(stable)}")
    notes.append(f"relative_spread={rel_spread:.4f}")

    return ScaleEstimate(
        meter_per_pixel=mpp,
        confidence=float(confidence),
        sample_count=len(samples),
        method="auto_vehicle_size",
        notes=notes,
    )