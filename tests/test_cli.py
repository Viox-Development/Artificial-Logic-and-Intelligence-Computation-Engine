"""Command-line usage: help, version, typos and missing packages print a one-line message instead of a traceback."""

from __future__ import annotations

import argparse
import io

import pytest

from alice import cli
from alice.config import load_config


def _no_agent(monkeypatch):
    """The test must fail if an agent gets built (a typo must not open the REPL)."""
    monkeypatch.setattr(cli, "build", lambda *a, **k: pytest.fail("ajan kurulmamalıydı"))


def test_init_creates_home(isolated_home, capsys):
    # The local import inside main() shadowed alice_home: `alice init` raised UnboundLocalError.
    assert cli.main(["init"]) == 0
    assert (isolated_home / "config.toml").exists() and (isolated_home / ".env").exists()
    # Rich wraps long paths across lines (the temp folder path is long on Windows): collapse the whitespace and check.
    assert "alice doctor" in " ".join(capsys.readouterr().out.split())


def test_version_flag_uses_package_metadata(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"alice {cli.package_version()}"


def test_package_version_falls_back_without_metadata(monkeypatch):
    import importlib.metadata

    def missing(_name):
        raise importlib.metadata.PackageNotFoundError("alice-harness")

    monkeypatch.setattr(importlib.metadata, "version", missing)
    from alice import __version__

    assert cli.package_version() == __version__


def test_main_help_is_turkish_and_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert e.value.code == 0
    assert "kullanım: alice" in out and "bu yardımı göster" in out and "sürümü göster" in out
    for sub in ("alice doctor", "alice voice", "alice plugins", "alice core"):
        assert sub in out


def test_typo_subcommand_suggests_instead_of_starting_agent(monkeypatch, capsys):
    _no_agent(monkeypatch)
    assert cli.main(["doctr"]) == 2
    out = capsys.readouterr().out
    assert "alice doctor" in out and "Bilinmeyen komut" in out
    assert cli.main(["status"]) == 2
    assert "alice core status" in capsys.readouterr().out


def test_unknown_command_hint_leaves_real_prompts_alone():
    assert cli.unknown_command_hint(["merhaba"]) is None
    assert cli.unknown_command_hint(["notlar"]) is None
    assert cli.unknown_command_hint(["dotcor", "bak"]) is None  # more than one word: it is a message
    assert cli.unknown_command_hint(["modles"]) is not None
    assert cli.unknown_command_hint(["voise"]) is not None


@pytest.mark.parametrize("sub,needle", [("phone", "alice phone"), ("quick", "alice quick"),
                                        ("notes", "alice notes"), ("core", "[--tray]"), ("voice", "--no-wake"),
                                        ("watch", "alice watch"), ("dcs", "alice dcs"), ("plugins", "alice plugins")])
def test_subcommand_help_does_not_run_the_command(sub, needle, monkeypatch, capsys):
    # `alice phone --help` must not open the bridge and wait forever, and `alice notes --help` must not list the notes.
    import alice.app.phone as phone

    monkeypatch.setattr(phone, "PhoneServer", lambda *a, **k: pytest.fail("köprü açılmamalıydı"))
    assert cli.main([sub, "--help"]) == 0
    assert needle in capsys.readouterr().out  # rich markup must not swallow the square brackets


def test_strict_args_for_simple_commands(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor", "--bogus"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "tanınmayan argüman" in err and "alice doctor --help" in err


def test_invalid_mode_message_is_turkish(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--mode", "bad"])
    err = capsys.readouterr().err
    assert "geçersiz seçim" in err and "seçenekler:" in err


def test_missing_optional_extra_prints_install_hint(monkeypatch, capsys):
    def boom(_argv):
        raise ModuleNotFoundError("No module named 'sounddevice'", name="sounddevice")

    monkeypatch.setattr(cli, "_dispatch", boom)
    assert cli.main(["voice"]) == 1
    out = capsys.readouterr().out
    assert "Eksik paket: sounddevice" in out and 'alice-harness[voice]' in out
    assert "Traceback" not in out


def test_unknown_missing_module_is_not_hidden(monkeypatch):
    def boom(_argv):
        raise ModuleNotFoundError("No module named 'yok_boyle_bir_sey'", name="yok_boyle_bir_sey")

    monkeypatch.setattr(cli, "_dispatch", boom)
    with pytest.raises(ModuleNotFoundError):
        cli.main(["voice"])


def test_install_hint_maps_extras():
    assert "[browser]" in cli.install_hint("playwright.sync_api")
    assert "[voice-local]" in cli.install_hint("faster_whisper")
    assert cli.install_hint("qrcode") == "pip install qrcode"


def test_oneshot_without_prompt(monkeypatch, capsys):
    _no_agent(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["-p"]) == 2
    assert "-p için görev yok" in capsys.readouterr().out


def test_invalid_model_ref(monkeypatch, capsys):
    _no_agent(monkeypatch)
    assert cli.main(["--model", "sadece-ad", "selam", "dünya"]) == 2
    assert "--model geçersiz" in capsys.readouterr().out


def test_check_ready_warns_when_no_api_keys(isolated_home, capsys, monkeypatch):
    import os
    import re

    from alice.providers.router import Router

    cfg = load_config()
    # Really make the environment keyless here: numbered keys (e.g. GEMINI_API_KEY_2 from .env) left by another test in
    # the same CI process were not in isolated_home's list, so no warning was printed.
    envs = {pc.get("api_key_env") for pc in (cfg.data.get("providers") or {}).values() if pc.get("api_key_env")}
    for k in list(os.environ):
        if any(k == e or re.fullmatch(rf"{re.escape(e)}_\d+", k) for e in envs):
            monkeypatch.delenv(k, raising=False)
    router = Router(cfg)
    if router.first_available("brain") is None:  # with no keys and no Ollama, the warning is already "model yok"
        assert not cli._check_ready(router)
        return
    assert cli._check_ready(router)
    out = capsys.readouterr().out
    assert "No API key at all" in out and "GEMINI_API_KEY" in out


def test_reflex_rejects_bad_input_before_starting(capsys):
    a = argparse.Namespace(goal="x", target="browser", url=None, title=None, input=["eksik"], max_steps=1, close=True)
    assert cli.reflex_cmd(load_config(), a) == 2
    assert "field=text" in capsys.readouterr().out


def test_report_days_must_be_number(capsys):
    assert cli.main(["report", "--days", "x"]) == 2
    assert "--days" in capsys.readouterr().out


def test_core_unknown_subcommand_keeps_brackets(capsys):
    from alice.core.cli import core_cmd

    assert core_cmd(["foo"]) == 2
    out = capsys.readouterr().out
    assert "unknown core command: foo" in out and "[--tray]" in out


def test_voice_unknown_subcommand(capsys):
    from alice.voice.cli import voice_cmd

    assert voice_cmd(["foo"]) == 2
    out = capsys.readouterr().out
    assert "unknown voice command: foo" in out and "[--voices" in out
