package com.hayatexe.alice;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.os.Build;

/** Paket kurucunun cevabı: Android onay isterse onayı aç (uygulama önündeyse) ya da bildirimle sor. */
public class UpdateReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        int status = i.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            Intent confirm = Build.VERSION.SDK_INT >= 33
                    ? i.getParcelableExtra(Intent.EXTRA_INTENT, Intent.class)
                    : i.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm == null) return;
            confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            if (MainActivity.visible) {
                try {
                    c.startActivity(confirm);
                    return;
                } catch (Exception ignored) {
                    // arka plandan açılamadı: bildirime düş
                }
            }
            Updater.notify(c, "ALICE güncellemesi hazır", "Kurmak için dokun.", confirm);
        } else if (status == PackageInstaller.STATUS_FAILURE_CONFLICT || status == PackageInstaller.STATUS_FAILURE_INCOMPATIBLE) {
            Updater.notify(c, "ALICE güncellenemedi",
                    "Yeni sürüm farklı bir anahtarla imzalanmış. Uygulamayı bir kez silip bilgisayardaki QR'den yeniden kur.", null);
        } else if (status == PackageInstaller.STATUS_FAILURE_ABORTED) {
            // Kullanıcı onay ekranında "İptal"e bastı: bir sonraki kontrolde yeniden sorulur.
            Updater.notify(c, "ALICE güncellemesi iptal edildi", "Ayarlar → Uygulama'dan yeniden başlatabilirsin.", null);
        } else if (status != PackageInstaller.STATUS_SUCCESS) {
            // Eskiden diğer hatalar sessizce yutuluyordu: kullanıcı neden güncellenmediğini hiç görmüyordu (#461).
            String msg = i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
            Updater.notify(c, "ALICE güncellenemedi",
                    "Android kurulumu tamamlamadı (" + status + (msg != null && !msg.isEmpty() ? ": " + msg : "") + ").", null);
        }
        // STATUS_SUCCESS: Android uygulamayı yeniden başlatır, BootReceiver (MY_PACKAGE_REPLACED) servisi açar.
    }
}
