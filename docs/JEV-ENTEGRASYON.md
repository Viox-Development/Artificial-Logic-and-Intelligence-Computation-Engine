# Jev'i agent harness'larına bağlamak: başkaları nasıl yapıyor, biz ne aldık

## Kaynaklar
- **TypeSafe, [Jev with coding agents](https://docs.typesafe.ai/introduction/coding-agents.md)**: Jev ajanın modeli değil;
  yazı/kod üretmez, sohbet etmez. Hızlı, kalibre olasılıklı yargılar verir; karar ve akış kodda. Kalıplar: yönlendirme,
  işlem öncesi kontrol (gating), puanlama, "JSON döndür" istemleri yerine tipli cevap.
- **[jev-mcp](https://github.com/legostin/jev-mcp)** (Claude Code / Codex için tarayıcı):
  - ana model adım döngüsünün dışında: `jev_task` görevi başlatır; ana model başta, takılınca (`consult`) ve sonda
    devreye girer;
  - her adımda tek toplu Jev çağrısı (sayfa türü, kaplama, hedefe ulaşıldı mı, 0-4 ilerleme, durum);
  - rutin alt işleri kod kuralları yapar, Jev sadece kodun karar veremediği yerde;
  - karar başına `act` ve `escalate` eşikleri; arada kalan cevaba ikinci bakış (en iyi adaylar, iki bakışın ortalaması);
    geri alınabilir adım işe yaramazsa geri alıp sıradaki aday;
  - geri alınamaz adımlar (öde, sipariş, gönder, sil) onaysız yapılmaz; gizli değerleri Jev görmez;
  - her adım, olasılık ve süre izde; `jev calibrate` eşik önerir. Ölçülen: 4-7 adımlık görevler 10-17 sn.
- **[typesafe-computer-use](https://github.com/awlevin/typesafe-computer-use)** (masaüstü): adım başına tek istekte 3 Choice
  (eylem türü, öğe, yer). Tarih, satır, odaktaki alan, "bu ekranda denenmiş eylemler" gibi gerçekleri kod hesaplayıp verir.
  Yazıcı model sadece serbest metin yazar, eylem seçmez. Düşük güvende, takılınca ya da adım sınırında durur. Adım ~1,5 sn.
- **[jev-code](https://github.com/FrancoisChastel/jev-code)**: Jev'i Claude Code / Codex / OpenCode'a MCP aracı olarak
  verir (classify, check, score, rank, ask). Sonuç: olasılık, marj ve eşiklerden hesaplanan `auto | review` kararı.
  Politika (eşikler) kullanıcıda.

## ALICE'te farklı olan ve düzeltilenler
Genel görev sınavında (Hesap Makinesi, Not Defteri, Gezgin, Ayarlar, tarayıcı, Paint) sürenin çoğu Jev'de değil ana modelin
turlarındaydı (Jev adım başına 0,23-0,6 sn). Ana model her takılmada tam bir tur düşünüp yeni refleks başlatıyordu.

| İlke (kaynak) | ALICE'te |
|---|---|
| Ana model döngünün dışında, takılınca danışma (jev-mcp `consult`) | Refleks `needs_help` olunca önce **içeriden danışır**: sohbet geçmişsiz kısa bir çağrıyla "sıradaki somut adım ne?", onu yapar, asıl hedefe döner (en fazla 2). Ana modele sadece çıkılamazsa döner. |
| Geri alınamaz adımda onay (jev-mcp güvenlik kapısı) | Seçilen öğe gönder, öde, sil, satın al, onayla/kabul ise refleks durur (`needs_confirmation`). Ana model kullanıcı açıkça istediyse `allow_irreversible=true` verir. |
| Kararsızlıkta ikinci bakış (jev-mcp) | En iyi aday %60'ın altında ve ikincisi %15'in üstündeyse en iyi 4 aday için ikinci soru; iki bakışın ortalaması. |
| İşe yaramayanı tekrar deneme, sıradakine geç (jev-mcp, typesafe-computer-use) | Bir ekranda hiçbir şey değiştirmeyen öğe o ekranda bir daha seçilmez; Jev'e `tried_here_without_effect` diye kodun gerçeği olarak verilir. |
| Yazıcı sadece metin yazar (typesafe-computer-use) | `text` verilirse kod ilk adımda klavyeyle yazar (Jev tuşa tıklamayı seçmişti); uzun/çok satırlı metin panodan, işaretler Unicode. |
| Kod hesaplanabilen gerçeği verir | Rapor ve oyunda: başa göre çıkan/kaybolan yazılar, değişen bölgeler. |
| İz | `~/.alice/reflex-trace.jsonl`: her adımda öğe listesi, olasılıklar, süre, sonuç. |
