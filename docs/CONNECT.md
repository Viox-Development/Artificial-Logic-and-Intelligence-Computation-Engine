# Bağlantılar: bir siteyi ALICE'e öğretmek

ALICE'in bir servisi kullanabilmesi için kod yazmak gerekmiyor. Bir TOML dosyası yazıyorsun, ALICE onu
şemalı bir araca çeviriyor: model eylemleri ve parametreleri görüyor, kimlik otomatik ekleniyor, cevap
okunur hale getiriliyor.

```powershell
alice connect                      # bağlantılar ve durumları
alice connect spotify              # OAuth: tarayıcıda izin al
alice connect steam --test library --arg steamid=7656119...
alice connect benimsite --new      # şablon oluştur: ~/.alice/connect/benimsite.toml
alice connect spotify --logout     # saklanan token'ı sil
```

Yerleşik tanımlar `alice/connect/builtin/*.toml`; seninkiler `~/.alice/connect/*.toml` (aynı ad varsa
seninki geçerli). `config.toml`:

```toml
[connect]
disabled = ["discord"]    # kapat
paths = ["D:/alice-baglantilar"]   # ek klasör
```

Araçlar `connect` grubunda duruyor: her isteğe eklenmiyorlar, mesajda servis adı geçince ya da model
`load_tools(["connect"])` dediğinde açılıyorlar.

## Tanım dosyası

```toml
name = "steam"                 # araç adı: küçük harf, rakam, alt çizgi
title = "Steam"
description = "Modelin okuyacağı açıklama: bu servis ne işe yarar."
base_url = "https://api.steampowered.com"
keywords = "steam|oyun|kütüphane"      # bu kelimeler geçince grup kendiliğinden açılır
docs = "https://steamcommunity.com/dev"
```

### Kimlik: `[auth]`

| kind | ne yapar | gereken alanlar |
|---|---|---|
| `none` | kimliksiz açık API | — |
| `api_key` | anahtarı query'ye ya da header'a koyar | `env`, `place` (query/header), `name`, `prefix` |
| `bearer` | `Authorization: Bearer <anahtar>` | `env` |
| `oauth2` | tarayıcıda izin, token saklama/yenileme | `client_id_env`, `authorize_url`, `token_url`, `scopes`, `redirect_port` |

Anahtarlar `.env`'den okunur (`env = "STEAM_API_KEY"`), hiçbir yere kopyalanmaz. `help` alanına anahtarın
nereden alınacağını yaz; ALICE anahtar eksikse bunu gösteriyor.

OAuth2'de akış şu: ALICE `127.0.0.1`'de bir porta tek seferlik sunucu açar, tarayıcıda izin sayfasını
açar, dönen kodu token'la takas eder ve `~/.alice/connect.json`'a (chmod 600) yazar. PKCE kullanıldığı
için client secret gerekmiyor. Servis geri dönüş adresini önceden kaydettiriyorsa (Spotify) `redirect_port`
ver ve servisin panelinde `http://127.0.0.1:<port>/callback` adresini kaydet.

### Eylemler: `[actions.<ad>]`

```toml
[actions.library]
summary = "Kütüphanedeki oyunlar"        # modele giden tek satırlık açıklama
method = "GET"                            # GET | POST | PUT | PATCH | DELETE
path = "/IPlayerService/GetOwnedGames/v1/"   # {param} yer tutucusu kullanılabilir
query = { format = "json" }               # her istekte giden sabit query
headers = { X-Thing = "1" }
body = { fixed = true }                   # POST/PUT için sabit gövde
pick = "response.games"                   # cevabın hangi dalı gösterilsin
fields = ["name", "playtime_forever"]     # listelerde gösterilecek alanlar (iç içe: "snippet.title")
limit = 40                                # en fazla kaç satır
writes = true                             # izin sistemi "yazma" saysın (GET dışında varsayılan true)
sends = true                              # başkasına gidiyor (mesaj, gönderi): auto modda da onay sorulur
cache_seconds = 300                       # okuyan eylemin cevabı bu kadar saniye hatırlanır (0: hiç)

[actions.library.params.steamid]
type = "string"        # string | integer | number | boolean | array
required = true
description = "SteamID64"
into = "query"         # query | path | body | header (boş: GET'te query, POST'ta body)
enum = ["a", "b"]
default = "a"
```

