package dev.hptrack;

import android.app.Application;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * 进程级 Python 运行时初始化。
 * Chaquopy 要求 Python.start() 在任何 Python 调用之前完成，放在 Application 最稳。
 */
public class HpTrackApp extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
    }
}
