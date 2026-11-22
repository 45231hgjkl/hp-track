# hp-track

校园跑本地客户端：一份 Python 代码，三个壳（Windows exe / Linux pyz / Android APK），
本地回环 `127.0.0.1:8737` 提供 Web 控制台，支持密码登录、短信登录与跑量提交。
考核点位一律取服务端当次下发的三点，本地只负责把这三个点跑完；配速按分钟/公里、
步数按步频推算，提交前用服务端给的上下限做一次预校验。
每次创建/提交之前先把状态写进 `run_state.json`：上次记录没收尾时不再新建，避免在服务器上
留下两条。没交完的记录可以**续跑而不是重建**——`school.py resume`（页面上的「继续提交」）
用的还是原来那个 `record_id`，按真实流逝时间判断是否跑够，绝不提前提交；
只有 `create_unknown` / `finish_unknown` 这种「结果不确定」的状态才只能到小程序核实。
确认无需处理时用 `school.py abandon`（页面上的「已核实」）把本地状态收掉。
轨迹按三点闭环里最短的一种顺序铺开，位置按速度曲线推进（有快慢起伏，不匀速）；
点位不在校区边界内时默认只提示，`--strict-area` 才中止、`--no-area` 跳过。

[![CI](https://github.com/45231hgjkl/hp-track/actions/workflows/ci.yml/badge.svg)](https://github.com/45231hgjkl/hp-track/actions/workflows/ci.yml)

## 三端产物

由 GitHub Actions 构建，产物不进仓库，挂在 Release 上。

| 平台 | 产物 | 体积 | 用户需要 |
|---|---|---|---|
| Windows | `windows-x64/hp-track.exe` | ≈ 7.4 MB | 双击即用 |
| Linux / macOS | `linux-x64/hp-track.pyz` | ≈ 124 KB | python3 ≥ 3.8 |
| Android | `android-arm64/hp-track.apk` | ≈ 12 MB | 直接安装（arm64 真机） |

本地构建产物统一落在 `dist/release/v<ver>/<platform>/`（已 gitignore），
另附 `SHA256SUMS.txt` 与 `release.json` 清单。

## 目录

```
src/       运行时：school.py（API/鉴权/轨迹） school_web.py（本地服务）
           pure_aes.py（纯标准库 AES） index.html（控制台页面）
android/   Chaquopy 工程：Java 壳 + 前台服务保活 + entry.py
scripts/   构建与验收：build_windows.ps1 / build_pyz.py / build_apk.ps1|sh
           verify_artifacts.py / verify_apk.py / sim_entry.py / paths.py / manifest.py
tests/     离线回归：selftest_login.py（129 项，不联网）
docs/      适配计划 ADAPTATION-PLAN.md、变更记录 CHANGELOG.md
.github/   ci.yml（检查+三端构建）、release.yml（tag 发布）
```

## 构建

```powershell
.\scripts\build_windows.ps1 -PythonExe python     # → windows-x64/hp-track.exe
```

```bash
python scripts/build_pyz.py                        # → linux-x64/hp-track.pyz
./scripts/build_apk.sh                             # → android-arm64/hp-track.apk（需 SDK + JDK17+）
python scripts/manifest.py                         # → SHA256SUMS.txt + release.json
```

冒烟：`python scripts/verify_artifacts.py <产物>`（起服务 → 取页面 → 404 → shutdown）；
APK 结构：`python scripts/verify_apk.py <apk> --sdk <SDK>`。

## 本地运行

```bash
python src/school_web.py --port 8737       # 浏览器开 http://127.0.0.1:8737
python src/school.py login --phone <PHONE> # CLI：密码 / --sms 短信
```

运行时零第三方依赖；`HP_TRACK_HOME`、`HP_TRACK_INDEX` 两个环境变量用于安卓端改会话目录与页面路径，
桌面端不设即默认行为。

## 发布

改 `VERSION` → 更新 `docs/CHANGELOG.md` → 提交 → 打 tag：

```bash
git tag v1.0.0 && git push origin v1.0.0
```

`release.yml` 会构建三端、生成清单并创建 Release。

## 开发检查

```bash
ruff check src scripts tests     # 与 CI 同一套规则（pyproject.toml）
python tests/selftest_login.py   # 登录链路离线回归
```

## 数据来源

校区边界多边形取自 OpenStreetMap OSM 边界数据（© OpenStreetMap contributors，ODbL 1.0），
内联在 `src/school.py` 的 `CAMPUS_BOUNDARY`，仅用于本地「点位是否在校区内」的提示。

## 图标

三端图标都由根目录的 `logo.png`（880×880）生成：

```bash
python scripts/make_icons.py     # 需要 Pillow，仅重新生成图标时用
```

产物入库，构建时不再依赖 Pillow：`assets/icon.ico`（Windows 七档尺寸）、
`android/.../res/mipmap-*/ic_launcher.png` 与 `ic_launcher_foreground.png`、
`mipmap-anydpi-v26/ic_launcher.xml`（Android 8+ 自适应）、以及 `index.html` 内联的 favicon。

## 诊断

出事时先导出诊断报告，再看报告里的「上次记录」和「最近事件」两段：

```bash
python src/school.py diagnose            # 打印
python src/school.py diagnose --out d.txt
```

页脚也有「导出诊断」，对应 `GET /api/diagnose`（text/plain）。报告含运行形态、数据目录、
会话有无（手机号打码）、账号的配速/步频区间、上次记录的 `status`/`record_id`/已跑/剩余/已传点/
可续判定/失败原因、四项文件检查与最近 30 条事件；会话串、密码、手机号原文、坐标与请求正文一律不出现。

## 许可

[MIT](LICENSE)。仅用于自有账号下的客户端行为研究。
