from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from alice.config import Config, default_config_dict
from alice.providers.openai_compat import ChatResult, ProviderError, normalize_message


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "alice-home"
    monkeypatch.setenv("ALICE_HOME", str(home))
    for k in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "CEREBRAS_API_KEY",
              "TYPESAFE_API_KEY", "MISTRAL_API_KEY", "SAMBANOVA_API_KEY", "GITHUB_MODELS_TOKEN", "ZAI_API_KEY",
              "COHERE_API_KEY", "AI_GATEWAY_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "HF_TOKEN",
              "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    return home


@pytest.fixture(autouse=True)
def no_project_scan(monkeypatch):
    """Testler gerçek ev klasörünü (ve Windows'ta diğer sürücüleri) proje diye taramasın."""
    from alice import projects

    monkeypatch.setattr(projects, "enabled", lambda cfg=None: False)


@pytest.fixture(autouse=True)
def clear_web_cache(monkeypatch):
    """web_fetch/web_search ve bağlantı önbellekleri testler arasında sızmasın; tekrar deneme beklemesin."""
    from alice.connect import runner
    from alice.tools import web

    web.cache_clear()
    runner.cache_clear()
    monkeypatch.setattr(runner, "_sleep", lambda s: None)
    yield
    web.cache_clear()
    runner.cache_clear()


def rm_rf(name: str) -> str:
    """Özyinelemeli silme komutu: Windows'ta PowerShell, diğerlerinde bash."""
    import sys
    return f"Remove-Item -Recurse -Force {name}" if sys.platform == "win32" else f"rm -rf {name}"


class FakeProvider:
    """Sıradaki cevabı ya da hatayı script'ten döndüren sahte sağlayıcı."""

    def __init__(self, name: str, script: list[Any] | Callable[..., Any]):
        self.name = name
        self.script = script
        self.calls: list[dict[str, Any]] = []

    def chat(self, model, messages, tools=None, **kw) -> ChatResult:
        self.calls.append({"model": model, "messages": messages, "tools": tools, **kw})
        item = self.script(model, messages, tools, **kw) if callable(self.script) else self.script.pop(0)
        if isinstance(item, ProviderError):
            raise item
        if isinstance(item, str):
            item = {"content": item}
        return ChatResult(normalize_message(item), self.name, model, {"prompt_tokens": 10, "completion_tokens": 5})


def make_config(roles: dict[str, list[str]], providers: dict[str, dict] | None = None, **agent) -> Config:
    data = default_config_dict()
    data["roles"] = roles
    data["providers"] = providers or {
        "a": {"base_url": "http://a", "api_key_env": "", "max_input_tokens": 100000},
        "b": {"base_url": "http://b", "api_key_env": "", "max_input_tokens": 100000, "vision_models": ["*"]},
    }
    data["models"] = {}
    data["decider"]["chain"] = ["llm:fast"]
    data.setdefault("memory", {})["auto_extract"] = False  # sahte modelin cevap sırasını arka plan çıkarımı bozmasın
    data["agent"].update(agent)
    return Config(data)


def tool_call(name: str, args: dict, cid: str = "c1") -> dict:
    return {"content": "", "tool_calls": [{"id": cid, "type": "function",
                                           "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def workdir(tmp_path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d
