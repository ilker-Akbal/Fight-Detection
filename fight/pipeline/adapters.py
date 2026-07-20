from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from fight.pipeline.utils import resize_with_padding


class MotionAdapter:
    def __init__(self, cfg_path: str):
        from fight.motion.src.core.config import load_config
        from fight.motion.src.service.motion_service import MotionRunner

        self.cfg = load_config(Path(cfg_path))
        self.runner = MotionRunner(self.cfg)

    def step(self, ts: float, frame_bgr: np.ndarray):
        out = self.runner.step(ts, frame_bgr)
        if out is None:
            return 0.0, False, None, None
        return float(out.score), bool(out.pass_frame), out.frame_resized, out.vis_bgr

    def close(self):
        try:
            self.runner.close()
        except Exception:
            pass


class YoloAdapter:
    def __init__(self, cfg_path: str, weights_path: str):
        from ultralytics import YOLO
        import yaml

        self.model = YOLO(weights_path)

        with open(cfg_path, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f) or {}

        self.imgsz = int(self.cfg.get("imgsz", 416))
        self.conf = float(self.cfg.get("conf", 0.25))
        self.iou = float(self.cfg.get("iou", 0.45))
        self.device = self.cfg.get("device", 0)
        self.verbose = bool(self.cfg.get("verbose", False))

    def detect_persons(self, frame_bgr: np.ndarray):
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

        res = self.model.predict(
            source=frame_rgb,
            imgsz=self.imgsz,
            conf=self.conf,
            iou=self.iou,
            device=self.device,
            verbose=self.verbose,
        )[0]

        out = []
        if res.boxes is None:
            return out

        boxes = res.boxes
        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        clss = boxes.cls.cpu().numpy().astype(int)

        h, w = frame_bgr.shape[:2]

        for (x1, y1, x2, y2), c, cls in zip(xyxy, confs, clss):
            if cls != 0:
                continue

            x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
            bw = max(1, x2 - x1)
            bh = max(1, y2 - y1)
            area = bw * bh
            area_ratio = area / float(w * h)
            aspect = bh / float(bw)

            if area_ratio < 0.004:
                continue

            if aspect < 0.9 or aspect > 6.0:
                continue

            out.append((float(c), (x1, y1, x2, y2)))

        out.sort(key=lambda x: x[0], reverse=True)
        return out


class PoseLiveAdapter:
    def __init__(self, cfg_path: str):
        from fight.pose.src.pose_adapter import PoseAdapter
        from fight.pose.src.pose_gate import PoseGate
        import yaml

        self.adapter = PoseAdapter(cfg_path)

        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        tcfg = cfg.get("temporal", {})
        self.gate = PoseGate(
            window_size=int(tcfg.get("window_size", 6)),
            need_positive=int(tcfg.get("need_positive", 2)),
            min_mean_score=float(tcfg.get("min_mean_score", 0.30)),
            peak_score_thr=float(tcfg.get("peak_score_thr", 0.54)),
            min_consecutive=int(tcfg.get("min_consecutive", 2)),
        )

    def check(self, roi_bgr: np.ndarray):
        raw = self.adapter.evaluate(roi_bgr, hist_positive=int(sum(self.gate.hist)))
        dec = self.gate.update(raw.score, raw.ok)

        raw.ok = dec.pose_ok
        raw.hist_positive = dec.hist_positive
        return raw

    def reset(self):
        self.gate.reset()


