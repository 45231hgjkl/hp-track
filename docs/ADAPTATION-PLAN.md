# 三端适配计划（Windows / Linux / Android）

> 执行版：产物、构建、验收、CI/CD。状态为实测结果。
> 版本基线 `VERSION=1.0.0`｜产物根目录 `dist/release/v<ver>/<platform>/`（不入库，由 Actions 构建）

---

## 0. 结论

**一份 Python 代码，三个壳。**

| 端 | 壳 | 产物 | 用户要不要装运行时 | 状态 |
|---|---|---|---|---|
| Windows | PyInstaller onefile | `windows-x64/hp-track.exe` 8.96 MB | 不要 | ✅ 冒烟 A1–A4 |
| Linux / macOS | zipapp | `linux-x64/hp-track.pyz` 64 KB | 要 python3 ≥ 3.8 | ✅ 冒烟 A1–A4 |
| Android | Chaquopy（CPython 进 APK 进程）+ 前台服务 | `android-arm64/hp-track.apk` 11.94 MB | 不要 | ✅ 结构/保活验收 K1–K7；真机待 M7 |

业务代码只有 `src/school.py` + `src/school_web.py`，三端共用；壳只做三件事：
把 `127.0.0.1:8737` 跑起来、把页面显示出来、别被系统杀掉。

---

## 1. 验收标准

| # | 验收项 | 判定 |
|---|---|---|
| A1 | `GET /` 返回 `index.html`（含 `<title>`） | `scripts/verify_artifacts.py` |
| A2 | `GET /api/session` 返回合法 JSON | 同上 |
| A3 | 未知路由 404 | 同上 |
| A4 | `POST /api/shutdown` 后进程退出 | 同上（APK 走通知栏「停止」） |
| A5 | 真实登录可达学校服务器 | 手工：错密码应回「账号或密码不正确！」 |
| A6 | 会话落点不丢 | 手工：重启看 `session.json` |
| A7 | 重复启动不端口冲突 | 手工：二次启动只开浏览器 |
| K1–K7 | APK 模块/资源/ABI/签名/版本/保活声明 | `scripts/verify_apk.py` |
| S1–S3 | 安卓入口 `entry.start` 桌面可跑通 | `scripts/sim_entry.py` |

---

## 2. 跨端改造

三端唯一差异是可写目录与静态资源位置，用两个环境变量抹平（桌面端不设即原行为）：

| 变量 | 位置 | 作用 |
|---|---|---|
| `HP_TRACK_HOME` | `src/school.py: data_dir()` | 会话目录；安卓指 `filesDir/hp-track`（APK 内 `__file__` 只读） |
| `HP_TRACK_INDEX` | `src/school_web.py: read_asset()` | `index.html` 实体路径；安卓由壳从 assets 复制到私有目录 |

---

## 3. Windows

```powershell
.\scripts\build_windows.ps1 -PythonExe python
```

实测 **8.96 MB，A1–A4 全过**（`frozen:true`）。固定下来的细节：

| 项 | 处理 |
|---|---|
| 资源随包 | `--add-data "$root\src\index.html;."`（必须绝对路径，`--specpath` 会改变相对路径解析） |
| 入口 | `--paths src` + `src\school_web.py` |
| 中间产物 | `--workpath .build\pyi-work --specpath .build\pyi-spec`（根目录不再出现 `*.spec`） |
| 保留 `ssl` | 排除清单不能含 `ssl`，HTTPS 必需 |
| 排除 | `cryptography` `mitmproxy` `frida` `tkinter` `unittest` `pydoc` `setuptools` `pkg_resources` |

待办：版本信息（exe 文件属性）、代码签名（压 Defender 误报）、`--onedir` 便携版。
图标已完成：`assets/icon.ico` 由 `scripts/make_icons.py` 从 `logo.png` 生成，`build_windows.ps1` 传 `--icon`。

---

## 4. Linux / macOS

```bash
python3 scripts/build_pyz.py
```

实测 **64,175 B，A1–A4 全过**。选 zipapp 而非 PyInstaller：后者不能交叉编译且与构建机
glibc 绑定（Ubuntu 24.04 的包到 CentOS 7 直接 `GLIBC_2.34 not found`）；zipapp 是纯字节码 zip，
`python3 ≥ 3.8` 通用，Windows 上即可出包。

待办：`install.sh`（`~/.local/bin` + `.desktop` 双击入口）、AppImage 零依赖版（可选）。

---

## 5. Android

### 5.1 路线

| 路线 | 原理 | 体积 | 代码份数 | 结论 |
|---|---|---|---|---|
| **Chaquopy** | CPython 3.12 随 APK 分发，Python 在进程内跑 `http.server` | 11.94 MB | 1 | ✅ 主选 |
| Termux + pyz | 用户自带 Python 跑 pyz | 0 | 1 | 备选 |
| gomobile | Go `c-shared` + Java 壳 | ~12 MB | 2 | ❌ 需 NDK+Go |
| exec 子进程 | 二进制改名 `lib*.so` 后 exec | ~11 MB | 2 | ❌ 绕过 W^X |

Chaquopy 可行的前提：运行时零第三方依赖（`pure_aes.py` 取代 `cryptography`），APK 里只有 CPython 运行时。

### 5.2 工程结构

```
android/
├── settings.gradle.kts / build.gradle.kts     AGP 9.1.1 + Chaquopy 17.2.0
├── gradle.properties
└── app/
    ├── build.gradle.kts      版本取自 ../VERSION；sync 任务从 ../src 同步运行时
    └── src/main/
        ├── AndroidManifest.xml   INTERNET + FGS + WAKE_LOCK + 忽略电池优化；Service(specialUse)
        ├── java/dev/hptrack/     HpTrackApp / HpTrackService / MainActivity
        ├── python/entry.py       start(port, home, index) → 环境变量 → school_web.main()
        └── assets/index.html     由 Gradle 从 ../src 同步
```

