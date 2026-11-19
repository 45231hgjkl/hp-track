#!/usr/bin/env bash
# build_apk.sh — Linux/macOS/CI 版 APK 构建 → dist/release/v<ver>/android-arm64/hp-track.apk
#
#   export ANDROID_HOME=/opt/android-sdk        # 或 ~/Android/Sdk
#   export JAVA_HOME=/usr/lib/jvm/temurin-21    # AGP 9.x 需要 JDK 17+
#   ./scripts/build_apk.sh
#
# 需要 gradle 9.3.1+（CI 用 gradle/actions/setup-gradle，本地可 export PATH 指过去）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$(tr -d '\r\n' < VERSION)"
ANDROID="$ROOT/android"

: "${ANDROID_HOME:=${ANDROID_SDK_ROOT:?请设 ANDROID_HOME 或 ANDROID_SDK_ROOT}}"
: "${JAVA_HOME:?请设 JAVA_HOME（AGP 9 需要 JDK 17+）}"
GRADLE="${GRADLE:-gradle}"

printf 'sdk.dir=%s\n' "${ANDROID_HOME//\\//}" > "$ANDROID/local.properties"

(cd "$ANDROID" && "$GRADLE" --no-daemon "${1:-assembleRelease}")

APK="$(find "$ANDROID/app/build/outputs/apk" -name '*.apk' -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)"
[ -n "$APK" ] || { echo "没找到 APK 产物" >&2; exit 1; }

OUT_DIR="$ROOT/dist/release/v$VERSION/android-arm64"
mkdir -p "$OUT_DIR"
cp "$APK" "$OUT_DIR/hp-track.apk"

SIZE=$(stat -c%s "$OUT_DIR/hp-track.apk")
echo "产物: $OUT_DIR/hp-track.apk  ($((SIZE / 1024 / 1024)) MB)  v$VERSION"

BT="$(ls -d "$ANDROID_HOME"/build-tools/* | sort -V | tail -1)"
[ -x "$BT/apksigner" ] && "$BT/apksigner" verify --print-certs "$OUT_DIR/hp-track.apk" | head -3 || true

python3 scripts/verify_apk.py "$OUT_DIR/hp-track.apk" --sdk "$ANDROID_HOME"