Model aracı `action` parametresiyle çağırıyor: `steam(action="library", steamid="765...")`.

## Yerleşik bağlantılar

| ad | kimlik | ne yapar |
|---|---|---|
| `steam` | `STEAM_API_KEY` | kütüphane, oynama süreleri, arkadaşlar, oyuncu durumu, oyun haberleri |
| `youtube_api` | `YOUTUBE_API_KEY` | video/kanal arama, video istatistikleri, yorumlar |
| `discord` | `DISCORD_BOT_TOKEN` | sunucular, kanallar, son mesajlar, mesaj gönderme (bot hesabıyla) |
| `spotify` | OAuth2 + `SPOTIFY_CLIENT_ID` | çalan parça, çal/duraklat/sonraki, arama, cihazlar, çalma listeleri |

## Resmî API'si olmayan siteler

WhatsApp, Instagram ve TikTok'un kişisel hesap için kullanılabilir resmî API'si yok: WhatsApp Cloud API
işletme numarası istiyor, Instagram ve TikTok'un API'leri işletme/içerik üretici hesabına ve onay
sürecine bağlı. Bunlar için doğru yol API değil, ALICE'in kalıcı profille açtığı gerçek tarayıcı
(`browser` aracı) ve reflex modu: bir kere giriş yapıyorsun, oturum açık kalıyor. Bu siteler için hazır
tarifler `alice/apps.toml` içinde duruyor; `app` aracı onları çalıştırıyor (bkz. README, "Uygulama tarifleri").

## Akışlar: bağlantıyı izlemek

Bir bağlantının eylemini arka planda yoklayıp yeni öğeleri bildirime çevirebilirsin: `~/.alice/feeds.toml`
(`alice feeds --new` örnek dosya oluşturur). Çekirdek servis (`alice core run`) çalışırken yoklanır.

```toml
[[feed]]
name = "yeni_video"
connector = "youtube_api"
action = "search"
args = { q = "DCS World update", type = "video", maxResults = "5" }
every_minutes = 30
id_field = "id.videoId"
title = "Yeni video: {snippet.title}"
body = "{snippet.channelTitle}"
```

`agent = "..."` eklersen yeni öğe geldiğinde ALICE uyanıp o işi yapar (özetlemek, bir şeyi kontrol etmek).
İlk tur sessizdir; bir turda en fazla 5 bildirim gider.

Hangi öğenin bildirileceğini `[feed.when]` (any/all/none/regex/fields/min/max/ask), hangisinin acil
olduğunu `[feed.escalate]` belirler; `mode = "change"` ile bir alanın değişmesi izlenir, `quiet`,
`digest_minutes` ve `cooldown_minutes` teslim zamanını ayarlar. Tam liste ve örnekler README'de,
"Akışlar" bölümünde. Koşulu kurarken `alice feeds --test <ad>` her öğenin neden geçtiğini/elendiğini
gösterir.

## Hız ve dayanıklılık

* `cache_seconds`: yavaş değişen veriyi (Steam kütüphanesi, YouTube video bilgisi, Discord kanal listesi)
  hatırlar; ajan aynı soruyu adım adım tekrar sorduğunda istek ağa çıkmaz. Varsayılan kapalı. Yazan ve
  gönderen eylemler asla önbellekten dönmez; akışlar her zaman taze ister.
* Tekrar deneme bir kez ve kısa: 429'da (sunucu isteği işlemeden reddetti) Retry-After 5 sn'den kısaysa
  beklenip tekrar denenir. Ağ hatası ve 502/503/504'te sadece okuyan eylemler tekrar denenir: mesaj
  gönderen bir isteği tekrarlamak aynı mesajı iki kez gönderebilir.

## İpuçları

* Önce `--test` ile dene: `alice connect <ad> --test <eylem> --arg k=v`. Çıktıyı modelin göreceği
  biçimde yazar, `fields` seçimini buna bakarak ayarla.
* `fields` vermezsen cevap JSON olarak gider; uzun JSON hem yavaştır hem kotayı yer. Gerçekten gereken
  alanları seç.
* Bozuk bir tanım ALICE'i durdurmaz: atlanır ve sebebi `alice connect` çıktısında görünür.
