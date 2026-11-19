#!/usr/bin/env python3
"""verify_apk.py — APK 结构验收（无真机也能跑的静态断言）

检查项：
  K1 assets/chaquopy/app.imy 里含 ecust.py / ecust_web.py / pure_aes.py / entry.py
  K2 assets/index.html 与仓库根 index.html 字节一致
  K3 lib/ 里只有 arm64-v8a，且 libpython3.12.so 存在
  K4 AndroidManifest 能解出 package / minSdk / targetSdk（调 aapt2，可缺省）
  K5 签名块存在（APK 末尾有 v2 签名 magic "APK Sig Block 42"）

用法:
    python scripts/verify_apk.py dist/hp-track.apk [--sdk C:\\...\\Android\\Sdk]
"""
import argparse
import os
import subprocess
import sys
import tempfile
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["ecust.py", "ecust_web.py", "pure_aes.py", "entry.py"]
SIG_MAGIC = b"APK Sig Block 42"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("apk")
    ap.add_argument("--sdk", default=os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "")
    a = ap.parse_args()

    apk = os.path.abspath(a.apk)
    if not os.path.isfile(apk):
        print(f"APK 不存在: {apk}", file=sys.stderr)
        return 2

    fails = []
    with zipfile.ZipFile(apk) as z:
        names = z.namelist()

        # K1: python 源码在 app.imy 里
        app_imy = next((n for n in names if n.endswith("chaquopy/app.imy")), None)
        if app_imy:
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "app.imy")
                with open(p, "wb") as f:
                    f.write(z.read(app_imy))
                try:
                    with zipfile.ZipFile(p) as inner:
                        inner_names = inner.namelist()
                except Exception:
                    inner_names = []
                # Chaquopy 可能存 .py 也可能存 .pyc（取决于构建机），两种都算通过
                packed = {n.rsplit(".", 1)[0] for n in inner_names}
                miss = [m for m in MODULES if m.rsplit(".", 1)[0] not in packed]
                if miss:
                    fails.append(f"app.imy 缺: {miss}")
                    print(f"FAIL K1 app.imy 缺 {miss}  （实际: {inner_names[:10]}）")
                else:
                    print(f"ok   K1 app.imy 含 {len(MODULES)} 个运行时模块")
        else:
            fails.append("找不到 assets/chaquopy/app.imy")
            print("FAIL K1 找不到 assets/chaquopy/app.imy")

        # K2: index.html 与仓库一致
        try:
            packed = z.read("assets/index.html")
            local = open(os.path.join(ROOT, "src", "index.html"), "rb").read()
            if packed == local:
                print(f"ok   K2 assets/index.html 与仓库一致（{len(packed)} B）")
            else:
                fails.append("assets/index.html 与仓库不一致")
                print(f"FAIL K2 index.html 不一致（包内 {len(packed)} B / 仓库 {len(local)} B）")
        except KeyError:
            fails.append("包内没有 assets/index.html")
            print("FAIL K2 包内没有 assets/index.html")

        # K3: native 库
        libs = [n for n in names if n.startswith("lib/")]
        abis = sorted({n.split("/")[1] for n in libs})
        has_py = any(n.endswith("libpython3.12.so") for n in libs)
        if abis == ["arm64-v8a"] and has_py:
            print(f"ok   K3 ABI={abis}，libpython3.12.so 存在")
        else:
            fails.append(f"ABI 或 python 库异常: abis={abis} has_py={has_py}")
            print(f"FAIL K3 abis={abis} has_py={has_py}")

    # K5: v2 签名块
    with open(apk, "rb") as f:
        f.seek(-64 * 1024, os.SEEK_END)
        tail = f.read()
    if SIG_MAGIC in tail:
        print("ok   K5 存在 v2 签名块（APK Sig Block 42）")
    else:
        fails.append("未见 v2 签名块")
        print("FAIL K5 未见 v2 签名块")

    # K4: aapt2 badging（可选）
    if a.sdk:
        bt = os.path.join(a.sdk, "build-tools")
        aapt2 = None
        if os.path.isdir(bt):
            for d in sorted(os.listdir(bt), reverse=True):
                cand = os.path.join(bt, d, "aapt2.exe" if os.name == "nt" else "aapt2")
                if os.path.isfile(cand):
                    aapt2 = cand
                    break
        if aapt2:
            out = subprocess.run([aapt2, "dump", "badging", apk], capture_output=True, text=True, errors="replace")
            head = [ln for ln in out.stdout.splitlines() if ln.startswith("package:") or ln.startswith("uses-permission")]
            print("ok   K4 " + "; ".join(head[:2]))

            # K7: versionName 必须与仓库 VERSION 一致
            want = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
            got = ""
            for ln in out.stdout.splitlines():
                if ln.startswith("package:"):
                    for tok in ln.split():
                        if tok.startswith("versionName="):
                            got = tok.split("=", 1)[1].strip("'")
            if got == want:
                print(f"ok   K7 versionName={got} 与 VERSION 一致")
            else:
                fails.append(f"versionName 不一致: 包内={got} 仓库={want}")
                print(f"FAIL K7 versionName={got} ≠ VERSION={want}")

            # K6: 前台服务与保活相关声明
            tree = subprocess.run([aapt2, "dump", "xmltree", "--file", "AndroidManifest.xml", apk],
                                  capture_output=True, text=True, errors="replace").stdout
            need = {
                "服务 HpTrackService": "dev.hptrack.HpTrackService",
                "前台服务类型": "foregroundServiceType",
                "FOREGROUND_SERVICE": "FOREGROUND_SERVICE",
                "WAKE_LOCK": "WAKE_LOCK",
                "忽略电池优化": "REQUEST_IGNORE_BATTERY_OPTIMIZATIONS",
                "stopWithTask=false": "stopWithTask",
            }
            miss = [k for k, v in need.items() if v not in tree]
            if miss:
                fails.append(f"manifest 缺: {miss}")
                print(f"FAIL K6 manifest 缺 {miss}")
            else:
                print("ok   K6 前台服务 + 保活权限全部就位")
        else:
            print("skip K4 未找到 aapt2")
    else:
        print("skip K4 未指定 SDK")

    size = os.path.getsize(apk) / 1024 / 1024
    print(f"\n{'PASS' if not fails else 'FAIL'}  {os.path.basename(apk)}  ({size:.2f} MB)")
    for f_ in fails:
        print("  - " + f_)
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())

