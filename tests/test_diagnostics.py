import sys

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from alice.tools.base import ToolContext
from alice.tools.diagnostics import AppState, ServiceDiagnose, TestRun


@pytest.fixture
def ctx(tmp_path):
    config = MagicMock()
    config.agent = {}
    return ToolContext(cwd=tmp_path, config=config)


def _script(tmp_path, code: str) -> str:
    """Write the code to a temporary .py file: 'py -3' does not exist on Ubuntu, and inline -c gets mangled in PowerShell."""
    p = tmp_path / "script.py"
    p.write_text(code, encoding="utf-8")
    return f'& "{sys.executable}" "{p}"' if sys.platform == "win32" else f'"{sys.executable}" "{p}"'


def test_app_state_runs(ctx):
    res = AppState().run(ctx)
    assert not res.is_error
    assert "Servis ve Mod" in res.text


def test_app_state_uses_real_config_roles(tmp_path):
    """The real Config has no 'brain' field; the model chain comes from roles, so this is an AttributeError regression test."""
    config = SimpleNamespace(agent={}, roles={"brain": ["model-a", "model-b"]}, data={})
    ctx = ToolContext(cwd=tmp_path, config=config)
    res = AppState().run(ctx, section="config")
    assert not res.is_error
    assert "model-a" in res.text


def test_service_diagnose_runs(ctx):
    res = ServiceDiagnose().run(ctx)
    assert not res.is_error
    assert "Core service diagnosis" in res.text


def test_service_diagnose_opens_and_closes_own_store(ctx):
    """If ctx.state has no store, it should open and close its own Store without crashing with a TypeError."""
    res = ServiceDiagnose().run(ctx, service="core")
    assert "Store okunurken hata" not in res.text


def test_test_run_success(ctx, tmp_path):
    res = TestRun().run(ctx, command=_script(tmp_path, "print('test passed')"))
    assert not res.is_error
    assert "All tests passed" in res.text


def test_test_run_reports_failure(ctx, tmp_path):
    code = (
        "print('=========================== short test summary info ===========================');\n"
        "print('FAILED tests/test_x.py::test_y - AssertionError: patladi');\n"
        "print('========================= 1 failed, 2 passed in 0.10s =========================');\n"
        "raise SystemExit(1)\n"
    )
    res = TestRun().run(ctx, command=_script(tmp_path, code))
    assert res.is_error
    assert "test_x.py::test_y" in res.text and "AssertionError" in res.text
    assert "başarıyla geçti" not in res.text
