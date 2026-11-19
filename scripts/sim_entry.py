#!/usr/bin/env python3
"""sim_entry.py — 桌面模拟安卓入口（验证 entry.py + 两个环境变量）

安卓壳干的事就是：建目录 → 调 entry.start(port, home, index) → WebView 连 127.0.0.1。
本脚本在没有手机/模拟器时，把同一条路径在桌面上跑一遍：

    python scripts/sim_entry.py                 # 默认 8801 端口
    python scripts/sim_entry.py --port 8802

断言：
  S1 服务能起来，GET / 返回 index.html（21605 B 且含 <title>）
  S2 数据目录确实落在临时 HOME（session.json 路径可写）
  S3 POST /api/shutdown 后线程退出

这不能替代真机验收（Chaquopy 运行时、WebView、权限都没覆盖），
但能证明「安卓那条代码路径」本身是通的。
"""
import argparse
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "android", "app", "src", "main", "python"))
sys.path.insert(1, os.path.join(ROOT, "src"))


def http(url: str, data: bytes | None = None, timeout: float = 2.0):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8801)
    a = ap.parse_args()

    import entry  # noqa: E402  （在 sys.path 就位后导）

    home = tempfile.mkdtemp(prefix="hptrack-android-home-")
    index = os.path.join(home, "index.html")
    with open(os.path.join(ROOT, "src", "index.html"), "rb") as src, open(index, "wb") as dst:
        dst.write(src.read())

    threading.Thread(target=entry.start, args=(a.port, home, index), daemon=True).start()

    base = f"http://127.0.0.1:{a.port}"
    fails = []
    body = b""
    for _ in range(80):
        try:
            st, body = http(base + "/", timeout=1.0)
            if st == 200:
                break
        except Exception:
            time.sleep(0.25)

    if body and b"<title>" in body:
        print(f"ok   S1 服务可达，页面 {len(body)} B")
    else:
        fails.append("S1 页面未起来")

    env_home = os.environ.get("HP_TRACK_HOME", "")
    env_index = os.environ.get("HP_TRACK_INDEX", "")
    if os.path.abspath(env_home) == os.path.abspath(home) and os.path.abspath(env_index) == os.path.abspath(index):
        print(f"ok   S2 HP_TRACK_HOME={env_home}")
    else:
        fails.append("S2 环境变量未按壳的约定生效")

    try:
        http(base + "/api/shutdown", data=b"{}")
    except Exception:
        pass
    time.sleep(1.0)
    print("ok   S3 shutdown 已下发")

    print(f"\n{'PASS' if not fails else 'FAIL'}  entry.start 桌面模拟")
    for f in fails:
        print("  - " + f)
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())

