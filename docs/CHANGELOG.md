# Changelog

版本号与根目录 `VERSION` 一致，tag 形如 `v1.0.0`。

## [1.1.0] — 2026-11-20

对齐小程序侧的数据口径。此前的记录用本地硬编码点位与秒制配速提交，服务端按
`min_pace` / `min_step_frequency` 校验时大概率不通过。

### 变更

- **点位改为服务端下发**：`run` 与 `POST /api/run/start` 先取
  `GET /xcxtapi/activity/randrunInfo?lat&lng`，只用返回的 3 个考核点建记录；
  本地不再构造坐标与点位名。响应形状兼容 `data` 为数组或对象两种情况。
- **`pace` 单位改为分钟/公里**（原为秒/公里，差 60 倍）。CLI 与 WebUI 共用同一口径。
- **步数按步频推算**：`step_count = 步频 × 时长(分)`，步频取 `min_step_frequency` /
  `max_step_frequency` 区间的中位值，取代原来的 `max(30*wait//60, wait)`。
- **提交前预校验**：`shortest_distance` / `min_pace` / `max_pace` /
  `min_step_frequency` / `max_step_frequency` 任一不合规即拒绝启动，并给出建议时长；
  字段缺失则跳过该项，不臆造约束。
- **轨迹按真实三点闭环**：`make_track()` 改为「起点 → 三点 → 起点」，5 米步长铺满里程、
  每秒取点，采样点自带时间偏移，调用方不再假设「每秒一点」。修掉原实现在环线末端的越界取点。
- **会话头净化**：`X-Session-ID` 过白名单与字符集校验后才拼进请求，防止被改过的
  `session.json` 注入 header；同时去掉恒为空的 `Authorization` 头。
- **WebUI**：预设文案按 5.5 min/km 重算（2.0/3.0/5.0 km 约 11/17/28 分钟），
  实际时长取 `min_pace`/`max_pace` 中值；结果行配速改按 min/km 显示；启动失败会提示建议时长。

### 新增

- `tests/selftest_login.py` 覆盖跑步链路：点位来自 `randrunInfo`、`createLine` 只收服务端三点、
  提交体 `pace`/`pass_point`/`step_count` 口径、参数越界在启动前拦截。离线项 29 → 48。

### 已知限制

- `randrunInfo` 需要在开放时段内返回 3 个点位，否则本次直接失败（不再回退到本地点位）。
- 首次提交的 `pace` 单位变更尚未在真机跑完整流程核对，以服务端 `checkRecord` 返回为准。

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
