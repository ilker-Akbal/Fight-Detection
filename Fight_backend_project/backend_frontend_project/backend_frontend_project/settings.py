import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent.parent

def find_repo_root(start_dir: Path) -> Path:
    """
    Django backend klasöründen çalışınca repo kökündeki fight/ ve HizTespiti/
    paketlerini Python import yoluna eklemek için repo kökünü otomatik bulur.

    Önemli:
    Repo root sayılması için hem fight/ hem HizTespiti/ aynı klasörde bulunmalı.
    Sadece HizTespiti/ görürsek backend içindeki yanlış/boş klasöre takılabilir.
    """
    start_dir = start_dir.resolve()

    for candidate in [start_dir, *start_dir.parents]:
        if (candidate / "fight").is_dir() and (candidate / "HizTespiti").is_dir():
            return candidate

    return BASE_DIR.parent.parent.resolve()

REPO_ROOT = find_repo_root(BASE_DIR)

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


if load_dotenv:
    load_dotenv(REPO_ROOT / ".env")


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-secret-key")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv(
        "DJANGO_ALLOWED_HOSTS",
        "localhost,127.0.0.1,0.0.0.0",
    ).split(",")
    if h.strip()
]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Project apps
    "speed_detection",
    "streams",
    "accounts",
    "guvenlik",
    "adminx",
]


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",

    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.AuthRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


ROOT_URLCONF = "backend_frontend_project.urls"


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


WSGI_APPLICATION = "backend_frontend_project.wsgi.application"


# ============================================================
# Database - Localhost / SQLite
# ============================================================

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "tr-tr"
TIME_ZONE = "Europe/Istanbul"
USE_I18N = True
USE_TZ = True


# ============================================================
# Localhost Deployment (/)
# ============================================================

# Yerelde uygulama doğrudan http://127.0.0.1:8000/ altında çalışır.
URL_PREFIX = ""
FORCE_SCRIPT_NAME = None


# ============================================================
# Static / Media Files
# ============================================================

STATIC_URL = f"{URL_PREFIX}/static/" if URL_PREFIX else "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

MEDIA_URL = f"{URL_PREFIX}/media/" if URL_PREFIX else "/media/"
MEDIA_ROOT = BASE_DIR / "media"


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


LOGIN_URL = f"{URL_PREFIX}/accounts/login/" if URL_PREFIX else "/accounts/login/"
LOGIN_REDIRECT_URL = f"{URL_PREFIX}/" if URL_PREFIX else "/"
LOGOUT_REDIRECT_URL = f"{URL_PREFIX}/accounts/login/" if URL_PREFIX else "/accounts/login/"


# ============================================================
# Session / CSRF
# ============================================================

SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SESSION_COOKIE_PATH = f"{URL_PREFIX}/" if URL_PREFIX else "/"
CSRF_COOKIE_PATH = f"{URL_PREFIX}/" if URL_PREFIX else "/"

SESSION_COOKIE_NAME = "fight_sessionid"
CSRF_COOKIE_NAME = "fight_csrftoken"


# ============================================================
# Reverse Proxy / HTTPS
# ============================================================

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True


# ============================================================
# Repository / Output Paths
# ============================================================

PIPELINE_OUTPUT_BASE = BASE_DIR / "media" / "pipeline_runs"
RUNTIME_CONTROL_MODE = os.getenv("RUNTIME_CONTROL_MODE", "supervisor").strip().lower()
RUNTIME_SUPERVISOR_URL = os.getenv(
    "RUNTIME_SUPERVISOR_URL", "http://127.0.0.1:8765"
).rstrip("/")
RUNTIME_SUPERVISOR_TOKEN = os.getenv("RUNTIME_SUPERVISOR_TOKEN", "")
RUNTIME_SUPERVISOR_TIMEOUT_SEC = float(
    os.getenv("RUNTIME_SUPERVISOR_TIMEOUT_SEC", "3.0")
)
SPEED_PIPELINE_OUTPUT_BASE = BASE_DIR / "media" / "speed_runs"
SPEED_CALIBRATION_OUTPUT_BASE = BASE_DIR / "media" / "speed_calibrations"