### 5.3 后台保活

| 手段 | 实现 | 解决什么 |
|---|---|---|
| 前台服务 | `startForeground()` + 常驻通知（含「停止」） | 后台几分钟被掐 |
| 服务承载 Python | Python 跑在 Service，不在 Activity | 切后台/退出界面断服务 |
| `START_STICKY` | `onStartCommand` 返回值 | 被回收后自动重启 |
| `stopWithTask=false` | Manifest | 从最近任务划掉即杀进程 |
| WakeLock | `PARTIAL_WAKE_LOCK`，上限 6 h | 息屏 CPU 休眠导致点位断档 |
| 忽略电池优化 | `ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`（可拒绝） | 厂商省电冻结 |
| 返回键 | `moveTaskToBack(true)` | 误触返回键杀掉任务 |

### 5.4 构建

```powershell
.\scripts\build_apk.ps1 -PythonExe python    # Windows
./scripts/build_apk.sh                       # Linux / CI（读 ANDROID_HOME|ANDROID_SDK_ROOT、JAVA_HOME）
```

实测 `BUILD SUCCESSFUL`，**11.94 MB**，`apksigner verify` 通过（V2 / CN=Android Debug），K1–K7 全过。

### 5.5 踩坑记录

| 现象 | 原因 | 处理 |
|---|---|---|
| `Minimum supported Gradle version is 9.3.1` | AGP 9.1.1 要求 Gradle 9.3.1+；JDK 25 不能用 Gradle 8.x | 脚本固定下载 Gradle 9.3.1 |
| `Python 3.12 is not available for the ABI 'armeabi-v7a'` | Chaquopy 17.2 只提供 arm64-v8a / x86_64 | `abiFilters` 只留 arm64-v8a |
| `uses this output of task ':app:syncRuntime' without declaring dependency` | Gradle 9 隐式依赖校验 | `tasks.configureEach { name.contains("PythonSources") -> dependsOn(syncRuntime) }` |
| 首版 APK 39.8 MB | 双 ABI + `.so` 未压缩 | 单 ABI + `jniLibs.useLegacyPackaging = true` → 11.94 MB |

### 5.6 待办与限制

- 真机验收 A1–A7（当前 `adb devices` 为空、无 AVD/系统镜像）
- 正式签名（`keystore.properties` + `signingConfigs.release`，现用 debug key）
- 国产 ROM 深度清理的补充唤醒（JobScheduler/闹钟）
- Chaquopy 双轨授权：开源/个人免费，闭源商用需 license；不支持带 C 扩展的 pip 包（本项目不需要）
- Android 13+ 首次运行需允许通知，否则看不到常驻通知（服务仍在跑）

---

## 6. release 规范

```
dist/release/v<ver>/
├── windows-x64/    hp-track.exe
├── linux-x64/      hp-track.pyz
├── android-arm64/  hp-track.apk
├── SHA256SUMS.txt
└── release.json    # version/tag/commit/built_at/artifacts{size,sha256}
```

- 版本唯一来源 `VERSION`（Gradle `versionName/versionCode`、目录名、Release 标题都从它派生）。
- 路径唯一来源 `scripts/paths.py`；清单 `scripts/manifest.py --check`（缺产物 CI 直接失败）。
- `dist/` 整体 gitignore：本地与 CI 都可重建，仓库只存源码。

---

## 7. 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 仓库迁移 + 跨端环境变量改造 | ✅ |
| M1 | Windows exe 重建 + 冒烟 | ✅ 8.96 MB |
| M2 | Linux pyz 重建 + 冒烟 | ✅ 64 KB |
| M3 | Android 首次出包 + 结构验收 | ✅ 11.94 MB |
| M4 | Android 后台保活 | ✅ K6 覆盖 |
| M5 | release 路径 + SHA256/release.json | ✅ |
| M6 | CI/CD（ci.yml + release.yml） | ✅ 待首次云端跑 |
| M7 | 真机验收 A1–A7 + 正式签名 | 🟡 待真机 |
| M8 | 仓库精简（只留源码/构建/文档） | ✅ |

---

## 8. CI/CD

| 工作流 | 触发 | 内容 |
|---|---|---|
| `ci.yml` | push(main) / PR / 手动 | ruff → compileall → 离线自测 → 安卓入口模拟；并行出三端并冒烟，产物上传 artifact |
| `release.yml` | tag `v*` / 手动(dry_run) | 读 `VERSION` → 三端构建 → 还原到 `dist/release/v<ver>/<platform>/` → `manifest.py --check` → `gh release create` |

android job 环境：`setup-java 21` + `android-actions/setup-android` + `sdkmanager platforms;android-36`
+ `gradle/actions/setup-gradle@v4 (9.3.1)`。

---

## 9. 维护成本

| 组合 | 代码份数 | 改接口要动 | 重建命令 |
|---|---|---|---|
| **Py 三壳（当前）** | 1 | `src/school.py`（+ `src/index.html` 若字段显示变） | 3 条 |
| Go + Kotlin 壳 | 2 | 两处 | 3 条 |
| 再加 Swift | 3 | 三处 | 4 条 |

不做：iOS/IPA（无 Mac、7 天掉签、沙箱禁 fork）、纯前端单 HTML（接口无 CORS 头）、Go 重写（收益不抵成本）。
