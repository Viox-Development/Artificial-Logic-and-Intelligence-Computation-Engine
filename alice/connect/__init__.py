"""Bağlantılar: bir siteyi/servisi TOML tanımıyla ALICE aracına çevirme katmanı.

* `manifest.py` — tanım dosyasını okur ve doğrular (yerleşikler: `builtin/*.toml`, kullanıcınınkiler
  `~/.alice/connect/*.toml`).
* `runner.py`   — eylemi çalıştırır: isteği kurar, kimliği ekler, cevabı modele okunur hale getirir.
* `oauth.py`    — OAuth2 (Authorization Code + PKCE), tarayıcı ve yerel geri dönüş adresiyle.
* `store.py`    — token'lar `~/.alice/connect.json` içinde (chmod 600).
* `tool.py`     — her bağlantı tek bir araç; eylemler `action` parametresiyle seçilir.

Yeni servis eklemek için kod yazmak gerekmiyor: `alice connect <ad> --new` bir şablon açar.
"""

from __future__ import annotations

from alice.connect.manifest import Manifest, ManifestError, discover
from alice.connect.runner import ConnectError, ready
from alice.connect.tool import ConnectTool, connector_tools

__all__ = ["ConnectError", "ConnectTool", "Manifest", "ManifestError", "connector_tools", "discover", "ready"]
