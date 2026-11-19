#!/system/bin/sh
# post-fs-data: 把 Reqable / mitmproxy 的 CA 注入系统与 APEX 信任库
# 目标目录是只读 erofs / ext4，用 bind mount 叠加，不修改分区本身
MODDIR=${0%/*}
LOG=$MODDIR/inject.log
: > "$LOG"
echo "=== inject start $(date) ===" >> "$LOG"

inject() {
  SRC="$1"; DST="$2"; TMP="$MODDIR/tmp_$3"
  if [ ! -d "$SRC" ]; then
    echo "skip: $SRC 不存在" >> "$LOG"
    return 0
  fi
  rm -rf "$TMP"
  mkdir -p "$TMP"
  cp -a "$SRC"/. "$TMP"/ 2>>"$LOG"
  cp "$MODDIR"/system/etc/security/cacerts/*.0 "$TMP"/ 2>>"$LOG"
  chmod 644 "$TMP"/*.0 2>>"$LOG"
  chcon u:object_r:system_file:s0 "$TMP"/*.0 2>>"$LOG"
  mount --bind "$TMP" "$DST" 2>>"$LOG"
  echo "mounted $DST  count=$(ls "$DST" | wc -l)" >> "$LOG"
}

inject /system/etc/security/cacerts              /system/etc/security/cacerts              sys
inject /apex/com.android.conscrypt/cacerts       /apex/com.android.conscrypt/cacerts       apex

echo "=== inject done ===" >> "$LOG"
