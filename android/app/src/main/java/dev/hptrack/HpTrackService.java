package dev.hptrack;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

/**
 * 前台服务：把 Python 那个 http.server 挂在服务里，而不是 Activity 里。
 *
 * 为什么必须这么做：
 *   * Activity 切后台随时可能被回收，服务里跑的东西才算「后台保活」；
 *   * Android 8+ 后台进程几分钟就被掐，前台服务（常驻通知）不会被掐；
 *   * START_STICKY + stopWithTask=false：用户从最近任务划掉也能重启。
 *
 * 另配一把 PARTIAL_WAKE_LOCK，防止息屏后 CPU 休眠导致上传点位断档。
 */
public class HpTrackService extends Service {
    static final String TAG = "hp-track";
    static final String ACTION_START = "dev.hptrack.START";
    static final String ACTION_STOP = "dev.hptrack.STOP";
    static final int PORT = 8737;

    private static final String CHANNEL_ID = "hp-track-run";
    private static final int NOTIF_ID = 0x4850;   // "HP"

    private PowerManager.WakeLock wakeLock;
    private volatile boolean pythonStarted;
    private volatile boolean stopping;

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopping = true;
            stopSelf();
            return START_NOT_STICKY;
        }

        startForeground(NOTIF_ID, buildNotification());
        acquireWakeLock();
        ensurePython();

        // 被系统杀掉后重启：START_STICKY 会重新走这里
        return START_STICKY;
    }

    private void ensurePython() {
        if (pythonStarted || stopping) {
            return;
        }
        pythonStarted = true;

        final File home = new File(getFilesDir(), "hp-track");
        //noinspection ResultOfMethodCallIgnored
        home.mkdirs();
        final File index = new File(getFilesDir(), "index.html");
        copyAsset("index.html", index);

        new Thread(() -> {
            try {
                PyObject entry = Python.getInstance().getModule("entry");
                entry.callAttr("start", PORT, home.getAbsolutePath(), index.getAbsolutePath());
            } catch (Throwable t) {
                Log.e(TAG, "python 服务异常退出", t);
                pythonStarted = false;
            }
        }, "py-server").start();
    }

    private void acquireWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) {
            return;
        }
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm == null) {
            return;
        }
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "hptrack:server");
        wakeLock.setReferenceCounted(false);
        wakeLock.acquire(6 * 60 * 60 * 1000L);     // 最长 6 h，够跑完一次打卡
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW);
        ch.setDescription(getString(R.string.channel_desc));
        ch.setShowBadge(false);
        nm.createNotificationChannel(ch);
    }

    private Notification buildNotification() {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent piOpen = PendingIntent.getActivity(this, 0, open, pendingFlags());

        Intent stop = new Intent(this, HpTrackService.class).setAction(ACTION_STOP);
        PendingIntent piStop = PendingIntent.getService(this, 1, stop, pendingFlags());

        Notification.Builder b = (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        return b.setContentTitle(getString(R.string.notif_title))
                .setContentText(getString(R.string.notif_text, PORT))
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true)
                .setContentIntent(piOpen)
                .addAction(new Notification.Action.Builder(
                        null, getString(R.string.notif_stop), piStop).build())
                .build();
    }

    private static int pendingFlags() {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                ? PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
                : PendingIntent.FLAG_UPDATE_CURRENT;
    }

    private void copyAsset(String name, File dst) {
        if (dst.isFile() && dst.length() > 0) {
            return;                       // 已有则保留用户改动
        }
        try (InputStream in = getAssets().open(name);
             ByteArrayOutputStream buf = new ByteArrayOutputStream()) {
            byte[] b = new byte[8192];
            int n;
            while ((n = in.read(b)) > 0) {
                buf.write(b, 0, n);
            }
            try (FileOutputStream out = new FileOutputStream(dst)) {
                out.write(buf.toByteArray());
            }
        } catch (Exception e) {
            Log.e(TAG, "复制 assets/" + name + " 失败", e);
        }
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        // 用户划掉最近任务：服务仍在，重新拉一次前台通知确保不被降级
        if (!stopping) {
            startForeground(NOTIF_ID, buildNotification());
        }
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
