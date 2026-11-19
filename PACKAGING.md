# hp-track 多平台打包方案

> 目标：把现有 WebUI（`ecust.py` + `ecust_web.py` + `index.html`）做成「别人下载后双击就能用」
> 的产物，覆盖 Windows / Linux / Android / iOS(IPA)。
>
> **状态：Windows 与 Linux/macOS 的单文件产物已实测可用，产物在 `dist/`。**
> Android 与 iOS 部分是方案与可行性判断。

---

## 一、结论先行

| 平台 | 方案 | 可行性 | 状态 |
|---|---|---|---|
| Windows | PyInstaller onefile | ✅ | **已完成，8.70 MB，实测通过** |
| Linux / macOS | zipapp `.pyz` | ✅ | **已完成，61.4 KB，实测通过** |
| Android | APK 内嵌二进制（gomobile） | ⚠️ 可行但重 | 方案待批 |
| iOS / IPA | BeeWare 或 Swift 重写 | ❌ 不建议 | 建议砍掉 |

---

## 二、已实测的结果（不是估算）

### 产物

| 文件 | 大小 | 冷启动 | 需要 Python |
|---|---|---|---|
| `dist/hp-track.exe` | **8.70 MB** | 1.6–1.9 s | 不需要 |
| `dist/hp-track.pyz` | **61.4 KB** | 1.1–1.2 s | 需要 3.8+ |

exe 双击 → 自动开浏览器 → 页面出现「关闭本地服务」按钮 → 点它进程退出。全链路已验证。

### 已通过的验证项

| 验证 | 结果 |
|---|---|
| 页面从包内读出（`--add-data` + `_MEIPASS`） | ✅ 21605 B |
| 冻结态真实 TLS 到 `run.ecust.edu.cn` | ✅ 返回服务端原文「账号或密码不正确！」 |
| 会话落点 `%APPDATA%\hp-track\session.json` | ✅ 探针文件被正确读到 |
| 外挂 `index.html` 覆盖内置版 | ✅ 改了 `<title>` 立即生效 |
| `POST /api/shutdown` 进程退出 | ✅ exe / pyz 均正常 |
| 重复双击复用已有实例 | ✅ 打印「已有实例在运行」并只开浏览器 |
| 假 sid → HTTP 401 → 判为会话过期 | ✅ `code=-2` / `_http_status=401` 双路径 |

### 一个关键前提：零第三方依赖

原代码唯一的 C 扩展依赖 `cryptography`（只用了 AES-128-CBC 一个原语）已被 `pure_aes.py` 完全替代：

```
FIPS-197 附录 B 官方测试向量          ok
200 组随机向量与 cryptography 双向一致  ok
enc/dec 往返（含中文/emoji）           ok
50 点轨迹 enc() 单次                  5.6 ms
32 位 / 64 位解释器                   均可
```

这一步是所有打包方案的**地基**：没有原生库，才不会有「Linux 上 GLIBC 版本不匹配」「Android 上 wheel 编不出来」「iOS 上交叉编译失败」这一整类问题。

---

## 三、三条硬约束

### 1. CORS 堵死了「纯前端单文件 HTML」

`run.ecust.edu.cn` 不返回 `Access-Control-Allow-Origin`。浏览器里任何 `fetch` 直连都被同源策略挡下。

**推论**：不存在「一个 HTML 双击打开就能用」的方案。**必须有一个本地进程做 HTTP 代理。**
这也决定了 Android/iOS 不能只套 WebView——后端必须和 WebView 在同一个包里。

### 2. AES payload 必须自己实现

`/xcxapi/checkRecord/`、`/updateRecordNew/`、`/uploadPathPointV3/` 的 body 是
`base64(iv) + ":" + base64(AES-128-CBC/PKCS7(json))`，硬编码 key `A7fK92LmXyQw8vRp`。
已用纯标准库解决。

### 3. 别人的电脑没有 Python

「一键打开」意味着不能要求先装 Python。exe 内置运行时（8.7 MB）解决；Linux 用 `.pyz`（61 KB）则依赖系统 python3，Linux 桌面基本都有。

---

## 四、逐平台

### 4.1 Windows —— 已完成

```powershell
pyinstaller --onefile --name hp-track `
  --add-data "index.html;." `
  --exclude-module cryptography --exclude-module tkinter `
  --exclude-module unittest --exclude-module pydoc `
  --exclude-module setuptools --exclude-module pkg_resources `
  --paths . --noconfirm --clean ecust_web.py
