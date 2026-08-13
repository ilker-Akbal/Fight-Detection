"""Django settings used only by the repository pytest suite."""
from __future__ import annotations

import os
from pathlib import Path

from backend_frontend_project.settings import *  # noqa: F403


DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
MIDDLEWARE = [
    item for item in MIDDLEWARE  # noqa: F405
    if item != "whitenoise.middleware.WhiteNoiseMiddleware"
]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
MEDIA_ROOT = Path(os.environ["FIGHT_TEST_MEDIA_ROOT"])
PIPELINE_OUTPUT_BASE = MEDIA_ROOT / "pipeline_runs"
SPEED_PIPELINE_OUTPUT_BASE = MEDIA_ROOT / "speed_runs"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
