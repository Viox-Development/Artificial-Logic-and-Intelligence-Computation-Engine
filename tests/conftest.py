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
    from alice.providers import ranking

    monkeypatch.setattr(ranking, "_health", None)  # gerçek kullanım kaydı her testte boş başlasın
    from alice.providers import arena

    monkeypatch.setattr(arena, "_cache", None)  # LMArena verisi yok: testler elle yazılmış tabloyla çalışır
    monkeypatch.setattr(ranking, "_offset", (0.0, 0))
    for k in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "CEREBRAS_API_KEY",
              "TYPESAFE_API_KEY", "MISTRAL_API_KEY", "SAMBANOVA_API_KEY", "GITHUB_MODELS_TOKEN", "ZAI_API_KEY",
              "COHERE_API_KEY", "AI_GATEWAY_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "HF_TOKEN",
              "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    return home


@pytest.fixture(autouse=True)
def close_sqlite_stores(monkeypatch):
    """Testlerin açtığı SQLite depolarını test sonunda kapat (ResourceWarning: unclosed database)."""
    from alice import memory
    from alice.core.store import Store

    opened: list[Any] = []
    for cls in (Store, memory.LongMemory):
        def init(self, *a, _orig=cls.__init__, **kw):
            _orig(self, *a, **kw)
            opened.append(self)
        monkeypatch.setattr(cls, "__init__", init)
    yield
    for k in [k for k, m in memory._shared.items() if m in opened]:
        memory._shared.pop(k, None)
    for s in opened:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(autouse=True)
def no_project_scan(monkeypatch):
    """Testler gerçek ev klasörünü (ve Windows'ta diğer sürücüleri) proje diye taramasın."""
    from alice import projects

    monkeypatch.setattr(projects, "enabled", lambda cfg=None: False)


@pytest.fixture(autouse=True)
def no_real_keyboard(monkeypatch):
    """Testler gerçek klavye girdisi göndermesin (send_unicode Windows'ta öndeki pencereye yazar). Bir test kendi
    sahtesini koyabilir."""
    from alice.tools import computer

    monkeypatch.setattr(computer, "send_unicode", lambda text: False)
    monkeypatch.setattr(computer, "unpark_mouse", lambda x, y: False)  # gerçek fare de oynamasın


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


def patch_sleep(monkeypatch, module, fn) -> None:
    """Sadece `module`'ün gördüğü time.sleep'i değiştir. `monkeypatch.setattr(module.time, "sleep", ...)` süreçteki
    bütün time.sleep'i değiştirir; paralel testte (pytest-xdist) işçinin kendi iş parçacıkları da sahte sleep'e
    düşer, KeyboardInterrupt atan sahte sleep işçiyi öldürür."""
    import time as real_time
    from types import SimpleNamespace

    fake = SimpleNamespace(**{k: getattr(real_time, k) for k in dir(real_time) if not k.startswith("_")})
    fake.sleep = fn
    monkeypatch.setattr(module, "time", fake)