```

四个必须处理的地方（都已改好）：

| 问题 | 处理 |
|---|---|
| `index.html` 要随包 | `--add-data` + `read_asset()`，同时优先读 exe 同目录，方便用户改皮肤 |
| 会话写进临时解压目录会丢 | `data_dir()` 在冻结态改用 `%APPDATA%\hp-track\` |
| `--noconsole` 后没法关窗口 | 保留控制台（关窗即退）+ 页面「关闭本地服务」按钮双通道 |
| 双击重复启动会端口冲突 | `port_in_use()` 检测，已有实例就只开浏览器 |

**已知代价**：PyInstaller 的 bootloader 常被 Defender 启发式误报（`Wacatac` 一类），同学首次运行可能要教他点「仍要运行」。Go 方案没这个问题，这也是下文 P2 的理由之一。

防火墙：绑 `127.0.0.1` 不触发弹窗；**别绑 `0.0.0.0`**。

### 4.2 Linux / macOS —— 已完成（zipapp）

```bash
python -m zipapp .build/stage -m "ecust_web:main" -p "/usr/bin/env python3" -o dist/hp-track.pyz
chmod +x dist/hp-track.pyz && ./dist/hp-track.pyz
```

61.4 KB，Windows 上就能构建，不需要 Linux 构建机。

**为什么不给 Linux 出 PyInstaller 二进制**：PyInstaller 不能交叉编译，必须有一台 Linux 机（WSL / Docker / CI）再跑一次；而且产物和构建机的 glibc 版本绑定，Ubuntu 24.04 上打的包到 CentOS 7 会 `GLIBC_2.34 not found`。用 `manylinux2014` 容器能规避，但那是另一套基础设施。

**zipapp 没有 glibc 问题**——纯字节码 zip，任何 `python3` ≥ 3.8 都能跑，同一份文件在 Linux/macOS 通用。

代价是用户要有 Python 3。对 Linux 桌面用户基本不算门槛；对 macOS 也已自带。

### 4.3 Android —— 2–3 天

| 路径 | 原理 | 可行性 | 成本 |
|---|---|---|---|
| A. Termux | `pkg install python`，跑 `.pyz` | ✅ 现在就能用 | 0，但要装 Termux 敲命令，不算「一键」 |
| B. Kotlin 重写 | OkHttp 重写全部 API 逻辑 | ✅ | 重，第二份代码，长期维护最差 |
| C. gomobile bind | Go → `.aar`，Kotlin 壳调 `Start(port)` | ✅ 最正规 | 2–3 天 |
| D. exec 子进程 | 二进制放 `lib/arm64-v8a/libhptrack.so` 改名后 exec | ⚠️ | 2 天，绕过 Android 10+ W^X，hacky 但业界常见 |

推荐 C：

```
hp-track.apk
├── classes.dex              Kotlin 壳：1 个 Activity + WebView（~150 行）
└── lib/arm64-v8a/
    └── libhptrack.so        Go 编译 c-shared，导出 Start/Stop/Version
```

`index.html` 用 `go:embed` 打进二进制，不占 assets。权限只要 `INTERNET`；localhost 明文 Android 默认放行。

**真实阻力是构建环境，不是技术**：本机当前没有 Android SDK、没有 Gradle（只有 JDK 25）。首次搭约 10 GB 下载 + 半天配置，之后每次改代码要走 `gradle assembleRelease` → 签名 → 传手机，迭代速度比 Windows 慢一个数量级。

APK 体积估算 10–12 MB。

### 4.4 iOS / IPA —— 建议不做

| 障碍 | 说明 |
|---|---|
| 必须有 Mac | 本机 Windows，**物理上无法产出 IPA** |
| 沙箱禁 fork/exec | 不能塞二进制跑，必须在 app 进程内 |
| 必须签名 | 免费 Apple ID 签的包 **7 天过期**；年费 $99 |
| 不能上架 | 这类工具过不了 App Store 审核，只能 AltStore/Sideloadly/TrollStore |
| ATS | 本地 `http://127.0.0.1` 要开 `NSAllowsLocalNetworking` |
| 排障难 | 用户装的是你签的包，出问题只能靠截图沟通 |

三条技术路径：BeeWare（Python 进 app，约 40 MB，`pure_aes` 已扫清编译障碍）、Swift 重写（**第三份代码**）、越狱设备。

**核心判断**：花 4 天做出来，7 天掉签意味着「别人」根本用不了。作为「给别人用」的工具，这个平台的产品价值是负的。

**建议**：iOS 用户引导到「电脑上跑 exe」。要做就单独立项，先解决 Mac + 签名渠道。

