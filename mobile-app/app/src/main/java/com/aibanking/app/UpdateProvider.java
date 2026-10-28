package com.aibanking.app;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import java.io.File;
import java.io.FileNotFoundException;

/**
 * 把 App 私有目录里下载好的更新包以 content:// 暴露给系统安装器。
 * Android 7+ 的安装 intent 只收 content://(file:// 会 FileUriExposedException),
 * 壳是零依赖工程(无 androidx),故用原生 ContentProvider 自绘这层分享。
 * authorities 固定为 com.aibanking.app.update,路径段即文件名。
 */
public class UpdateProvider extends ContentProvider {

    static final String AUTHORITY = "com.aibanking.app.update";

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode)
            throws FileNotFoundException {
        File dir = AppUpdater.updateDir(getContext());
        File f = new File(dir, uri.getLastPathSegment());
        return ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection,
                      String[] selectionArgs) {
        return 0;
    }
}
