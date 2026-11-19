#!/usr/bin/env python3
"""manifest.py — 生成 release 目录的 SHA256SUMS.txt 与 release.json

    python scripts/manifest.py            # 校验/收集 dist/release/v<ver>/ 下三端产物
    python scripts/manifest.py --check    # 缺产物则退出码 1（CI 用）

release.json 记录：版本、tag、构建时间、各平台产物体积与 sha256、git commit。
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402


def sha256(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return os.environ.get("GITHUB_SHA", "")[:7]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="缺产物直接失败")
    a = ap.parse_args()

    root = paths.release_root()
    entries, missing = {}, []
    for platform, name in paths.PLATFORMS.items():
        p = root / platform / name
        if p.is_file():
            entries[platform] = {
                "file": f"{platform}/{name}",
                "size": p.stat().st_size,
                "sha256": sha256(p),
            }
        else:
            missing.append(f"{platform}/{name}")

    if missing:
        print("缺少产物:\n  " + "\n  ".join(missing), file=sys.stderr)
        if a.check:
            return 1

    lines = [f"{e['sha256']}  {e['file']}" for e in entries.values()]
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    manifest = {
        "name": "hp-track",
        "version": paths.version(),
        "tag": paths.tag(),
        "commit": git_commit(),
        "built_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "artifacts": entries,
        "missing": missing,
    }
    (root / "release.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for platform, e in entries.items():
        print(f"  ok  {platform:<14} {e['size'] / 1048576:>6.2f} MB  {e['sha256'][:16]}…")
    print(f"\n清单: {root / 'SHA256SUMS.txt'}")
    print(f"清单: {root / 'release.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
