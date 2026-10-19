"""Bağlantı katmanı: tanım okuma, istek kurma, kimlik, cevap biçimleme, OAuth, araç üretimi. Ağa çıkılmaz."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from alice.connect import manifest as mf
from alice.connect import oauth, runner, store
from alice.connect.tool import ConnectTool, connector_tools
from alice.tools.base import ToolContext, ToolError
from tests.conftest import make_config

STEAM = """
name = "deneme"
title = "Deneme"
description = "Test servisi"
base_url = "https://api.example.com"

[auth]
kind = "api_key"
env = "DENEME_KEY"
place = "query"
name = "key"
help = "anahtarı buradan al"

[actions.liste]
summary = "Şeyleri listele"
path = "/v1/things"
query = { format = "json" }
pick = "response.items"
fields = ["name", "count"]
[actions.liste.params.owner]
type = "string"
required = true
description = "Sahip"

[actions.gonder]
summary = "Bir şey gönder"
method = "POST"
path = "/v1/box/{box_id}/items"
writes = true
[actions.gonder.params.box_id]
type = "string"
required = true
into = "path"
description = "Kutu"
[actions.gonder.params.text]
type = "string"
required = true
description = "İçerik"
"""


@pytest.fixture
def m(tmp_path):
    p = tmp_path / "deneme.toml"
    p.write_text(STEAM, encoding="utf-8")
    return mf.load_file(p)


class FakeHTTP:
    """httpx yerine: isteği kaydeder, hazır cevabı döndürür."""

    def __init__(self, payload=None, status=200, text=""):
        self.payload, self.status, self.text_body = payload, status, text
        self.seen: dict = {}

    def request(self, method, url, params=None, headers=None, json=None, timeout=None):
        self.seen = {"method": method, "url": url, "params": params, "headers": headers, "json": json}
        return self

    @property
    def content(self):
        return b"x" if (self.payload is not None or self.text_body) else b""

    @property
    def status_code(self):
        return self.status

    @property
    def text(self):
        return self.text_body

    def json(self):
        if self.payload is None:
            raise ValueError("json değil")
        return self.payload


# ------------------------------------------------------------------------------ tanım
def test_bozuk_tanim_anlasilir_hata(tmp_path):
    bad = tmp_path / "x.toml"
    bad.write_text('name = "X Y"\nbase_url = "https://a"\n', encoding="utf-8")
    with pytest.raises(mf.ManifestError):
        mf.load_file(bad)
    bad.write_text('name = "ok"\nbase_url = "ftp://a"\n[actions.a]\n', encoding="utf-8")
    with pytest.raises(mf.ManifestError, match="base_url"):
        mf.load_file(bad)
    bad.write_text('name = "ok"\nbase_url = "https://a"\n', encoding="utf-8")
    with pytest.raises(mf.ManifestError, match="actions"):
        mf.load_file(bad)
    bad.write_text('name = "ok"\nbase_url = "https://a"\n[auth]\nkind = "api_key"\n[actions.a]\n', encoding="utf-8")
    with pytest.raises(mf.ManifestError, match="auth.env"):
        mf.load_file(bad)


def test_bozuk_tanim_digerlerini_dusurmez(tmp_path, monkeypatch):
    d = tmp_path / "connect"
    d.mkdir()
    (d / "iyi.toml").write_text(STEAM, encoding="utf-8")
    (d / "kotu.toml").write_text("bu toml değil ===", encoding="utf-8")
    monkeypatch.setattr(mf, "user_dir", lambda: d)
    found, errors = mf.discover(make_config({"brain": ["a:m"]}))
    assert "deneme" in found and errors and "kotu" in errors[0]


def test_yerlesik_tanimlar_gecerli():
    found, errors = mf.discover()
    assert not errors
    assert {"steam", "spotify", "discord", "youtube_api"} <= set(found)
    for man in found.values():
        assert man.actions and man.base_url.startswith("https://")


def test_kullanici_tanimi_yerlesigi_ezer(tmp_path, monkeypatch):
    d = tmp_path / "connect"
    d.mkdir()
    (d / "steam.toml").write_text(STEAM.replace('name = "deneme"', 'name = "steam"'), encoding="utf-8")
    monkeypatch.setattr(mf, "user_dir", lambda: d)
    found, _ = mf.discover()
    assert found["steam"].title == "Deneme"


def test_config_ile_kapatilir(monkeypatch):
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["connect"] = {"disabled": ["discord", "spotify"]}
    found, _ = mf.discover(cfg)
    assert "discord" not in found and "steam" in found


# ------------------------------------------------------------------------------ istek kurma
def test_istek_kurulur_anahtar_query_ye_konur(m, monkeypatch):
    monkeypatch.setenv("DENEME_KEY", "gizli")
    req = runner.build(m, m.actions["liste"], {"owner": "hayat"})
    assert req["url"] == "https://api.example.com/v1/things"
    assert req["params"] == {"format": "json", "owner": "hayat", "key": "gizli"}
    assert req["json"] is None and req["method"] == "GET"


def test_post_parametreleri_govdeye_yol_parametresi_adrese(m, monkeypatch):
    monkeypatch.setenv("DENEME_KEY", "gizli")
    req = runner.build(m, m.actions["gonder"], {"box_id": "a/b", "text": "selam"})
    assert req["url"].endswith("/v1/box/a%2Fb/items")  # yol parametresi kaçışlanır
    assert req["json"] == {"text": "selam"}


def test_eksik_zorunlu_parametre(m, monkeypatch):
    monkeypatch.setenv("DENEME_KEY", "k")
    with pytest.raises(runner.ConnectError, match="eksik parametre: owner"):
        runner.build(m, m.actions["liste"], {})


def test_anahtar_yoksa_anlasilir_hata(m, monkeypatch):
    monkeypatch.delenv("DENEME_KEY", raising=False)
    with pytest.raises(runner.ConnectError, match="DENEME_KEY"):
        runner.build(m, m.actions["liste"], {"owner": "x"})
    assert runner.ready(m) == (False, "DENEME_KEY yok — anahtarı buradan al")


def test_bearer_ve_prefix_header_a_gider(tmp_path, monkeypatch):
    src = STEAM.replace('place = "query"\nname = "key"', 'place = "header"\nname = "Authorization"\nprefix = "Bot "')
    p = tmp_path / "b.toml"
    p.write_text(src, encoding="utf-8")
    man = mf.load_file(p)
    monkeypatch.setenv("DENEME_KEY", "T0K3N")
    req = runner.build(man, man.actions["liste"], {"owner": "x"})
    assert req["headers"]["Authorization"] == "Bot T0K3N"
    assert "key" not in req["params"]


# ------------------------------------------------------------------------------ çağrı ve cevap
def test_cagri_ve_liste_bicimleme(m, monkeypatch):
    monkeypatch.setenv("DENEME_KEY", "k")
    http = FakeHTTP({"response": {"items": [{"name": "bir", "count": 3}, {"name": "iki", "count": 9}]}})
    data = runner.call(m, m.actions["liste"], {"owner": "x"}, client=http)
    out = runner.render(data, m.actions["liste"])
    assert out == "1. name=bir  count=3\n2. name=iki  count=9"
    assert http.seen["params"]["owner"] == "x"


def test_limit_asilinca_kalan_sayisi_yazilir(m):
    act = m.actions["liste"]
    act.limit = 2
    data = {"response": {"items": [{"name": str(i), "count": i} for i in range(5)]}}
    assert runner.render(data, act).endswith("… 3 sonuç daha")


def test_http_hatalari_anlasilir(m, monkeypatch):
    monkeypatch.setenv("DENEME_KEY", "k")
    for status, parca in ((401, "yetki"), (429, "hız sınırı"), (500, "HTTP 500")):
        with pytest.raises(runner.ConnectError, match=parca):
            runner.call(m, m.actions["liste"], {"owner": "x"}, client=FakeHTTP(None, status, "patladı"))


def test_bos_sonuc(m):
    assert runner.render({"response": {"items": []}}, m.actions["liste"]) == "sonuç yok"
    assert runner.render({}, m.actions["liste"]) == "sonuç yok"


def test_pick_ic_ice_alan_okur():
    act = mf.Action(name="a", fields=["snippet.title"])
    assert runner.pick({"a": {"b": [1, 2]}}, "a.b.1") == 2
    assert runner.render([{"snippet": {"title": "Başlık"}}], act) == "1. snippet.title=Başlık"


# ------------------------------------------------------------------------------ araç
def test_arac_semasi_ve_calismasi(m, workdir, monkeypatch):
    monkeypatch.setenv("DENEME_KEY", "k")
    tool = ConnectTool(m)
    props = tool.parameters["properties"]
    assert tool.name == "deneme" and props["action"]["enum"] == ["gonder", "liste"]
    assert tool.parameters["required"] == ["action"]
    assert "Şeyleri listele" in tool.description
    assert tool.risk_for({"action": "gonder"}) == "write"   # POST: izin sorulsun
    assert tool.risk_for({"action": "liste"}) == "network"

    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    monkeypatch.setattr("alice.connect.tool.call", lambda *a, **k: {"response": {"items": [{"name": "x", "count": 1}]}})
    assert tool.run(ctx, action="liste", owner="hayat").text == "1. name=x  count=1"
    with pytest.raises(ToolError, match="bilinmeyen eylem"):
        tool.run(ctx, action="yok")


def test_anahtarsiz_arac_calismaz_ama_listede_durur(m, workdir, monkeypatch):
    monkeypatch.delenv("DENEME_KEY", raising=False)
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    tool = ConnectTool(m)
    assert "kullanılamaz" in tool.description
    with pytest.raises(ToolError, match="DENEME_KEY"):
        tool.run(ctx, action="liste", owner="x")


def test_yerlesik_arac_adi_cakisirsa_atlanir(monkeypatch, tmp_path):
    d = tmp_path / "connect"
    d.mkdir()
    (d / "web.toml").write_text(STEAM.replace('name = "deneme"', 'name = "web_search"'), encoding="utf-8")
    monkeypatch.setattr(mf, "user_dir", lambda: d)
    notes: list[str] = []
    tools = connector_tools(make_config({"brain": ["a:m"]}), notes.append, reserved={"web_search"})
    assert "web_search" not in {t.name for t in tools}
    assert any("zaten bir araç adı" in n for n in notes)


def test_baglanti_araclari_connect_grubunda():
    from alice.tools import all_tools
    from alice.tools.groups import group_of

    names = {t.name for t in all_tools(make_config({"brain": ["a:m"]}))}
    assert {"steam", "spotify"} <= names
    assert group_of("steam") == "connect" and group_of("spotify") == "connect"


# ------------------------------------------------------------------------------ token saklama
def test_token_saklama_ve_silme():
    store.save("x", access_token="a", refresh_token="r", expires_at=0)
    assert store.get("x")["access_token"] == "a"
    assert "x" in store.names()
    assert not store.expired(store.get("x"))          # expires_at=0: süresiz say
    store.save("x", expires_at=1)
    assert store.expired(store.get("x"))
    assert store.forget("x") and not store.forget("x")
    assert store.get("x") == {}


def test_oauth_bagli_degilse_arac_soyler(monkeypatch):
    found, _ = mf.discover()
    spotify = found["spotify"]
    assert runner.ready(spotify)[0] is False
    store.save("spotify", access_token="tok", expires_at=0)
    assert runner.ready(spotify)[0] is True
    req = runner.build(spotify, spotify.actions["now_playing"], {})
    assert req["headers"]["Authorization"] == "Bearer tok"


def test_suresi_dolan_token_yenilenir(monkeypatch):
    found, _ = mf.discover()
    spotify = found["spotify"]
    store.save("spotify", access_token="eski", refresh_token="r", expires_at=1)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setattr(oauth, "refresh", lambda auth, name, cid, secret="": {"access_token": "yeni"})
    assert runner.token_for(spotify) == "yeni"


def test_yenileme_tokeni_yoksa_tekrar_baglan_denir():
    found, _ = mf.discover()
    spotify = found["spotify"]
    store.save("spotify", access_token="eski", expires_at=1, refresh_token="")
    with pytest.raises(runner.ConnectError, match="alice connect spotify"):
        runner.token_for(spotify)


# ------------------------------------------------------------------------------ OAuth akışı
def test_oauth_akisi_kodu_yakalar_ve_token_saklar(monkeypatch):
    """Gerçek loopback sunucusu: tarayıcı yerine adrese biz istek atıyoruz."""
    auth = mf.Auth(kind="oauth2", client_id_env="X_ID", authorize_url="https://servis.example/auth",
                   token_url="https://servis.example/token", scopes=["oku"], pkce=True)
    posted: dict = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "token_type": "Bearer"}

    def fake_post(url, data=None, headers=None, timeout=None):
        posted.update(url=url, data=data)
        return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    def browser(url: str):
        # Servis gibi davran: state'i al, koda karşılık yerel adrese dön.
        import urllib.parse as up

        q = up.parse_qs(up.urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["oku"]
        threading.Thread(target=lambda: urllib.request.urlopen(
            q["redirect_uri"][0] + f"?code=KOD&state={q['state'][0]}", timeout=5).read()).start()

    tok = oauth.authorize(auth, "servis", "cid", open_browser=browser, timeout=20)
    assert tok["access_token"] == "AT" and tok["refresh_token"] == "RT"
    assert store.get("servis")["access_token"] == "AT"
    assert posted["data"]["code"] == "KOD" and "code_verifier" in posted["data"]
    assert posted["data"]["redirect_uri"].startswith("http://127.0.0.1:")


def test_oauth_state_uyusmazsa_reddeder(monkeypatch):
    auth = mf.Auth(kind="oauth2", client_id_env="X_ID", authorize_url="https://s.example/a",
                   token_url="https://s.example/t")

    def browser(url: str):
        import urllib.parse as up

        q = up.parse_qs(up.urlparse(url).query)
        threading.Thread(target=lambda: urllib.request.urlopen(
            q["redirect_uri"][0] + "?code=KOD&state=baska", timeout=5).read()).start()

    with pytest.raises(oauth.OAuthError, match="state"):
        oauth.authorize(auth, "servis", "cid", open_browser=browser, timeout=20)
    assert store.get("servis") == {}


def test_oauth_zaman_asimi_adresi_soyler():
    auth = mf.Auth(kind="oauth2", client_id_env="X", authorize_url="https://s.example/a",
                   token_url="https://s.example/t")
    with pytest.raises(oauth.OAuthError, match="süre doldu"):
        oauth.authorize(auth, "servis", "cid", open_browser=lambda _u: None, timeout=0.5)


# ------------------------------------------------------------------------------ CLI
def test_cli_listeler_ve_sablon_olusturur(tmp_path, monkeypatch, capsys):
    from alice.connect.cli import connect_cmd

    monkeypatch.setattr(mf, "user_dir", lambda: tmp_path / "connect")
    assert connect_cmd([]) == 0
    assert "Bağlantılar" in capsys.readouterr().out
    assert connect_cmd(["mysite", "--new"]) == 0
    p = tmp_path / "connect" / "mysite.toml"
    assert p.exists() and 'name = "mysite"' in p.read_text(encoding="utf-8")
    assert connect_cmd(["mysite", "--new"]) == 1  # ikinci kez üzerine yazmaz
    assert connect_cmd(["yokboyle"]) == 1


def test_cli_test_eylemi_calistirir(tmp_path, monkeypatch, capsys):
    from alice.connect.cli import connect_cmd

    d = tmp_path / "connect"
    d.mkdir()
    (d / "deneme.toml").write_text(STEAM, encoding="utf-8")
    monkeypatch.setattr(mf, "user_dir", lambda: d)
    monkeypatch.setenv("DENEME_KEY", "k")
    monkeypatch.setattr("alice.connect.cli.call", lambda *a, **k: {"response": {"items": [{"name": "n", "count": 2}]}})
    assert connect_cmd(["deneme", "--test", "liste", "--arg", "owner=hayat"]) == 0
    assert "name=n" in capsys.readouterr().out
    assert connect_cmd(["deneme", "--test", "yok"]) == 1


def test_cli_logout_tokeni_siler(capsys):
    store.save("spotify", access_token="a")
    from alice.connect.cli import connect_cmd

    assert connect_cmd(["spotify", "--logout"]) == 0
    assert store.get("spotify") == {}


def test_store_dosyasi_json_ve_izinli(tmp_path):
    store.save("a", access_token="1")
    p = store.path()
    assert json.loads(p.read_text(encoding="utf-8"))["a"]["access_token"] == "1"
    if hasattr(Path, "stat"):
        mode = p.stat().st_mode & 0o777
        assert mode in (0o600, 0o666)  # Windows'ta chmod sınırlı
