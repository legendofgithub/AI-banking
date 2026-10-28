package com.aibanking.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.ProgressDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.widget.Toast;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import org.json.JSONObject;

/**
 * App 内自更新(2026-10-28):启动后向电脑端 /app/version.json 要版本清单,
 * versionCode 比本机大则弹窗提示;「立即更新」下载 bankagent.apk 到 App 私有
 * updates 目录,sha256 校验通过后经 UpdateProvider 唤起系统安装器。
 *
 * Android 8+ 首次装更新需用户在系统设置授予"安装未知应用":
 * 检测 canRequestPackageInstalls 不满足时带用户跳设置,回来 onResume 继续。
 * 全程零第三方依赖(org.json 是 Android 运行时内置)。
 */
public class AppUpdater {

    private static final String UPDATE_FILE = "bankagent-update.apk";

    private final Activity activity;
    private final String server;
    private boolean checking = false;
    private File pendingInstall = null;

    AppUpdater(Activity activity, String server) {
        this.activity = activity;
        this.server = server;
    }

    static File updateDir(Context ctx) {
        File dir = ctx.getExternalFilesDir("updates");
        if (dir == null) {
            dir = new File(ctx.getCacheDir(), "updates");
        }
        if (!dir.exists()) {
            dir.mkdirs();
        }
        return dir;
    }

    /** 启动/配置完服务器后调用:后台比对版本,有新版才打扰用户 */
    void check() {
        if (checking) {
            return;
        }
        checking = true;
        new Thread(() -> {
            JSONObject v = null;
            try {
                v = fetchJson(server + "/app/version.json");
            } catch (Exception ignored) {
                // 电脑没开/清单缺失:静默,不打扰
            } finally {
                checking = false;
            }
            if (v == null) {
                return;
            }
            try {
                int remote = v.getInt("versionCode");
                PackageInfo pi = activity.getPackageManager()
                        .getPackageInfo(activity.getPackageName(), 0);
                int local = (Build.VERSION.SDK_INT >= 28)
                        ? (int) pi.getLongVersionCode() : pi.versionCode;
                if (remote > local) {
                    final JSONObject info = v;
                    activity.runOnUiThread(() -> prompt(info));
                }
            } catch (Exception ignored) {
            }
        }, "update-check").start();
    }

    private void prompt(JSONObject v) {
        String name = v.optString("versionName", "");
        String note = v.optString("note", "");
        String msg = "新版本 " + name + " 已就绪" + (note.isEmpty() ? "" : "\n" + note);
        new AlertDialog.Builder(activity)
                .setTitle(R.string.update_title)
                .setMessage(msg)
                .setPositiveButton(R.string.update_now,
                        (DialogInterface d, int w) -> download(v))
                .setNegativeButton(R.string.update_later, null)
                .show();
    }

    private void download(JSONObject v) {
        String apkPath = v.optString("apk", "app/bankagent.apk");
        String sha256 = v.optString("sha256", "");
        ProgressDialog progress = new ProgressDialog(activity);
        progress.setMessage(activity.getString(R.string.update_downloading));
        progress.setProgressStyle(ProgressDialog.STYLE_HORIZONTAL);
        progress.setMax(100);
        progress.setCancelable(true);
        progress.show();

        new Thread(() -> {
            File out = new File(updateDir(activity), UPDATE_FILE);
            try {
                HttpURLConnection c = (HttpURLConnection)
                        new URL(server + "/" + apkPath).openConnection();
                c.setConnectTimeout(8000);
                c.setReadTimeout(20000);
                long total = c.getContentLength();
                try (InputStream in = c.getInputStream();
                     OutputStream os = new FileOutputStream(out)) {
                    byte[] buf = new byte[16384];
                    long done = 0;
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        os.write(buf, 0, n);
                        done += n;
                        if (total > 0) {
                            final int pct = (int) (done * 100 / total);
                            activity.runOnUiThread(() -> progress.setProgress(pct));
                        }
                    }
                }
                if (!sha256.isEmpty() && !sha256.equalsIgnoreCase(sha256Of(out))) {
                    out.delete();
                    toast(R.string.update_verify_fail);
                    return;
                }
                activity.runOnUiThread(() -> {
                    progress.dismiss();
                    install(out);
                });
            } catch (Exception e) {
                out.delete();
                toast(R.string.update_download_fail);
            } finally {
                activity.runOnUiThread(progress::dismiss);
            }
        }, "update-download").start();
    }

    /** 唤起系统安装器;Android 8+ 未授权"安装未知应用"时先跳设置 */
    private void install(File apk) {
        if (Build.VERSION.SDK_INT >= 26
                && !activity.getPackageManager().canRequestPackageInstalls()) {
            pendingInstall = apk;
            toast(R.string.update_need_permission);
            activity.startActivity(new Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + activity.getPackageName())));
            return;
        }
        pendingInstall = null;
        Uri uri = new Uri.Builder().scheme("content")
                .authority(UpdateProvider.AUTHORITY)
                .appendPath(apk.getName()).build();
        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setDataAndType(uri, "application/vnd.android.package-archive");
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        activity.startActivity(intent);
    }

    /** 从设置页回来:上次已下载但被权限拦住的更新继续安装 */
    void resume() {
        if (pendingInstall != null && pendingInstall.exists()
                && Build.VERSION.SDK_INT >= 26
                && activity.getPackageManager().canRequestPackageInstalls()) {
            install(pendingInstall);
        }
    }

    private void toast(int resId) {
        activity.runOnUiThread(
                () -> Toast.makeText(activity, resId, Toast.LENGTH_LONG).show());
    }

    private static JSONObject fetchJson(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(5000);
        c.setReadTimeout(8000);
        try (InputStream in = c.getInputStream()) {
            return new JSONObject(readAll(in));
        } finally {
            c.disconnect();
        }
    }

    private static String readAll(InputStream in) throws Exception {
        java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
        byte[] b = new byte[4096];
        int n;
        while ((n = in.read(b)) > 0) {
            buf.write(b, 0, n);
        }
        return buf.toString("UTF-8");
    }

    private static String sha256Of(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) {
                md.update(buf, 0, n);
            }
        }
        StringBuilder sb = new StringBuilder();
        for (byte b : md.digest()) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }
}
