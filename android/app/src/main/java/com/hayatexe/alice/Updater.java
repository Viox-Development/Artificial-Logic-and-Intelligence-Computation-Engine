package com.hayatexe.alice;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

/**
 * Kendini güncelleme. Bilgisayar GitHub'daki son APK'yı indirip /api/phone/app.apk'den verir (alice/app/phone_apk.py).
 * Uygulama ara ara sürümü sorar; yeniyse indirir, özetini (SHA-256) doğrular ve Android'in paket kurucusuyla kurar.
 * Android 12 ve sonrasında uygulama kendini sormadan güncelleyebilir; daha eskilerde ya da Android sorarsa
 * "Güncelle" onayı çıkar. "Bilinmeyen uygulamaları yükle" izni bir kez verilir.
 */
final class Updater {
    static final int ID_UPDATE = 3;
    private static final long EVERY_MS = 3 * 3600 * 1000L;
    // Bilgisayar uyuyordu ya da ağ yoktu: 3 saat değil, 15 dakika sonra yeniden dene (#461).
    private static final long RETRY_MS = 15 * 60 * 1000L;
    private static volatile boolean busy;
    private static volatile long lastCheck;

    private Updater() {}

    static long versionCode(Context c) {
        try {
            PackageInfo p = c.getPackageManager().getPackageInfo(c.getPackageName(), 0);
            return Build.VERSION.SDK_INT >= 28 ? p.getLongVersionCode() : p.versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    /** Arka planda kontrol et. force: zaman sınırına bakma (kullanıcı "Güncelle"ye bastı). */
    static void check(Context c, boolean force) {
        Context app = c.getApplicationContext();
        if (!Prefs.configured(app)) return;
        if (busy) {
            if (force) toast(app, "Güncellemeye zaten bakılıyor…");
            return;
        }
        long now = System.currentTimeMillis();
        if (!force && now - lastCheck < EVERY_MS) return;
        lastCheck = now;
        busy = true;
        new Thread(() -> {
            try {
                run(app, force);
            } catch (Exception e) {
                // bilgisayar kapalı ya da indirme yarıda kaldı: 15 dakika sonra yeniden
                lastCheck = System.currentTimeMillis() - EVERY_MS + RETRY_MS;
                if (force) toast(app, "Güncellenemedi: " + (e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName()));
            } finally {
                busy = false;
            }
        }, "alice-update").start();
    }

    private static void run(Context c, boolean interactive) throws Exception {
        // refresh=1: bilgisayar önce GitHub'daki android-apk dalını çeker, sonra cevap verir. Eskiden önce cevap verip
        // sonra çekiyordu: yeni sürümden sonraki ilk soru hep eski sürümü görüyordu ve telefon 3 saat bekliyordu (#461).
        JSONObject info = new JSONObject(getInfo(c));
        long remote = info.optLong("version_code", 0);
        String name = info.optString("version_name");
        if (remote <= versionCode(c)) {
            if (interactive) {
                String err = info.optString("error");
                if (remote <= 0) toast(c, "Bilgisayarda telefon uygulaması yok" + (err.isEmpty() ? "" : ": " + err));
                else toast(c, "Uygulama güncel (" + name + ")" + (err.isEmpty() ? "" : ". Bilgisayar yeni sürüme bakamadı: " + err));
            }
            return;
        }
        if (!c.getPackageManager().canRequestPackageInstalls()) {
            Intent s = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + c.getPackageName()));
            s.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            if (interactive && MainActivity.visible) c.startActivity(s);
            else notify(c, "ALICE güncellemesi var (" + name + ")",
                    "Kendini güncelleyebilmesi için bir kez izin ver: dokun, \"Bu kaynaktan izin ver\"i aç.", s);
            return;
        }
        if (interactive) toast(c, "Güncelleme indiriliyor (" + name + ")…");
        File apk = download(c, info.optString("sha256"));
        if (interactive) toast(c, "Güncelleme kuruluyor…");
        install(c, apk);
    }

    private static String getInfo(Context c) throws IOException {
        HttpURLConnection h = (HttpURLConnection) new URL(Prefs.base(c) + "/api/phone/app?refresh=1").openConnection();
        h.setConnectTimeout(6000);
        h.setReadTimeout(120000);  // bilgisayar yeni APK'yı GitHub'dan indiriyor olabilir
        h.setRequestProperty("X-Alice-Token", Prefs.token(c));
        try {
            if (h.getResponseCode() != 200) throw new IOException("HTTP " + h.getResponseCode());
            try (InputStream in = h.getInputStream()) {
                java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
                return b.toString("UTF-8");
            }
        } finally {
            h.disconnect();
        }
    }

    /** Kullanıcı "Güncelle"ye bastıysa sonucu söyle (arka plan iş parçacığından). */
    static void toast(Context c, String text) {
        new Handler(Looper.getMainLooper()).post(() -> Toast.makeText(c, text, Toast.LENGTH_LONG).show());
    }

    private static File download(Context c, String sha256) throws Exception {
        HttpURLConnection h = (HttpURLConnection) new URL(Prefs.base(c) + "/api/phone/app.apk").openConnection();
        h.setConnectTimeout(6000);
        h.setReadTimeout(60000);
        h.setRequestProperty("X-Alice-Token", Prefs.token(c));
        if (h.getResponseCode() != 200) throw new IOException("HTTP " + h.getResponseCode());
        File out = new File(c.getCacheDir(), "update.apk");
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (InputStream in = h.getInputStream(); OutputStream o = new FileOutputStream(out)) {
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) {
                md.update(buf, 0, n);
                o.write(buf, 0, n);
            }
        } finally {
            h.disconnect();
        }
        StringBuilder hex = new StringBuilder();
        for (byte b : md.digest()) hex.append(String.format("%02x", b));
        if (!sha256.isEmpty() && !sha256.equalsIgnoreCase(hex.toString())) {
            //noinspection ResultOfMethodCallIgnored
            out.delete();
            throw new IOException("APK özeti tutmuyor (indirme bozuk)");
        }
        return out;
    }

    private static void install(Context c, File apk) throws IOException {
        PackageInstaller pi = c.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams p = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        p.setAppPackageName(c.getPackageName());
        p.setSize(apk.length());
        if (Build.VERSION.SDK_INT >= 31) p.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        int id = pi.createSession(p);
        try (PackageInstaller.Session s = pi.openSession(id)) {
            try (InputStream in = new FileInputStream(apk); OutputStream o = s.openWrite("alice.apk", 0, apk.length())) {
                byte[] buf = new byte[65536];
                int n;
                while ((n = in.read(buf)) > 0) o.write(buf, 0, n);
                s.fsync(o);
            }
            Intent done = new Intent(c, UpdateReceiver.class);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
            s.commit(PendingIntent.getBroadcast(c, id, done, flags).getIntentSender());
        }
    }

    static void notify(Context c, String title, String body, Intent open) {
        EventService.channels(c);
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        Notification.Builder b = new Notification.Builder(c, EventService.CH_ALERT)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setAutoCancel(true);
        if (open != null) {
            b.setContentIntent(PendingIntent.getActivity(c, ID_UPDATE, open,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
        }
        nm.notify(ID_UPDATE, b.build());
    }
}
