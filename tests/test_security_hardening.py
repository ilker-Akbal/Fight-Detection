"""Run security regressions without touching the application's database or runtime."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


BACKEND = Path(__file__).resolve().parents[1] / "Fight_backend_project" / "backend_frontend_project"


def test_django_security_regressions_in_isolated_database():
    code = (
        "import os; os.environ['DJANGO_SETTINGS_MODULE']='backend_frontend_project.settings'; "
        "from django.conf import settings; "
        "settings.DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}; "
        "import django; django.setup(); from django.core.management import call_command; "
        "call_command('test','accounts.tests','speed_detection.tests','guvenlik.security_tests',"
        "'streams.security_tests',verbosity=1,interactive=False)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("value,expected", [(None, False), ("0", False), ("1", True)])
def test_debug_default_and_explicit_opt_in(value, expected):
    # Disable dotenv only in this child process: test the setting's default,
    # independently of an operator's explicit development opt-in in .env.
    env = dict(os.environ)
    env.pop("DJANGO_DEBUG", None)
    if value is not None:
        env["DJANGO_DEBUG"] = value
    code = (
        "import runpy, sys, types; "
        "sys.modules['dotenv']=types.SimpleNamespace(load_dotenv=lambda *a, **kw: None); "
        "settings=runpy.run_module('backend_frontend_project.settings'); "
        f"assert settings['DEBUG'] is {expected!r}"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