class Stage3Adapter:
    """
    Canlı pipeline Stage3 adapter.

    Eski sistem:
        R3D-18, 32/64 frame, eski Kinetics mean/std.

    Yeni sistem:
        X3D-M, 64 frame context -> 16 frame sample,
        mean/std eğitim koduyla aynı:
            mean=[0.45, 0.45, 0.45]
            std =[0.225, 0.225, 0.225]

    run_multi_live_queue.py tarafındaki arayüz aynı kalır:
        Stage3Adapter.infer(frames) -> fight_prob
    """

    def __init__(self, cfg_path: str):
        import importlib.util
        import yaml
        import torch

        self.torch = torch

        cfgp = Path(cfg_path).resolve()
        with open(cfgp, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f) or {}

        stage3_root = cfgp.parent.parent
        self.stage3_root = stage3_root

        mcfg = self.cfg.get("model", {}) or {}
        icfg = self.cfg.get("input", {}) or {}

        self.model_name = str(mcfg.get("name", "x3d_m"))
        self.num_classes = int(mcfg.get("num_classes", 2))
        self.device = str(mcfg.get("device", "cuda"))
        self.amp = bool(mcfg.get("amp", True))
        self.amp_dtype_name = str(mcfg.get("amp_dtype", "bf16")).lower().strip()

        if self.device.startswith("cuda") and not torch.cuda.is_available():
            self.device = "cpu"
            self.amp = False

        # Eğitim mantığı:
        # 64 context frame okunur, modele 16 frame verilir.
        self.context_frames = int(icfg.get("context_frames", 64))
        self.clip_len = int(icfg.get("clip_len", 16))
        self.size = int(icfg.get("size", 224))
        self.pad_value = int(icfg.get("pad_value", 114))

        mean_vals = icfg.get("mean", [0.45, 0.45, 0.45])
        std_vals = icfg.get("std", [0.225, 0.225, 0.225])

        ckpt_rel = str(mcfg.get("ckpt_path", "")).replace("\\", "/")
        if not ckpt_rel:
            raise RuntimeError("stage3.yaml içinde model.ckpt_path yok")

        p = Path(ckpt_rel)
        candidates = [
            (Path.cwd() / p).resolve(),
            (stage3_root / p).resolve(),
            (stage3_root / "weights" / p.name).resolve(),
        ]

        ckpt_path = None
        for c in candidates:
            if c.exists():
                ckpt_path = c
                break

        if ckpt_path is None:
            raise FileNotFoundError(
                "Stage3 checkpoint bulunamadı. Denenen yollar:\n"
                + "\n".join(str(x) for x in candidates)
            )

        ml_path = stage3_root / "src" / "model_loader.py"
        spec = importlib.util.spec_from_file_location("stage3_model_loader", str(ml_path))
        mod = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(mod)

        self.strict = bool(mcfg.get("strict", False))

        self.model = mod.load_model(
            str(ckpt_path),
            device=self.device,
            num_classes=self.num_classes,
            model_name=self.model_name,
            pretrained=False,
            strict=self.strict,
        )
        self.model.eval()

        mean = torch.tensor(mean_vals, dtype=torch.float32).view(1, 3, 1, 1, 1)
        std = torch.tensor(std_vals, dtype=torch.float32).view(1, 3, 1, 1, 1)
        self.mean = mean.to(self.device)
        self.std = std.to(self.device)

        if self.device.startswith("cuda"):
            try:
                torch.backends.cudnn.benchmark = True
            except Exception:
                pass
            try:
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
                torch.set_float32_matmul_precision("high")
            except Exception:
                pass

        self.amp_dtype = self._resolve_amp_dtype()

        print(
            "[STAGE3] loaded",
            f"model={self.model_name}",
            f"ckpt={ckpt_path}",
            f"device={self.device}",
            f"context_frames={self.context_frames}",
            f"clip_len={self.clip_len}",
            f"size={self.size}",
            f"amp={self.amp}",
            f"amp_dtype={self.amp_dtype_name}",
        )

    def _resolve_amp_dtype(self):
        torch = self.torch

        if not str(self.device).startswith("cuda"):
            return torch.float32

        if self.amp_dtype_name in {"bf16", "bfloat16"}:
            try:
                if torch.cuda.is_bf16_supported():
                    return torch.bfloat16
            except Exception:
                pass
            return torch.float16

        if self.amp_dtype_name in {"fp16", "float16", "half"}:
            return torch.float16

        return torch.float16

    @staticmethod
    def _linspace_indices(n: int, k: int):
        if n <= 0:
            return []
        if k <= 1:
            return [max(0, n // 2)]
        return np.linspace(0, n - 1, k).astype(np.int64).tolist()

    def _select_frames_for_x3d(self, clip_bgr_list):
        frames = list(clip_bgr_list)

        if not frames:
            raise RuntimeError("Stage3 clip boş")

        # En güncel context kullanılır.
        # Event uzun ise son 64 frame, kısa ise mevcut frame + pad.
        if self.context_frames > 0 and len(frames) > self.context_frames:
            frames = frames[-self.context_frames:]

        if len(frames) < self.clip_len:
            last = frames[-1]
            frames = frames + [last] * (self.clip_len - len(frames))

        ids = self._linspace_indices(len(frames), self.clip_len)
        sampled = [frames[int(i)] for i in ids]

        if len(sampled) != self.clip_len:
            raise RuntimeError(
                f"Stage3 sample hatası: beklenen={self.clip_len}, gelen={len(sampled)}"
            )

        return sampled

    def _preprocess(self, clip_bgr_list):
        torch = self.torch

        frames = self._select_frames_for_x3d(clip_bgr_list)

        out = []
        for f in frames:
            if f is None:
                continue

            rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)

            # Pipeline ROI tarafında kare/letterbox mantığı kullanıldığı için
            # canlıda aspect bozmadan padding ile 224'e çekiyoruz.
            rgb = resize_with_padding(
                rgb,
                out_size=self.size,
                pad_value=self.pad_value,
                interpolation=cv2.INTER_LINEAR,
            )

            out.append(rgb)

        if not out:
            raise RuntimeError("Stage3 preprocess sonrası frame kalmadı")

        if len(out) < self.clip_len:
            last = out[-1]
            out = out + [last] * (self.clip_len - len(out))

        arr = np.stack(out, axis=0)  # T,H,W,C RGB
        x = torch.from_numpy(arr).to(torch.float32) / 255.0

        # T,H,W,C -> T,C,H,W -> 1,T,C,H,W -> 1,C,T,H,W
        x = x.permute(0, 3, 1, 2).contiguous()
        x = x.unsqueeze(0)
        x = x.permute(0, 2, 1, 3, 4).contiguous()

        x = x.to(self.device, non_blocking=True)
        x = (x - self.mean) / self.std
        return x

    def infer(self, clip_bgr_list):
        torch = self.torch
        x = self._preprocess(clip_bgr_list)

        use_amp = bool(self.amp and str(self.device).startswith("cuda"))

        with torch.no_grad():
            if str(self.device).startswith("cuda"):
                with torch.amp.autocast(
                    device_type="cuda",
                    dtype=self.amp_dtype,
                    enabled=use_amp,
                ):
                    logits = self.model(x)
            else:
                logits = self.model(x)

            if isinstance(logits, (tuple, list)):
                logits = logits[0]

            if hasattr(logits, "ndim") and logits.ndim == 2 and logits.shape[1] >= 2:
                prob = torch.softmax(logits, dim=1)[:, 1].detach().float().item()
            else:
                prob = torch.sigmoid(logits).detach().float().item()

        return float(prob)