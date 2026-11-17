from alice.config import init_home


def test_old_env_gets_new_provider_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=abc\nGROQ_API_KEY=xyz\n", encoding="utf-8")
    init_home()
    text = env.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=abc" in text and "GROQ_API_KEY=xyz" in text
    assert "\nNVIDIA_API_KEY=\n" in text and "build.nvidia.com" in text
    assert text.count("GEMINI_API_KEY") == 1
    init_home()
    assert env.read_text(encoding="utf-8") == text


def test_set_config_value_header_with_comment_and_multiline_value(tmp_path, monkeypatch):
    from alice.config import tomllib

    from alice.config import remove_config_section, set_config_value

    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "﻿[kairos]                 # arka plan nöbeti\nenabled = false\nmin_minutes = 20\n\n"
        "[voice]\nmodels = [\"a\",\n          \"b\"]\nlive = 1\n\n[x.y]  # not\nk = 1\n\n[z]\nq = 2\n",
        encoding="utf-8")
    set_config_value("kairos", "enabled", True)
    set_config_value("voice", "models", ["c"])
    data = tomllib.loads(cfg.read_text(encoding="utf-8-sig"))  # a single [kairos] section; the file is valid TOML
    assert data["kairos"] == {"enabled": True, "min_minutes": 20}
    assert data["voice"] == {"models": ["c"], "live": 1}
    assert remove_config_section("x.y")
    data = tomllib.loads(cfg.read_text(encoding="utf-8-sig"))
    assert "x" not in data and data["z"] == {"q": 2}


def test_set_config_value_on_default_config_stays_valid(tmp_path, monkeypatch):
    from alice.config import tomllib

    from alice.config import set_config_value

    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    init_home()
    set_config_value("kairos", "enabled", True)
    set_config_value("buddy", "chatter", False)
    set_config_value("voice", "gemini_tts_models", ["m1"])
    data = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert data["kairos"]["enabled"] is True and data["buddy"]["chatter"] is False
    assert data["voice"]["gemini_tts_models"] == ["m1"] and data["voice"]["stt_cpu_model"] == "small"


def test_env_file_with_bom_loads_first_key(tmp_path, monkeypatch):
    import os

    from alice.config import load_env_file

    monkeypatch.delenv("BOMTEST_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("﻿BOMTEST_KEY=abc\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ.get("BOMTEST_KEY") == "abc"
    monkeypatch.delenv("BOMTEST_KEY", raising=False)


def test_new_env_file_is_private_on_posix(tmp_path, monkeypatch):
    from alice import config

    calls = []
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.os, "chmod", lambda p, mode: calls.append((str(p), mode)))
    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    config.init_home()
    assert (str(tmp_path / ".env"), 0o600) in calls
    monkeypatch.setattr(config.sys, "platform", "win32")
    calls.clear()
    config.make_private(tmp_path / ".env")
    assert calls == []  # Windows: the profile ACL protects it
