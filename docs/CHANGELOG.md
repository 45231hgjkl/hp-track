# Changelog

版本号与根目录 `VERSION` 一致，tag 形如 `v1.0.0`。

## [1.0.0] — 2026-11-19

首个三端产物、验收脚本、CI/CD 全部打通的版本；仓库只保留源码与构建链。

### 新增

- **三端产物**（由 Actions 构建，不入库）：`windows-x64/hp-track.exe` 8.96 MB、
  `linux-x64/hp-track.pyz` 64 KB、`android-arm64/hp-track.apk` 11.94 MB。
- **Android 后台保活**：`HpTrackService` 前台服务（`specialUse`）+ 常驻通知（含「停止」）+
  `PARTIAL_WAKE_LOCK` + `START_STICKY` + `stopWithTask=false` + 引导忽略电池优化。
  Python 服务跑在 Service，Activity 只做显示。
- **跨端环境变量**：`HP_TRACK_HOME`（会话目录）、`HP_TRACK_INDEX`（页面路径），桌面端不设即原行为。
- **构建脚本**：`scripts/stage.py`、`build_pyz.py`、`build_windows.ps1`、`build_apk.ps1`、`build_apk.sh`。
- **验收脚本**：`verify_artifacts.py`（A1–A4）、`verify_apk.py`（K1–K7）、`sim_entry.py`（S1–S3）。
- **release 规范**：`VERSION` 单一版本源 + `paths.py`（路径）+ `manifest.py`（SHA256SUMS/release.json）。
- **CI/CD**：`ci.yml`（lint + 离线回归 + 三端构建）、`release.yml`（tag → 三端构建 → Release）。

### 变更

- 目录规范化：运行时收敛到 `src/`，离线回归进 `tests/`，构建与验收入 `scripts/`。
- 版本号由 `VERSION` 注入 Gradle（`versionName` / `versionCode`）。
- APK 只打 `arm64-v8a` 并压缩 `.so`：39.8 MB → 11.94 MB。
- `dist/`（含 `dist/release/`）整体 gitignore，发布产物只走 Actions/Release。

### 移除

- 分析过程与临时材料：抓包/差分/解包/Frida 探测脚本、小程序解包中间产物、界面截图与 dump、
  证书校验小工具、Magisk 证书注入模块、示例轨迹、历史打包讨论稿。
- 重复文件 `post-fs-data.lf.sh`（与 `post-fs-data.sh` 字节一致）、手写 `hp-track.spec`
  （PyInstaller 重建，改由脚本生成并忽略）、`certcheck/CertCheck.class`（编译产物）。

### 已知限制

- APK 尚未在真机跑完整登录/跑量流程（无设备），M7 待办。
- 发布签名仍是 Android debug key，长期分发前需换正式 keystore。
