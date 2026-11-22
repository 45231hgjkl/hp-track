#!/usr/bin/env python3
"""stage.py — 组装可打包的运行时目录 .build/stage

运行时只有 src/ 下的 4 个文件，研究/注入脚本一概不进产物：
    src/school.py  src/school_web.py  src/pure_aes.py  src/index.html

用法:
    python scripts/stage.py            # → .build/stage
    python scripts/stage.py --out X    # 自定义输出目录
"""
import argparse
import os
import shutil
import sys

try:                      # Windows 控制台/CI 默认非 UTF-8，中文输出会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
RUNTIME = ["school.py", "school_web.py", "pure_aes.py", "index.html"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, ".build", "stage"))
    a = ap.parse_args()

    out = os.path.abspath(a.out)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)

    missing = []
    for name in RUNTIME:
        src = os.path.join(SRC, name)
        if not os.path.isfile(src):
            missing.append(name)
            continue
        shutil.copy2(src, os.path.join(out, name))
        print(f"  + src/{name:<16} {os.path.getsize(src):>8} B")
    if missing:
        print(f"缺少文件: {', '.join(missing)}（应在 src/ 下）", file=sys.stderr)
        return 1

    total = sum(os.path.getsize(os.path.join(out, n)) for n in RUNTIME)
    print(f"stage 就绪: {out}  ({total} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