# ============================================================
# Fight Detection Pipeline
# ============================================================

PIPELINE_ENTRY_MODULE = "fight.pipeline_mp.run_multiprocess"

PIPELINE_DEFAULTS = {
    "motion_config": "fight/motion/configs/motion.yaml",
    "yolo_config": "fight/yolo/configs/yolo.yaml",
    "yolo_weights": "fight/yolo11n.pt",
    "pose_config": "fight/pose/configs/pose.yaml",
    "stage3_config": "fight/3D_CNN/configs/stage3.yaml",

    "person_conf": 0.25,
    "yolo_stride": 2,
    "pose_stride": 2,
    "fight_thr": 0.35,

    "use_pose": True,
    "use_stage3": True,

    "roi_size": 320,
    "roi_pad_value": 114,
    "min_persons_for_pose": 2,
    "pose_hold_frames": 8,
    "event_close_grace_frames": 12,

    "prebuffer_frames": 24,
    "max_event_frames": 128,
    "clip_fps": 16.0,
    "min_queue_frames": 16,

    "reconnect_sec": 2.0,
    "status_log_every": 20,

    "stage3_queue_size": 64,
    "incident_queue_size": 256,
    "report_queue_size": 8192,
    "stage3_enqueue_timeout_sec": 0.15,

    "person_request_queue_size": 64,
    "person_result_queue_size": 64,
    "person_camera_result_queue_size": 2,
    "person_request_enqueue_timeout_sec": 0.5,
    "person_result_enqueue_timeout_sec": 1.0,
    "person_route_timeout_sec": 0.5,
    "person_inference_timeout_sec": 30.0,
    "person_batch_enabled": False,
    "person_batch_size": 1,
    "person_batch_max_wait_ms": 0.0,

    "pose_request_queue_size": 64,
    "pose_result_queue_size": 64,
    "pose_camera_result_queue_size": 2,
    "pose_request_enqueue_timeout_sec": 0.5,
    "pose_result_enqueue_timeout_sec": 1.0,
    "pose_route_timeout_sec": 0.5,
    "pose_inference_timeout_sec": 30.0,
    "pose_batch_enabled": False,
    "pose_batch_size": 1,
    "pose_batch_max_wait_ms": 0.0,

    "performance_metrics_enabled": False,
    "performance_metrics_sample_every": 1,
    "performance_metrics_max_samples": 2048,
    "performance_metrics_warmup_requests": 2,

    "camera_cv2_threads": 1,
    "person_worker_cv2_threads": 1,
    "pose_worker_cv2_threads": 1,
    "stage3_cv2_threads": 1,
    "incident_cv2_threads": 1,

    "camera_ingest_mode": "centralized",
    "camera_ingest_fight_queue_size": 8,
    "camera_ingest_preview_queue_size": 1,
    "camera_ingest_publish_timeout_sec": 0.2,
    "camera_ingest_file_fight_policy": "ordered",
    "camera_ingest_cv2_threads": 1,
    "camera_preview_cv2_threads": 1,
    "camera_reconnect_enabled": True,
    "camera_reconnect_initial_delay_sec": 0.5,
    "camera_reconnect_max_delay_sec": 8.0,

    "restart_camera_processes": False,
    "camera_restart_backoff_sec": 3.0,
    "loop_file_sources": False,
    "stop_when_file_camera_done": True,

    "person_track_max_age": 8,
    "person_track_min_hits": 1,
    "person_track_iou_match_thr": 0.22,
    "person_track_conf_alpha": 0.65,
    "person_track_max_tracks": 12,

    "pair_driven_event_start": True,
    "pair_event_start_score": 0.25,
    "pair_event_min_2p_frames": 2,

    "pair_hold_sec": 1.0,
    "direct_roi_hold_sec": 1.0,
    "clip_soft_hold_frames": 10,
    "roi_invalid_drop_frames": 4,
    "roi_person_min_count": 2,
    "roi_person_min_iou": 0.08,
    "two_p_grace_frames": 20,

    "pose_hold_sec": 1.0,
    "pose_trigger_hold_sec": 2.4,
    "pair_enter_score": 0.58,
    "pair_keep_score": 0.42,
    "pair_keep_frames": 12,
    "pair_min_hits_to_activate": 2,
    "pair_candidate_confirm_frames": 2,
    "pair_identity_iou_thr": 0.35,
    "pair_switch_margin": 0.06,
    "pair_roi_expand_x": 1.20,
    "pair_roi_expand_y": 1.14,
    "pair_debug": False,

    "stage3_event_min_positive_hits": 4,
    "stage3_event_min_pose_mean": 0.12,
    "stage3_event_min_pose_max": 0.25,
    "stage3_event_min_duration_sec": 0.25,
    "stage3_drop_close_reasons": "roi_crop_failed",

    "incident_enter_thr": 0.62,
    "incident_keep_thr": 0.40,
    "incident_vote_window": 7,
    "incident_vote_enter_needed": 2,
    "incident_vote_keep_needed": 2,
    "incident_merge_gap_sec": 20.0,
    "incident_max_bridge_nonfight": 1,
    "incident_min_segments": 2,
    "incident_single_strong_fight_thr": 0.82,
    "incident_confirm_min_duration_sec": 0.5,
    "incident_cooldown_sec": 60.0,
    "incident_clip_ready_wait_sec": 8.0,
    "incident_stale_finalize_sec": 8.0,
    "incident_temporal_iou_merge_thr": 0.30,
    "incident_write_nonfight": False,
    "incident_keep_temp_parts": True,

    "preview_every_frames": 5,
    "preview_write_interval_sec": 0.25,
    "preview_jpeg_quality": 75,
    "report_flush_interval_sec": 0.25,

    "stop_run_when_all_file_cameras_done": True,
    "file_run_finalize_wait_sec": 25.0,
    "file_run_queue_empty_settle_sec": 10.0,
}


