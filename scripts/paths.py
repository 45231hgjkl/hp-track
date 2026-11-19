#!/usr/bin/env python3
"""paths.py — 版本号与 release 产出路径的唯一来源

目录规范（三端统一）：

    dist/
      release/
        v1.0.0/
          windows-x64/  hp-track.exe
          linux-x64/    hp-track.pyz
          android-arm64/hp-track.apk
          SHA256SUMS.txt
          release.json

用法（脚本内）:
    from paths import version, release_dir
    out = release_dir("windows-x64") / "hp-track.exe"
命令行:
    python scripts/paths.py              # 打印三端产物应有路径
"""
import json
import sys
from pathlib import Path

try:                      # Windows 控制台默认 GBK，✓ 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"

PLATFORMS = {
    "windows-x64": "hp-track.exe",
    "linux-x64": "hp-track.pyz",
    "android-arm64": "hp-track.apk",
}


def version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def tag() -> str:
    return "v" + version()


def release_root() -> Path:
    return ROOT / "dist" / "release" / tag()


def release_dir(platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ValueError(f"未知平台 {platform}，可选: {', '.join(PLATFORMS)}")
    d = release_root() / platform
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifact(platform: str) -> Path:
    return release_dir(platform) / PLATFORMS[platform]


def main() -> int:
    print(f"VERSION {version()}  →  {release_root()}")
    for p, name in PLATFORMS.items():
        path = release_root() / p / name
        mark = "✓" if path.is_file() else " "
        size = f"{path.stat().st_size / 1048576:.2f} MB" if path.is_file() else "-"
        print(f"  [{mark}] {p:<14} {name:<14} {size}")
    sums = release_root() / "SHA256SUMS.txt"
    if sums.is_file():
        print(f"  [✓] SHA256SUMS.txt ({len(sums.read_text().splitlines())} 行)")
    manifest = release_root() / "release.json"
    if manifest.is_file():
        print("  [✓] release.json")
        print(json.dumps(json.loads(manifest.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
