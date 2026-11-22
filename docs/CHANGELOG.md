# Changelog

版本号与根目录 `VERSION` 一致，tag 形如 `v1.0.0`。

## [1.3.2] — 2026-11-22

三件排障与稳健性改动，照 ECU 的 `RunDiagnostics` / `NetworkErrors` / `exclusive` 三处做法。

### 新增：诊断报告

- `diagnose()` 生成一份纯文本报告：运行形态（源码 / exe / pyz）、Python 与平台、数据目录、
  监听端口、`HP_TRACK_*` 是否设置、会话有无（手机号打码）、服务端是否报过期、
  账号的配速与步频区间、上次记录的 `status` / `record_id` / `slot` / 已跑 / 剩余 / 已传点 /
  可续判定 / 失败原因 / `submission_end_ms` 是否已钉住、四项文件检查、最近 30 条事件。
- 出口：`school.py diagnose [--out FILE]`、`GET /api/diagnose`（text/plain）、
  页面页脚的「导出诊断」链接。
- 报告只放排查所需字段：会话串、密码、手机号原文、坐标、请求正文一律不出现；
  日志与失败原因先过 `redact()`。`/api/diagnose` 额外做 Host/Origin 校验，防 DNS rebinding 读取。

### 新增：网络错误分类

- `classify_error(exc)` 把异常归成固定码并给可操作提示：`cert_invalid` / `tls_failed` /
  `network_timeout` / `dns_failed` / `connection_refused` / `http_<code>` /
  `network_unreachable` / `unknown`，`URLError` 会递归到 `reason` 再判。
- `_req()` 的 `URLError` 分支改为返回分类提示（不再回显 `str(reason)` 原文），
  响应里多带 `error_code`。

### 修复：任务启动竞态

- 新增 `RUN_TOKEN` 单任务令牌：`/api/run/start` 与 `/api/run/resume` 在**所有校验通过后**
  才 `claim`，线程 `finally` 释放。原来「检查 `running` → 起线程」之间有窗口，
  连点两次理论上能起两个任务。

### 测试

- 离线自测 105 → 129 项：报告含关键字段且不含手机号/会话串/坐标、`/api/diagnose` 返回
  text/plain、八类错误码与递归判定、令牌二次获取失败与释放后可再获取、并发续跑只放行一次。

### 已知限制

- 诊断报告不含服务端响应体（只含状态码与分类），无法据此判断服务端业务逻辑。
- 报告不落盘、不自动上传；需要时手动导出再粘贴。

## [1.3.1] — 2026-11-22

两件事：补上「中断后继续同一条记录」的能力；三端统一使用项目 logo 作为图标。

1.3.0 只能拦住未收尾的记录，不能把它交完；用户重跑就会在服务器上多留一条 0.01 km 的残记录。
这一版照 ECU 的状态机补齐续跑，记录被中断后不再需要重建。

### 图标

- 新增 `scripts/make_icons.py`：从仓库根目录 `logo.png`（880×880）一次生成三端图标，
  产物入库，构建时不再依赖 Pillow。
- Windows：`assets/icon.ico`（16/24/32/48/64/128/256 七档，22% 圆角），
  `build_windows.ps1` 传 `--icon`，exe 资源已带上图标。
- Android：`mipmap-{mdpi,hdpi,xhdpi,xxhdpi,xxxhdpi}/ic_launcher.png`（48–192dp 传统图标）
  + `ic_launcher_foreground.png`（108–432dp 自适应前景）+ `mipmap-anydpi-v26/ic_launcher.xml`
  + `values/colors.xml` 的背景色 `#4C9CF0`；manifest 补 `android:icon`。
- WebUI：`index.html` 内联 48×48 base64 favicon（约 8 KB），源码 / exe / pyz / APK 都不需要额外资源文件。
- `verify_apk.py` 增加 K8：用 `aapt2 dump badging` 确认 manifest 真的引用了应用图标
  （AGP 在 release 会重命名资源，按文件名查不到）。

### 变更

- **`run_state.json` 补全字段**：`start_ms`、`duration_ms`、`distance`、`student_id`、
  `geo`（起点与三点坐标）、`track_seed`、`submission_end_ms`。原来只存点位名，续跑时无法重建轨迹。
- **时间改由 `start_ms` 推导**：新增 `run_moving_ms()` / `run_remaining_ms()` /
  `run_end_ms()`，用真实流逝时间判断「跑够没有」，不再依赖内存里的等待进度。
- **`submission_end_ms` 先落盘再发请求**：首次提交与续跑共用同一个 `submit_payload()`，
  重试时 start/end 不再漂移，payload 可重放。
- **提交链路收敛为 `submit_record()`**：CLI 与 WebUI 共用一份状态机，HTTP 与日志由调用方注入；
  `checking` 阶段失败退回 `created`（可续），`finish_pending` / `record_updated` 阶段失败落
  `finish_unknown`（不可续），与 ECU 的 `markInterrupted()` 同一套语义。
- **新增续跑入口**：CLI `school.py resume [--wait] [--accept-invalid]`，
  WebUI `POST /api/run/resume`，页面在「可续」时显示「继续提交」按钮（此时隐藏「已核实」，
  避免误丢可续记录）。
