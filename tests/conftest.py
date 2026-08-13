from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DJANGO_ROOT = REPO_ROOT / "Fight_backend_project" / "backend_frontend_project"
if str(DJANGO_ROOT) not in sys.path:
    sys.path.insert(0, str(DJANGO_ROOT))

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="fight_detection_pytest_media_")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.django_settings")
os.environ["FIGHT_TEST_MEDIA_ROOT"] = _TEST_MEDIA_ROOT

import django

django.setup()

from django.test.runner import DiscoverRunner


_runner = None
_old_config = None


def pytest_sessionstart(session):
    global _runner, _old_config
    _runner = DiscoverRunner(verbosity=0, interactive=False)
    _runner.setup_test_environment()
    _old_config = _runner.setup_databases()


def pytest_sessionfinish(session, exitstatus):
    if _runner is not None and _old_config is not None:
        _runner.teardown_databases(_old_config)
        _runner.teardown_test_environment()
    shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

