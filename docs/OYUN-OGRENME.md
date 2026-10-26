# Oyunu kendi öğrenen oturum: araştırma ve tasarım

Hedef: hangi oyunu açarsan aç, ALICE oynarken oyunu kendisi öğrensin. Koda oyuna özel bilgi yazılmasın: "tur düğmesi
sağ üstte", "Pick a construction sadece başlık" gibi bilgileri ilk Unciv denemesinde biz elle yazmıştık; başka bir oyunda
işe yaramazlar.

## Araştırma

| Çalışma | Ne yapıyor | Bize kalan |
|---|---|---|
| [Cradle](https://arxiv.org/abs/2403.03186) (RDR2, Cities: Skylines, Stardew Valley, Dealer's Life 2) | Her eylemden sonra önceki ve sonraki kareye bakıp "eylem işe yaradı mı, görev bitti mi, olmadıysa neden?" diye soruyor; sonuçlar hafızaya ve beceri kütüphanesine gidiyor. | Yansıtma eylem başına değil tur başına da işe yarar. Ama Cradle'ın istemleri oyuna özel; yeni oyuna geçmek elle istem yazmak demek. Onu yapmayacağız. Raporlanan zayıflık: alışılmadık ikonlar ve çizim tarzında görme. |
| [ExpeL](https://arxiv.org/abs/2308.10144) | Başarılı/başarısız denemelerden doğal dilde "içgörü" listesi çıkarıyor; listeyi ADD / EDIT / UPVOTE / DOWNVOTE işlemleriyle güncelliyor. Model ağırlığı değişmiyor. | Notlar her seferinde baştan yazılmaz, işlemlerle güncellenir; yanlış not oylarla düşer. |
| [Experiential Reflective Learning](https://www.alphaxiv.org/abs/2603.24639) (2026) | Her denemeden sonra (başarılı ya da değil) "tetik koşulu + talimat" biçiminde sezgi çıkarıyor, görevde sadece ilgili olanları getiriyor. ExpeL'den iyi (56,1% / 50,9%). Ham deneme kayıtlarını örnek diye vermek temel çizginin bile altında kalıyor (46,4%). Başarısızlıktan çıkan sezgiler daha faydalı. | Ders = `when` (ekranda ne görününce) + `do`. Her adımda sadece ekrandakine uyan dersler gider. Ham trace değil, damıtılmış ders. Başarısız turdan da ders çıkar. |
| [Cogito, Ergo Ludo](https://arxiv.org/abs/2509.25052) | Hiç kural bilmeden başlıyor; her bölümden sonra trajektoriye bakıp "kurallar bilgi tabanı" ve "strateji defteri" güncelliyor (Minesweeper, Frozen Lake, Sokoban). | Kurallar (arayüz/oyun nasıl işliyor) ve strateji ayrı tutulur. |
| [AppAgent](https://arxiv.org/abs/2312.13771) / [v2](https://arxiv.org/abs/2408.11824) | Bilinmeyen uygulamada öğelere dokunup önceki/sonraki ekrana bakarak her öğenin ne işe yaradığını belgeliyor; sonra o belgeyle çalışıyor. | Keşif = "bu yazıya tıklayınca ne oldu" kaydı. Bunu kod zaten trace'te tutuyor; LLM'e kanıt olarak verilir. |
| [Mobile-Agent-E](https://arxiv.org/abs/2501.11733) | Her görevden sonra "Experience Reflector" ipuçlarını (Tips) günceller; çok birikince sadece ilgili olanları seçen bir getirici var. | Aynı: oturumlar arası kalıcı ipuçları + seçici getirme. |
| [Voyager](https://arxiv.org/abs/2305.16291) | Ortam geri bildirimi + öz-doğrulama ile beceri kütüphanesi büyütüyor. | "Bitti" iddiası ortamdan gelen kanıta dayanmalı. |
| [Evidence-First Reflection](https://arxiv.org/abs/2608.24015) (2026) | Yansıtmadan önce eylemin yerini ve değişen bölgeleri bulup, eylemle ilgisiz değişiklikleri ayıklıyor; sonra karar veriyor. OSWorld'de +5,9 puan. | Önce kod kanıtı çıkarsın (ne çıktı, ne kayboldu, hangi sayı değişti), model ona baksın. |
| Adım doğrulama ([EchoPath](https://arxiv.org/abs/2609.16635), [VLAA-GUI](https://www.emergentmind.com/topics/vlaa-gui)) | Erken "bitti" demek başlıca hata; tamamlanma ekranda görünen kanıta bağlanıyor. Her eylem görünür değişiklik yapmayabilir. | "Bitti mi?" sorusuna turun başından beri değişenler verilir. Değişmeme tek başına hata sayılmaz (zaten iki tekrar kuralı var). |

TypeSafe kılavuzu (`.claude/skills/typesafe-ai`): Jev'e kodun zaten bildiği gerçekleri hazır ver ("state facts your code
already knows"), durumda ilgisiz alan olmasın, karar kodda olsun.

## Tasarım

### 1. Kendi notunu kendisi çıkarsın (yansıtma)
Her `turn` sonunda:
- **Kod kanıt çıkarır** (LLM yok): her adım için eylem, ekran değişti mi, o adımda çıkan ve kaybolan yazılar; boşa giden
  eylemler (iki kez değişmeyen), döngüler. Bunlar kesin bilgi.
- **Model dersleri günceller**: mevcut dersler + kanıt verilir; ExpeL gibi işlemler döner: `add` (when, do), `edit`,
  `drop`, `confirm`. Ders kanıta dayanmak zorunda; görmediği oyun olayını uyduramaz.
- Dersler `~/.alice/games/<oyun>/lessons.jsonl`: `when` (tetik: ekranda görünen yazı/durum), `do`, `kind`
  (ui | rule | strategy), `score` (confirm +1, çelişki −1; ≤ −2 olunca düşer), `source` (seen | prior).

### 2. Hazır not yok; ilk açılışta keşif
Notu olmayan oyunda `start`: ilk ekranın yazıları (OCR) + oyunun adı modele verilir; model oyun hakkında bildiklerinden
ve ekrandan başlangıç dersleri yazar. Bunlar `source: prior` (doğrulanmamış) işaretlenir; oyunda karşılığı görülünce
`confirm` ile güçlenir, çelişince düşer. Koddaki Unciv notları kalkar.

### 3. "Bitti mi?"yi değişimden anlasın
Fark kipinde ajan, adımın/turun başındaki ekranla şimdikini karşılaştırır ve Jev'in durumuna kod-hesaplı bir alan ekler:
`changes_since_start: {appeared: [...], disappeared: [...]}` (kısa, sayılarla birlikte). Tur sayacı, yıl, altın gibi
değerler hangi oyunda olursa olsun burada görünür. `done` sorusunun ölçütü bu alana atıf yapar.

### Seçici getirme
Her adımda Jev'e giden `notes`: `when`'i ekrandaki yazılarla eşleşen dersler + genel (tetiksiz) dersler, puanı yüksek
olanlar önce, sınırlı sayıda. Sonraki tura son üç tur özeti de gider.

### Yazısız değişim (Solitaire'den sonra)
İlk sürüm "ekran değişti mi?" sorusunu sadece okunan yazılarla cevaplıyordu. Solitaire'de desteye doğru tıklanıyor ve
kart açılıyordu ama OCR kart yüzünü okumadığı için "değişmedi" deniyor, tekrar koruması durduruyor, yansıtma da "menülerde
kayboldun" diye yanlış ders uyduruyordu. Düzeltmeler:
- değişim yazı **ya da** görüntüyle ölçülür (`image_changed`, karenin ≥ %0,1'i; yanıp sönen imleç sayılmaz);
- döngü anahtarına görüntünün kaba parmak izi girer (desteden art arda çekmek döngü değil);
- kanıtta yazısız değişimin yeri yazılır ("picture changed at: top-middle");
- Jev'e turun başındaki ekran ve **en büyük değişen bölgenin önce/sonra kırpıntısı** verilir (EFR gibi; saat gibi küçük
  değişimler elenir) ve soyut "tamamen bitti mi?" yerine gözlenebilir tek yargı sorulur: "istenen değişiklik öncekinde
  yok, şimdikinde var mı?". Canlıda kart çekildikten sonra bu yargı 0,36'dan 0,80'e çıktı; eşik 0,75.

### Ölçüm
Doğrulama Unciv'e bakarak değil, hiç not olmadan ikinci bir oyunla yapılır.
