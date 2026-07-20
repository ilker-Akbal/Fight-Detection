# ▶️ Pipeline Çalıştırma

## Motion Test

```text
python -m fight.pipeline.run_live --motion-config fight/motion/configs/motion.yaml --show
```

---

## Webcam ile Tam Pipeline

```text
python -m fight.pipeline.run_live --motion-config fight/motion/configs/motion.yaml --yolo-config fight/yolo/configs/yolo.yaml --use-pose --pose-weights fight/pose/weights/yolo11n-pose.pt --use-stage3 --stage3-config fight/3D_CNN/configs/stage3.yaml --show
```

---

## Video ile Pipeline

```text
python -m fight.pipeline.run_live --source fight/sample_2.mp4 --motion-config fight/motion/configs/motion.yaml --yolo-config fight/yolo/configs/yolo.yaml --use-pose --pose-weights fight/pose/weights/yolo11n-pose.pt --use-stage3 --stage3-config fight/3D_CNN/configs/stage3.yaml --show
```

---

# 📁 Proje Klasör Yapısı

```text
fight
 ├── motion
 ├── yolo
 ├── pose
 ├── 3D_CNN
 ├── pipeline
 ├── shared
 ├── tools
 └── clip_debug
```

---

# 📌 Not

Model `.pt` dosyalarına erişim yoksa modeli yeniden paketlemek için şu araç kullanılabilir:

```text
fight/tools/pack_pt_from_folder_v2.py
```
