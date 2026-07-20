from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn


def build_x3dm(num_classes: int = 2, pretrained: bool = False) -> torch.nn.Module:
    try:
        from pytorchvideo.models.hub import x3d_m
    except Exception as exc:
        raise RuntimeError(
            "pytorchvideo import edilemedi. Kurulum yap:\n"
            "pip install pytorchvideo fvcore iopath"
        ) from exc

    model = x3d_m(pretrained=pretrained)

    if not hasattr(model, "blocks") or not hasattr(model.blocks[-1], "proj"):
        raise RuntimeError("X3D-M head bulunamadı: model.blocks[-1].proj")

    in_features = model.blocks[-1].proj.in_features
    model.blocks[-1].proj = nn.Linear(in_features, int(num_classes))
    return model


def build_model(
    num_classes: int = 2,
    model_name: str = "x3d_m",
    pretrained: bool = False,
) -> torch.nn.Module:
    name = str(model_name or "x3d_m").lower().strip()

    if name in {"x3d_m", "x3dm", "x3d-m"}:
        return build_x3dm(num_classes=num_classes, pretrained=pretrained)

    raise ValueError(f"Desteklenmeyen Stage3 modeli: {model_name!r}")


def _resolve_ckpt_path(ckpt_path: str) -> str:
    p = Path(str(ckpt_path))

    if p.is_absolute() and p.exists():
        return str(p)

    cwd_cand = (Path.cwd() / p).resolve()
    if cwd_cand.exists():
        return str(cwd_cand)

    stage3_root = Path(__file__).resolve().parents[1]

    cand1 = (stage3_root / p).resolve()
    if cand1.exists():
        return str(cand1)

    cand2 = (stage3_root / "weights" / p.name).resolve()
    if cand2.exists():
        return str(cand2)

    raise FileNotFoundError(
        "Checkpoint bulunamadı. Denenen yollar:\n"
        f"- {p}\n"
        f"- {cwd_cand}\n"
        f"- {cand1}\n"
        f"- {cand2}"
    )


def _is_lfs_pointer(path: str) -> bool:
    head = Path(path).read_bytes()[:128]
    return head.startswith(b"version https://git-lfs.github.com/spec/v1")


def _is_zip(path: str) -> bool:
    try:
        return zipfile.is_zipfile(path)
    except Exception:
        return False


def _extract_state_dict(ckpt: Any) -> dict:
    if isinstance(ckpt, dict):
        for key in ("model", "state_dict", "model_state_dict"):
            val = ckpt.get(key)
            if isinstance(val, dict):
                return val

    if isinstance(ckpt, dict):
        tensor_like = [k for k, v in ckpt.items() if hasattr(v, "shape")]
        if tensor_like:
            return ckpt

    raise RuntimeError(
        "Checkpoint içinden state_dict okunamadı. "
        "Beklenen key: model/state_dict/model_state_dict veya direkt state_dict."
    )


def _clean_state_dict(sd: dict) -> dict:
    cleaned = {}

    for k, v in sd.items():
        nk = str(k)

        # DataParallel / torch.compile prefix temizliği
        for _ in range(4):
            old = nk
            for pref in ("module.", "_orig_mod.", "model."):
                if nk.startswith(pref):
                    nk = nk[len(pref):]
            if nk == old:
                break

        cleaned[nk] = v

    return cleaned


def _count_loadable_keys(model: torch.nn.Module, sd: dict) -> tuple[int, int]:
    model_sd = model.state_dict()
    matched = 0

    for k, v in sd.items():
        if k in model_sd and tuple(model_sd[k].shape) == tuple(v.shape):
            matched += 1

    return matched, len(model_sd)


def load_ckpt(
    model: torch.nn.Module,
    ckpt_path: str,
    strict: bool = False,
):
    ckpt_path = _resolve_ckpt_path(ckpt_path)

    if _is_lfs_pointer(ckpt_path):
        raise RuntimeError(
            f"Checkpoint gerçek ağırlık değil, Git LFS pointer dosyası: {ckpt_path}"
        )

    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    sd = _clean_state_dict(_extract_state_dict(ckpt))

    matched, total = _count_loadable_keys(model, sd)

    if matched <= 0:
        sample_ckpt = list(sd.keys())[:20]
        sample_model = list(model.state_dict().keys())[:20]
        raise RuntimeError(
            "Checkpoint ile X3D-M modeli arasında hiç eşleşen ağırlık yok.\n"
            f"ckpt_path={ckpt_path}\n"
            f"matched={matched}/{total}\n"
            f"checkpoint ilk keyler={sample_ckpt}\n"
            f"model ilk keyler={sample_model}"
        )

    missing, unexpected = model.load_state_dict(sd, strict=False)

    print(
        "[STAGE3][CKPT]",
        f"path={ckpt_path}",
        f"matched={matched}/{total}",
        f"missing={len(missing)}",
        f"unexpected={len(unexpected)}",
        f"strict={strict}",
        flush=True,
    )

    if missing:
        print("[STAGE3][CKPT] first missing:", missing[:20], flush=True)
    if unexpected:
        print("[STAGE3][CKPT] first unexpected:", unexpected[:20], flush=True)

    if strict and (missing or unexpected):
        raise RuntimeError(
            f"Checkpoint strict=True ile yüklenemedi: {ckpt_path}\n"
            f"missing={len(missing)} unexpected={len(unexpected)}"
        )

    return ckpt


def load_model(
    ckpt_path: str,
    device: str = "cuda",
    num_classes: int = 2,
    model_name: str = "x3d_m",
    pretrained: bool = False,
    strict: bool = False,
) -> torch.nn.Module:
    if str(device).startswith("cuda") and not torch.cuda.is_available():
        print("[STAGE3][WARN] CUDA yok, CPU kullanılacak.", flush=True)
        device = "cpu"

    model = build_model(
        num_classes=int(num_classes),
        model_name=model_name,
        pretrained=bool(pretrained),
    )

    load_ckpt(model, ckpt_path, strict=bool(strict))

    model.eval()
    model.to(device)

    print(
        "[STAGE3][MODEL] loaded",
        f"model={model_name}",
        f"device={device}",
        f"num_classes={num_classes}",
        flush=True,
    )

    return model