# ============================================================
# Speed Detection Pipeline
# ============================================================

SPEED_PIPELINE_ENTRY_MODULE = "HizTespiti.speed_mp.run_multiprocess_speed"

SPEED_PIPELINE_BASE_CONFIG = "HizTespiti/speed/configs/speed.yaml"

SPEED_YOLO_WEIGHTS = os.getenv("SPEED_YOLO_WEIGHTS", "yolo11s.pt")

SPEED_PIPELINE_DEFAULTS = {
    "config": "HizTespiti/speed/configs/speed.yaml",
    "weights": os.getenv("SPEED_DEFAULT_WEIGHTS", "yolo11s.pt"),
    "calibration": "HizTespiti/calibration/out_calibration/cam_001_calibration.json",
    "camera_id": "cam_001",
    "source": "fight/sample_2.mp4",
    "no_show": True,
    "no_motion": False,
}


# ============================================================
# External Mail Service Settings
# ============================================================

MAIL_SERVICE_ENABLED = os.getenv("MAIL_SERVICE_ENABLED", "True") == "True"

MAIL_API_URL = os.getenv(
    "MAIL_API_URL",
    "https://yzdd.gop.edu.tr/postman/api/v1/email/send",
)

MAIL_API_KEY = os.getenv("MAIL_API_KEY", "")

FRONTEND_URL = os.getenv(
    "FRONTEND_URL",
    "http://127.0.0.1:8000",
)

MAIL_LOGO_URL = os.getenv(
    "MAIL_LOGO_URL",
    f"{FRONTEND_URL}/static/images/togu-logo.png",
)