---

## 五、可行性与维护性矩阵

### 可行性

| 平台 | 技术可行 | 本机能否构建 | 阻碍 |
|---|---|---|---|
| Windows | ✅ | ✅ | 已验证 |
| Linux / macOS | ✅ | ✅ | 已验证（zipapp 无需切换构建机） |
| Android | ✅ | ❌ 缺 SDK + Gradle | 环境 ~10 GB |
| iOS | ⚠️ | ❌ 缺 macOS + Xcode | 换机器 + 签名渠道 |

### 维护性：学校改接口时要动几处

| 方案组合 | 代码份数 | 改动点 | 重建命令 | 分发文件 |
|---|---|---|---|---|
| **Python：exe + pyz（当前）** | **1** | **1** | **2** | **2** |
| Python + Linux 也出 PyInstaller | 1 | 1 | 2（需 Linux 机） | 2 |
| Go 主干（Win/Linux/Mac） | 1 | 1 | 1 | 3 |
| Go + Kotlin 壳（+Android） | 2 | 1（Go） | 2 | 4 |
| 再加 Swift（+iOS） | 3 | **2** | 3（需 Mac） | 6 |

**关键洞察**：只要引入第二种语言，就多一份必须同步的「API 字段变更」劳动。而学校改字段是最可能发生的维护事件（14 个端点，任何一个加字段都要多处对着改）。

当前方案已经是「一份代码 + 两个产物」，维护性接近最优。

---

## 六、建议的下一步

### 已完成（P0 + P1）

- ✅ 去 `cryptography`，`pure_aes.py` 接手
- ✅ 会话落点 `%APPDATA%` / pyz 同目录
- ✅ `--browser` / `--no-browser`、`POST /api/shutdown`、端口复用检测
- ✅ `read_asset()` 统一三种运行形态的资源读取
- ✅ `_req` 捕获 HTTPError，401 也判会话过期
- ✅ `dist/hp-track.exe` 8.70 MB、`dist/hp-track.pyz` 61.4 KB

### 可选 P2：Go 主干重写（2 天）

一份 Go 代码覆盖全部逻辑，一条命令出全平台：

```bash
GOOS=windows GOARCH=amd64 go build -o hp-track.exe
GOOS=linux   GOARCH=amd64 go build -o hp-track
GOOS=darwin  GOARCH=arm64 go build -o hp-track-mac
GOOS=android GOARCH=arm64 go build -buildmode=c-shared -o libhptrack.so
```

约 800–900 行，`index.html` 一行不用改（HTTP 契约不变）。收益：免 glibc、免构建机切换、杀软误报显著降低、Android 直接接得上。

**只是 Windows 同学用 → 不值得。想长期维护 + 上手机 → 值得。**

### 可选 P3：Android APK（2–3 天，基于 P2）

### P4：iOS（先解决 Mac 和签名，否则不谈）

---

## 七、需要拍板的三件事

1. **现在就分发 exe，还是先花 2 天重写成 Go 再分发？**
   - 现在能用 → 直接分发 `dist/hp-track.exe`
   - 怕杀软误报 / 要覆盖手机 → 走 P2

2. **Android 走哪条**：gomobile（正规，需装 10 GB SDK）还是 Termux 说明文档（0 成本，但不算一键）？

3. **iOS 是否接受「不做」**？

---

## 附：文件清单

```
ecust.py            353 行   API client + 轨迹生成 + CLI
ecust_web.py        521 行   http.server 后端 + 登录 + 跑步任务
index.html          560 行   单页前端
pure_aes.py         187 行   纯标准库 AES-128-CBC
selftest_login.py    29 项   登录链路离线回归

dist/hp-track.exe   8.70 MB  Windows 单文件（已验证）
dist/hp-track.pyz   61.4 KB  Linux/macOS 单文件（已验证）

build/              11.7 MB  PyInstaller 中间产物，可删
.build/venv/        29.6 MB  构建环境，可删后按需重建
```

重建命令：

```powershell
# Windows
.build\venv\Scripts\pyinstaller.exe --onefile --name hp-track --add-data "index.html;." `
  --exclude-module cryptography --exclude-module tkinter --exclude-module unittest `
  --exclude-module pydoc --exclude-module setuptools --exclude-module pkg_resources `
  --paths . --noconfirm --clean ecust_web.py

# 全平台
python -m zipapp .build\stage -m "ecust_web:main" -p "/usr/bin/env python3" -o dist\hp-track.pyz
```
