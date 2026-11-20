# hp-track

华东理工「健康跑」本地客户端：一份 Python 代码，三个壳（Windows exe / Linux pyz / Android APK），
本地回环 `127.0.0.1:8737` 提供 Web 控制台，支持密码登录、短信登录与跑量提交。
考核点位一律取服务端当次下发的三点，本地只负责把这三个点跑完；配速按分钟/公里、
步数按步频推算，提交前用服务端给的上下限做一次预校验。
每次创建/提交之前先把状态写进 `run_state.json`：上次记录没收尾时不再新建，避免在服务器上
留下两条；确认后可以用 `ecust.py abandon`（或页面上的「已核实」）把本地状态收掉。
轨迹按三点闭环里最短的一种顺序铺开，位置按速度曲线推进（有快慢起伏，不匀速）；
点位不在奉贤校区边界内时默认只提示，`--strict-area` 才中止、`--no-area` 跳过。

## 数据来源

奉贤校区边界多边形取自 OpenStreetMap way 304214471（© OpenStreetMap contributors，ODbL 1.0），
内联在 `src/ecust.py` 的 `CAMPUS_BOUNDARY`，仅用于本地「点位是否在校区内」的提示。

[![CI](https://github.com/45231hgjkl/hp-track/actions/workflows/ci.yml/badge.svg)](https://github.com/45231hgjkl/hp-track/actions/workflows/ci.yml)

## 三端产物

由 GitHub Actions 构建，产物不进仓库，挂在 Release 上。

| 平台 | 产物 | 体积 | 用户需要 |
|---|---|---|---|
| Windows | `windows-x64/hp-track.exe` | ≈ 9 MB | 双击即用 |
| Linux / macOS | `linux-x64/hp-track.pyz` | ≈ 64 KB | python3 ≥ 3.8 |
| Android | `android-arm64/hp-track.apk` | ≈ 12 MB | 直接安装（arm64 真机） |

本地构建产物统一落在 `dist/release/v<ver>/<platform>/`（已 gitignore），
另附 `SHA256SUMS.txt` 与 `release.json` 清单。

## 目录

```
src/       运行时：ecust.py（API/鉴权/轨迹） ecust_web.py（本地服务）
           pure_aes.py（纯标准库 AES） index.html（控制台页面）
android/   Chaquopy 工程：Java 壳 + 前台服务保活 + entry.py
scripts/   构建与验收：build_windows.ps1 / build_pyz.py / build_apk.ps1|sh
           verify_artifacts.py / verify_apk.py / sim_entry.py / paths.py / manifest.py
tests/     离线回归：selftest_login.py（73 项，不联网）
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
python src/ecust_web.py --port 8737       # 浏览器开 http://127.0.0.1:8737
python src/ecust.py login --phone <PHONE> # CLI：密码 / --sms 短信
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

## 许可

[MIT](LICENSE)。仅用于自有账号下的客户端行为研究。
