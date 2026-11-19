package dev.hptrack;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Log;
import android.view.KeyEvent;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.net.InetSocketAddress;
import java.net.Socket;

/**
 * 安卓壳：只负责「拉起前台服务 + 显示页面」。
 *
 * 真正的 Python 服务在 HpTrackService 里（前台服务 + WakeLock），
 * Activity 退出/切后台都不影响打卡任务；再从桌面点开重新连上同一个服务。
 */
public class MainActivity extends Activity {
    private static final String TAG = "hp-track";

    private WebView web;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setWebViewClient(new WebViewClient());
        setContentView(web);
        web.loadData("<h3 style='font-family:sans-serif'>hp-track 启动中…</h3>", "text/html", "utf-8");

        startServer();
        requestIgnoreBatteryOptimizations();
        waitAndLoad();
    }

    private void startServer() {
        Intent svc = new Intent(this, HpTrackService.class).setAction(HpTrackService.ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(svc);
        } else {
            startService(svc);
        }
    }

    /** 让系统别把前台服务当成「可随时冻结」的耗电应用；用户可拒绝，拒绝也不影响使用。 */
    private void requestIgnoreBatteryOptimizations() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            return;
        }
        PowerManager pm = getSystemService(PowerManager.class);
        if (pm == null || pm.isIgnoringBatteryOptimizations(getPackageName())) {
            return;
        }
        try {
            startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            Log.w(TAG, "无法请求忽略电池优化", e);
        }
    }

    private void waitAndLoad() {
        new Thread(() -> {
            if (waitPort(20_000)) {
                runOnUiThread(() -> web.loadUrl("http://127.0.0.1:" + HpTrackService.PORT + "/"));
            } else {
                toast("本地服务未在 20s 内就绪，稍后下拉重试或重开应用");
            }
        }, "wait-port").start();
    }

    private boolean waitPort(long timeoutMs) {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            try (Socket sock = new Socket()) {
                sock.connect(new InetSocketAddress("127.0.0.1", HpTrackService.PORT), 300);
                return true;
            } catch (Exception ignored) {
                try {
                    Thread.sleep(250);
                } catch (InterruptedException e) {
                    return false;
                }
            }
        }
        return false;
    }

    private void toast(final String msg) {
        runOnUiThread(() -> Toast.makeText(this, msg, Toast.LENGTH_LONG).show());
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            // 退到后台而不是杀服务：任务还在跑
            moveTaskToBack(true);
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
