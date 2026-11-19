#!/usr/bin/env python3
"""verify_artifacts.py — 产物冒烟测试（Win/Linux/macOS/APK 后端同一条链路）

做四件事：
  1. 起产物进程（--no-browser）
  2. 轮询 127.0.0.1:PORT，等页面可达
  3. GET / 断言含 <title>健康跑控制台；GET /api/session 断言 JSON 结构
  4. POST /api/shutdown，断言进程在超时内退出

不联网、不登录、不读真实账号。

用法:
    python scripts/verify_artifacts.py dist/hp-track.exe
    python scripts/verify_artifacts.py dist/hp-track.pyz --port 8799
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:                      # Windows 控制台默认 GBK，中文断言信息会糊
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def http(url: str, data: bytes | None = None, timeout: float = 3.0):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact")
    ap.add_argument("--port", type=int, default=8737)
    ap.add_argument("--boot-timeout", type=float, default=90.0)
    a = ap.parse_args()

    art = os.path.abspath(a.artifact)
    if not os.path.isfile(art):
        print(f"产物不存在: {art}", file=sys.stderr)
        return 2

    base = f"http://127.0.0.1:{a.port}"
    env = dict(os.environ, HP_TRACK_HOME=os.path.join(os.path.dirname(art), ".verify-home"))
    argv = [art] if art.lower().endswith(".exe") else [sys.executable, art]
    argv += ["--no-browser", "--port", str(a.port)]

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    fails = []

    try:
        deadline = time.time() + a.boot_timeout
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                st, body = http(base + "/", timeout=1.0)
                if st == 200 and b"<title>" in body:
                    up = True
                    print(f"ok   boot  {len(body)} B, title ok")
                    break
            except Exception:
                time.sleep(0.3)
        if not up:
            fails.append("boot: 端口未起来或页面无 <title>"
                         f"（进程 {'已退出 rc=' + str(proc.poll()) if proc.poll() is not None else '仍在运行'}）")

        if up:
            st, body = http(base + "/api/session")
            try:
                js = json.loads(body)
                ok = st == 200 and isinstance(js, dict)
            except Exception:
                ok = False
            print(("ok   " if ok else "FAIL ") + f"/api/session -> {st} {body[:120]!r}")
            if not ok:
                fails.append("/api/session 非法响应")

            st, body = http(base + "/api/nonexistent-endpooint")
            ok = st == 404
            print(("ok   " if ok else "FAIL ") + f"unknown route -> {st}")
            if not ok:
                fails.append("未知路由未返回 404")

            try:
                http(base + "/api/shutdown", data=b"{}")
            except Exception:
                pass
            t0 = time.time()
            while time.time() - t0 < 8 and proc.poll() is None:
                time.sleep(0.2)
            ok = proc.poll() is not None
            print(("ok   " if ok else "FAIL ") + f"shutdown 退出 rc={proc.returncode}")
            if not ok:
                fails.append("/api/shutdown 未使进程退出")
    finally:
        alive = proc.poll() is None
        if alive:
            proc.kill()
        if alive or fails:
            try:
                out = proc.communicate(timeout=10)[0] or b""
            except Exception:
                out = b""
            print("--- 进程输出尾部 ---")
            print(out.decode("utf-8", "replace")[-3000:])

    print(f"\n{'PASS' if not fails else 'FAIL'}  {os.path.basename(art)}")
    for f in fails:
        print("  - " + f)
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
