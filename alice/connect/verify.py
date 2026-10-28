"""Servisin "tamam" dediği ama yapmadığı işleri yakala.

Spotify: masaüstü uygulamasının Connect oturumu bayatlayınca cihaz `is_active=true` görünür, `play` 204 döner,
ama hiçbir şey çalmaz (oynatıcı durumu, zaman damgası dahil, hiç değişmez). Cihaza aktarmak da uyandırmaz.
Parçayı `spotify:` bağlantısıyla yerelde açmak hem çalar hem oturumu canlandırır.

Uygulama açık ama bir süredir boştaysa (ya da hiç açık değilse) Spotify `play`e 404 NO_ACTIVE_DEVICE der. Bu da aynı
yoldan çözülür; modele hata dönüp ona cihaz aratıp tekrar denetmek gereksiz bir tur ve "1 hata" demekti."""

from __future__ import annotations

import os
from typing import Any

from alice.connect import runner
from alice.connect.manifest import Action, Manifest

POLL = 0.5          # saniye
API_WAIT = 3.0      # play'den sonra API'nin çalmaya başlamasını bu kadar bekle
LOCAL_WAIT = 12.0   # yerelde açtıktan sonra uygulamanın çalmasını bu kadar bekle (kapalıysa açılması da sürer)


def after(m: Manifest, act: Action, args: dict[str, Any], data: Any) -> str:
    """Eylemden sonra gerçekten olduğunu doğrula; modele eklenecek not (boş: söylenecek bir şey yok)."""
    if m.name == "spotify" and act.name == "play":
        return _spotify_play(m, args)
    return ""


def recover(m: Manifest, act: Action, args: dict[str, Any], err: Exception) -> str:
    """Eylem hata verdi ama başka yoldan yapılabiliyorsa yap; modele gidecek sonuç (boş: hata olduğu gibi kalsın)."""
    if m.name == "spotify" and act.name == "play" and "NO_ACTIVE_DEVICE" in str(err):
        return _spotify_local(m, args, "Spotify'da etkin cihaz yoktu")
    return ""


def _spotify_playing(m: Manifest) -> bool:
    act = m.actions.get("now_playing")
    if act is None:
        return True  # doğrulayamıyoruz; bozuk not yazmayalım
    try:
        data = runner.call(m, act, {}, cache=False)
    except runner.ConnectError:
        return False
    return bool(isinstance(data, dict) and data.get("is_playing"))


def _wait_playing(m: Manifest, seconds: float) -> bool:
    waited = 0.0
    while True:
        if _spotify_playing(m):
            return True
        if waited >= seconds:
            return False
        runner._sleep(POLL)
        waited += POLL


def _open_local(uri: str) -> bool:
    start = getattr(os, "startfile", None)
    if start is None:
        return False
    try:
        start(uri)
    except OSError:
        return False
    return True


def _spotify_play(m: Manifest, args: dict[str, Any]) -> str:
    if _wait_playing(m, API_WAIT):
        return "doğrulandı: çalıyor"
    return _spotify_local(m, args, "API 'tamam' dedi ama çalmadı")


def _spotify_local(m: Manifest, args: dict[str, Any], why: str) -> str:
    """Parçayı (uri yoksa uygulamayı) yerelde aç; uygulama kapalıysa açılır. Sonra gerçekten çalıyor mu bak."""
    uris = [str(u) for u in (args.get("uris") or []) if str(u).startswith("spotify:")]
    if not _open_local(uris[0] if uris else "spotify:"):
        return (f"UYARI: {why} ve Spotify uygulaması açılamadı. Spotify uygulamasını açıp tekrar dene.")
    if not uris:  # uygulama öne geldi: kaldığı yerden devam ettir
        runner._sleep(2.0)
        try:
            runner.call(m, m.actions["play"], {}, cache=False)
        except runner.ConnectError:
            pass
    if _wait_playing(m, LOCAL_WAIT):
        return f"{why}; parça Spotify uygulamasında açıldı, doğrulandı: çalıyor"
    return (f"UYARI: {why}; Spotify uygulamasında açıldı ama çalmaya başlamadı. "
            "Spotify uygulamasını kapatıp açmak gerekebilir.")
