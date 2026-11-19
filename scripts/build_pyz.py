#!/usr/bin/env python3
"""build_pyz.py — 出 Linux / macOS 通用单文件 → dist/release/v<ver>/linux-x64/hp-track.pyz

zipapp 纯字节码，无 glibc 绑定，Windows 上即可构建，Linux/macOS 通用。
要求目标机有 python3 >= 3.8。
"""
import argparse
import os
import stat
import subprocess
import sys
import zipapp

try:                      # Windows 控制台/CI 默认非 UTF-8，中文输出会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import paths  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--interpreter", default="/usr/bin/env python3")
    a = ap.parse_args()

    stage = os.path.join(ROOT, ".build", "stage")
    rc = subprocess.call([sys.executable, os.path.join(HERE, "stage.py"), "--out", stage])
    if rc != 0:
        return rc

    out = a.out or str(paths.artifact("linux-x64"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)

    zipapp.create_archive(stage, target=out, main="ecust_web:main", interpreter=a.interpreter)
    os.chmod(out, os.stat(out).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"产物: {out}  ({os.path.getsize(out)} B)  v{paths.version()}")
    print(f"试跑: python3 {os.path.basename(out)} --no-browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
