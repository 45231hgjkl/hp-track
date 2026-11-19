# hp-track

华东理工「健康跑」小程序逆向与自动化的本地研究工程（仅本地回环、自有账号）。

- 来源会话：`session-7dc4c6ef-e9af-4cda-8668-3daad799e445`
- 原始工作目录：`C:\Users\forestree\Desktop\hp-track`（本仓库为迁移后的 Git 版本）
- 迁移日期：2026-11-19

## 目录结构

| 路径 | 说明 |
|---|---|
| `ecust.py` | CLI 核心：密码登录 / 短信登录 / 会话复用（`X-Session-ID`）、建记录、上报点位、提交 |
| `ecust_web.py` | 本地 WebUI 服务（默认 `127.0.0.1:8737`），只监听回环，接口前缀 `/api/` |
| `index.html` | WebUI 前端单页（登录 + 跑量控制台） |
| `ecust_run.py` | API 直连提交脚本（服务端只校验 mileage / running_time / pass_point） |
| `pure_aes.py` | 请求体加解密实现（纯 Python，无外部依赖） |
| `gen_run_track.py` | 轨迹/点位生成（配合提交接口） |
| `analyze_capture.py` | 抓包差分分析：时间线、接口清单、字段差分、上报节奏 |
| `ecust_dump.py` | mitmproxy addon：自动解密小程序加密请求体 |
| `selftest_login.py` | 登录链路离线自测（Fake 客户端，不连真实服务器） |
| `wxunpack.py` | 微信小程序包解密/解包（V1MMWX 容器） |
| `mobile_capture.py` | 手机端抓包流程辅助脚本 |
| `inject_wechat.py` / `hold_inject.py` / `*_probe.js` / `*_bypass.js` / `wechat_ssl.js` | Frida 注入与 SSL/网络栈探测脚本 |
| `mod/ecust_cert/` | Magisk/KernelSU 模块：把 Reqable + mitmproxy CA 注入系统信任库 |
| `certcheck/` | 证书校验验证用 Java/DEX 小工具 |
| `PACKAGING.md` | 多平台打包方案（Windows PyInstaller / Linux-macOS zipapp / Android / iOS 评估） |
| `demo_2400m.gpx` | 示例轨迹文件 |
| `ui.xml`、`*.png` | 小程序界面 dump 与截图（分析留证） |

## 运行

```bash
# CLI
python ecust.py login --phone <PHONE>            # 交互输入密码
python ecust.py login --phone <PHONE> --sms      # 短信验证码
python ecust.py run   --sid <X-SESSION-ID> ...

# WebUI
python ecust_web.py --port 8737
# 浏览器打开 http://127.0.0.1:8737

# 离线自测（不联网）
python selftest_login.py

# 抓包解密
mitmdump -s ecust_dump.py -p 8080 --set upstream_cert=false
```

依赖：`cryptography`、`mitmproxy`、`frida`（仅注入脚本需要）。

## 不入库的内容（见 `.gitignore`）

迁移时按以下原则做了裁剪，原目录文件仍在原处：

| 排除项 | 原因 |
|---|---|
| `session.json`、`mc.db`、`capture*.jsonl`、`capture_raw.flow`、`*.log` | 含真实手机号、`X-Session-ID`、抓包明文等凭据，禁止入库 |
| `.build/`、`build/`、`dist/`、`__pycache__/` | 虚拟环境与 PyInstaller 构建产物，可重新生成 |
| `frida-server`、`frida-server.xz` | 83 MB / 24 MB 设备二进制，自行下载对应架构版本 |
| `unpacked/` | 由小程序包解出的第三方代码与静态资源，属中间产物 |
| `_t.0`、`u.0` | 运行时临时文件 |

## 说明

仅用于自有账号下的协议与客户端行为研究；接口调用全部发生在本地回环或本人设备。