- **续跑前校验账号**：重新拉 `getUserInfo`，`id` 必须等于记录里的 `student_id`，否则拒绝；
  未到时长时 `--wait` / `wait:true` 会等到点再提交，绝不提前交。
- **`/api/run/start` 区分两种未收尾**：可续的返回 `can_resume: true` 并带剩余秒数，
  不可续的仍是「去小程序核实」。

### 新增

- 离线自测 73 → 105 项：真实时间差与到点判定、`end` 钉住后复用、payload 由 `start_ms` 重建、
  可续状态枚举（`create_unknown` / `finish_unknown` / `finished` / `checking` / `discarded` 均不可续）、
  续跑不调 `createLine`、未到点不提交、账号不一致不提交。

### 已知限制

- 续跑只处理 `created` / `check_failed`；`create_unknown` / `finish_unknown` 的创建或提交结果
  本身不确定，仍然只提示核实（与 ECU 一致）。
- 中断太久（例如超过计划时长一倍）会导致配速偏离服务端区间，此时续跑会被本地预校验拒绝，
  那条记录只能作废——这是服务端判定的边界，不是本地能绕的。
- 仍无多任务队列，`run_state.json` 只保留最近一条。

## [1.3.0] — 2026-11-20

轨迹生成从「匀速绕圈」改成「按最短闭环 + 速度曲线推进」，并加上校区边界校验。

### 变更

- **三点访问顺序择优**：`best_ring()` 枚举 3 个点位的 6 种访问顺序，取闭环周长最短的一条
  （避免「折返跑」把周长撑大）。只影响本地怎么跑，提交给服务端的 `pass_point` 数组原样不动。
- **速度曲线**：`make_track()` 改为按
  `v(s) = 1 + 0.08·sin(s/90+φ) + 0.04·cos(s/210+φ)`（φ 随机）累积时间轴，
  每秒位移不再恒定——同一段路有快慢起伏，比「每秒固定米数」更像一次真实跑步。
  `seed` 参数用于测试复现。
- **校区边界校验**：内联校区边界多边形（WGS84），比对前用 GCJ-02 正算统一坐标系。
  点位或起点落在边界外时默认只提示（可能换校区了），`--strict-area` 才中止，
  `--no-area` 直接跳过；WebUI 传 `strict_area` / `no_area`。
- 采样精度保持 0.5 米铺线、每秒一个上传点，`accuracy` 固定 5.0（原先在 4–9 间随机）。

### 新增

- 离线自测 63 → 73 项：顺序择优确实取到最短周长、6 种排列都算过、时间偏移连续、
  位移变异系数 > 1%（证明速度曲线生效）、轨迹总长与目标里程误差 < 2%、
  校区内外点位判定、GCJ-02 偏移方向。

### 已知限制

- 边界只覆盖校区，换校区需要改 `CAMPUS_BOUNDARY` 或用 `--no-area`。
- 没有走路网：轨迹是「起点 → 三点 → 起点」的直线闭环，不贴合实际道路。
  要贴合需要 OSM 道路图（763 节点 / 902 边）与 Dijkstra，代价是仓库多一份 27 KB 数据
  和三端各一套寻路实现，暂不做。
- 速度曲线只调制时间轴，不产生 GPS 漂移；位置噪声仍是 ±1.3 米的高斯项。

## [1.2.0] — 2026-11-20

记录状态落盘 + 未收尾拦截。此前任务只活在进程内存里，进程一挂就既不知道自己有没有
`createLine` 成功、也拿不到 `record_id`，重跑会在服务器上留下两条记录。

### 变更

- **新增 `run_state.json`**（与 `session.json` 同目录，原子写）：每次网络副作用之前先落盘，
  状态机为 `create_pending → created → checking → finish_pending → record_updated → finished`，
  异常落到 `create_unknown` / `finish_unknown` / `check_failed`。
- **未收尾即拦截**：`create_pending` 之外的非终态记录一律拒绝新建，CLI 与 WebUI 同一条规则；
  `create_unknown` / `finish_unknown` 明确不自动重试。
- **`checkRecord` 三分支**：`status==1` 继续；`status==0` 中止并落 `check_failed`（CLI `--accept-invalid`
  可覆盖，WebUI 传 `accept_invalid`）；结构无法识别时中止而不是猜。
- **人工收尾通道**：CLI `school.py abandon`、`run --force`，WebUI `POST /api/run/resolve`
  与页面上的「已核实，清掉」——只动本地状态，服务器记录原样保留。
- **日志脱敏**：`rlog()` 与失败原因统一过 `redact()`，手机号打码、长 token（sid 形态）整段替换；
  `stderr` 的堆栈也走同一层脱敏，避免用户分享日志时带出会话。
- **`/api/session` 与 `/api/run/status`** 增加 `run_state` 摘要，页面顶部显示未收尾提示。

### 新增

- 离线自测 48 → 63 项：创建失败落 `create_unknown`、未收尾拦截、`resolve` 收尾后重跑、
  `status=0` 不发 `updateRecordNew` 也不传轨迹、日志与失败原因不含手机号/sid。

### 已知限制

- `run_state.json` 只保留最近一次记录，不做多任务队列。
- `create_pending` 状态下的重跑仍会新建记录；判断依据是「没有 record_id 就说明请求没发出去」，
  若网络在响应返回前中断，服务端可能已建记录而本地仍是 `create_pending`。

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
