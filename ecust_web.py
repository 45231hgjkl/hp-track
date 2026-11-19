#!/usr/bin/env python3
"""ecust_web.py — 华东理工健康跑 WebUI

启动:  python ecust_web.py [--port 8737]
访问:  http://127.0.0.1:8737

接口全部在本地回环，不对外监听。认证与会话复用 ecust.py。

登录设计:
  密码  POST /api/login/password  {phone,password}
  SMS   POST /api/login/sms/send  {phone}      服务端 60s 冷却
        POST /api/login/sms/verify{phone,code}
  状态  GET  /api/me              用户公开字段（白名单，不含 password/iphone 原文）
        GET  /api/session         本地缓存会话摘要
        POST /api/logout          {purge} 默认 true，同时删 session.json
        POST /api/refresh         重新拉 userinfo

会话失效（服务端 code=-2）在 secure() 里统一拦截: 清内存态 + 返回 401 relogin。
"""
import argparse
import datetime
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ecust as core

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8737
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
SMS_COOLDOWN = 60


def read_asset(name: str) -> bytes:
    """读静态资源，兼容源码 / PyInstaller / zipapp 三种形态。

    顺序上先查 exe 或 pyz 同目录，让用户能直接改 index.html 定制。
    """
    cands = []
    if core.FROZEN:
        cands.append(os.path.join(os.path.dirname(sys.executable), name))
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            cands.append(os.path.join(mei, name))
    elif core.ZIPAPP:
        cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), name))
    cands.append(os.path.join(HERE, name))

    for p in cands:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()

    if core.ZIPAPP:
        import zipfile
        with zipfile.ZipFile(os.path.abspath(sys.argv[0])) as z:
            return z.read(name)
    raise FileNotFoundError(name)


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0

# 前端可见的用户字段白名单。password、iphone 原文、session_key 一律不出网。
USER_FIELDS = (
    "id", "name", "nickname", "headimg", "sex",
    "school", "faculty", "myclass", "id_student", "school_id",
    "mileage", "mileage_km", "run_time", "target_mileage",
    "has_finished", "finished_percent",
    "shortest_distance", "min_pace", "max_pace",
    "min_step_frequency", "max_step_frequency", "distance_to_marker",
)

LOCK = threading.Lock()
STATE = {
    "cli": None,          # core.Ecust
    "user": None,         # dict（已脱敏）
    "phone": None,        # 最近一次登录成功的手机号
    "expired": False,     # 上次交互是否因会话过期失败
    "restore_error": None,
    "sms": {"phone": None, "at": 0.0},
    "run": {
        "running": False, "phase": "idle", "elapsed": 0, "total": 0,
        "mileage": 0, "record_id": None, "log": [], "result": None, "error": None,
    },
}


class SessionExpired(Exception):
    """服务端 code=-2：X-Session-ID 失效。"""


def sanitize_user(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    out = {k: data.get(k) for k in USER_FIELDS if k in data}
    out["phone"] = core.mask_phone(data.get("iphone"))
    return out


def adopt(cli, info: dict):
    """把登录成功的客户端与脱敏用户信息写入全局态，并刷新本地会话缓存。"""
    d = info.get("data") or {}
    with LOCK:
        STATE["cli"] = cli
        STATE["user"] = sanitize_user(info.get("data"))
        STATE["phone"] = d.get("iphone") or STATE["phone"]
        STATE["expired"] = False
        STATE["restore_error"] = None
    if getattr(cli, "sid", None):
        core.save_session({
            "sid": cli.sid, "uid": cli.uid,
            "phone": d.get("iphone"),
            "name": d.get("nickname") or d.get("name"),
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })


def secure(fn, *args, **kw):
    """调用业务接口；code=-2 时清登录态并抛 SessionExpired。"""
    r = fn(*args, **kw)
    if core.is_expired(r):
        with LOCK:
            STATE["cli"] = None
            STATE["user"] = None
            STATE["expired"] = True
        raise SessionExpired("登录已过期，请重新登录")
    return r


def require_login() -> core.Ecust:
    cli = STATE["cli"]
    if cli is None:
        raise SessionExpired("未登录")
    return cli


def rlog(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with LOCK:
        STATE["run"]["log"].append(f"{ts}  {msg}")
        if len(STATE["run"]["log"]) > 300:
            STATE["run"]["log"] = STATE["run"]["log"][-300:]


# ---------------- 后台跑步任务 ----------------
def run_job(mileage: int, wait: int, pass_points: int = 3):
    st = STATE["run"]
    with LOCK:
        st.update(running=True, phase="create", elapsed=0, total=wait,
                  mileage=mileage, record_id=None, log=[], result=None, error=None)
    try:
        c = STATE["cli"]
        if c is None:
            raise SessionExpired("未登录")

        pts = [{"lat": str(la), "lng": str(lo), "point_name": n}
               for (la, lo), n in zip(core.RING[:3], core.POINT_NAMES)]
        rlog("创建跑步记录…")
        rid = secure(c.create_line, pts)
        if not rid:
            raise RuntimeError("createLine 失败，会话可能已过期")
        with LOCK:
            st["record_id"] = rid
        rlog(f"record_id = {rid}")

        track = core.make_track(core.RING, mileage, wait)
        rlog(f"生成轨迹 {len(track)} 点 / {mileage}m / {wait}s")

        t0 = time.time()
        with LOCK:
            st["phase"] = "wait"
        while True:
            el = time.time() - t0
            with LOCK:
                st["elapsed"] = int(el)
            if el >= wait:
                break
            time.sleep(1)

        with LOCK:
            st["phase"] = "submit"
        rlog("提交记录…")
        now = datetime.datetime.now()
        start = datetime.datetime.fromtimestamp(t0)
        body = {"record_id": rid, "pace": round(wait / (mileage / 1000)),
                "running_time": wait, "mileage": mileage,
                "start_time": (start + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "pass_point": pass_points, "step_count": max(30 * wait // 60, wait)}

        ck = secure(c.post, "/xcxapi/checkRecord/", {"a": core.enc(body)})
        ok = (ck.get("data") or {}).get("status") == 1
        rlog(f"checkRecord → {'合格' if ok else (ck.get('data') or {}).get('message') or ck.get('message')}")
        if not ok:
            raise RuntimeError((ck.get("data") or {}).get("message") or "服务端预校验未通过")

        up = secure(c.post, "/xcxapi/updateRecordNew/", {"a": core.enc(body)})
        rlog(f"updateRecordNew → code={up.get('code')}")

        with LOCK:
            st["phase"] = "upload"
        done = 0
        total_b = (len(track) + 49) // 50
        for i in range(0, len(track), 50):
            batch = [{"name": "", "lat": la, "lng": lo, "accuracy": ac,
                      "timestamp": int((start + datetime.timedelta(seconds=j)).timestamp() * 1000)}
                     for j, (la, lo, ac) in enumerate(track[i:i + 50], start=i)]
            r = secure(c.post, "/xcxapi/uploadPathPointV3/",
                       {"img_url": core.enc({"record_id": rid, "path_point": batch, "path_image": ""})})
            if r.get("code") != 1:
                raise RuntimeError(f"轨迹上传失败: {r.get('message')}")
            done += 1
            rlog(f"轨迹批次 {done}/{total_b}")

        info = secure(c.record_info, rid).get("data", {})
        rlog(f"完成  status={info.get('status')}  {info.get('warning') or ''}")
        with LOCK:
            st["result"] = {k: info.get(k) for k in
                            ("id", "mileage", "running_time", "pace", "speed",
                             "step_frequency", "start_time", "end_time", "status", "warning")}
            st["phase"] = "done"
    except SessionExpired as e:
        rlog(f"失败: {e}")
        with LOCK:
            st["error"] = str(e)
            st["phase"] = "error"
    except Exception as e:  # noqa: BLE001
        rlog(f"失败: {e}")
        with LOCK:
            st["error"] = str(e)
            st["phase"] = "error"
        traceback.print_exc()
    finally:
        with LOCK:
            st["running"] = False


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ecust-web/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _origin_ok(self) -> bool:
        """挡掉外部页面直接 POST 到回环端口（CSRF）与 DNS rebinding。"""
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if (self.headers.get("Host") or "").lower() not in hosts:
            return False
        org = (self.headers.get("Origin") or "").lower()
        if org and org not in {f"http://{h}" for h in hosts}:
            return False
        return True

    # ---- 通用结果 ----
    def _relogin(self, msg="登录已过期，请重新登录"):
        self._json({"ok": False, "error": msg, "relogin": True}, 401)

    def _user_bundle(self):
        with LOCK:
            user = STATE["user"]
            phone = STATE["phone"]
        return {"ok": bool(user), "user": user,
                "phone": core.mask_phone(phone) if phone else None}

    # ---- GET ----
    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p in ("/", "/index.html"):
                try:
                    self._send(200, read_asset("index.html"), "text/html; charset=utf-8")
                except FileNotFoundError:
                    self._send(404, b"index.html not found", "text/plain; charset=utf-8")
            elif p == "/api/me":
                self._json(self._user_bundle())
            elif p == "/api/session":
                cached = core.load_session()
                self._json({
                    "ok": True,
                    "active": STATE["user"] is not None,
                    "expired": STATE["expired"],
                    "restore_error": STATE["restore_error"],
                    "frozen": core.PACKAGED,
                    "port": self.server.server_port,
                    "cached": ({
                        "phone": core.mask_phone(cached.get("phone")),
                        "name": cached.get("name"),
                        "saved_at": cached.get("saved_at"),
                    } if cached.get("sid") else None),
                    "user": STATE["user"],
                })
            elif p == "/api/history":
                try:
                    c = require_login()
                    r = secure(c.history, 1, 30)
                except SessionExpired as e:
                    return self._relogin(str(e))
                data = r.get("data") or {}
                lst = data.get("list") if isinstance(data, dict) else data
                self._json({"ok": True,
                            "total": data.get("total") if isinstance(data, dict) else None,
                            "list": lst or []})
            elif p == "/api/run/status":
                with LOCK:
                    st = dict(STATE["run"])
                    st["log"] = list(st["log"])[-120:]
                self._json({"ok": True, "state": st})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 500)

    # ---- POST ----
    def do_POST(self):
        p = urlparse(self.path).path
        if not self._origin_ok():
            return self._json({"ok": False, "error": "来源校验失败"}, 403)
        b = self._body()
        try:
            if p == "/api/login/password":
                self._login_password(b)
            elif p == "/api/login/sms/send":
                self._sms_send(b)
            elif p == "/api/login/sms/verify":
                self._sms_verify(b)
            elif p == "/api/logout":
                self._logout(b)
            elif p == "/api/run/start":
                self._run_start(b)
            elif p == "/api/refresh":
                try:
                    c = require_login()
                    info = secure(c.userinfo)
                except SessionExpired as e:
                    return self._relogin(str(e))
                if info.get("code") == 1:
                    adopt(c, info)
                self._json(self._user_bundle())
            elif p == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": str(e)}, 500)

    # ---- 登录动作 ----
    def _login_password(self, b: dict):
        phone = str(b.get("phone") or "").strip()
        pwd = str(b.get("password") or "")
        if not PHONE_RE.match(phone):
            return self._json({"ok": False, "error": "手机号格式不对"})
        if not pwd:
            return self._json({"ok": False, "error": "请输入密码"})

        c = core.Ecust()
        try:
            r = c.login_password(phone, pwd)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"连不上服务器: {e}"}, 502)
        if r.get("code") != 1 or not c.sid:
            return self._json({"ok": False, "field": "password",
                               "error": r.get("message") or "手机号或密码不正确"})

        info = c.userinfo()
        if info.get("code") != 1:
            return self._json({"ok": False, "field": "password",
                               "error": info.get("message") or "登录成功但读取用户信息失败"})
        adopt(c, info)
        self._json(self._user_bundle())

    def _sms_send(self, b: dict):
        phone = str(b.get("phone") or "").strip()
        if not PHONE_RE.match(phone):
            return self._json({"ok": False, "error": "手机号格式不对"})

        now = time.time()
        with LOCK:
            s = STATE["sms"]
            left = int(SMS_COOLDOWN - (now - s["at"])) if s["phone"] == phone else 0
            if left > 0:
                return self._json({"ok": False, "error": f"请 {left}s 后再试",
                                   "cooldown": left})
            s["phone"], s["at"] = phone, now

        c = core.Ecust()
        try:
            r = c.send_sms(phone)
        except Exception as e:  # noqa: BLE001
            with LOCK:
                STATE["sms"]["at"] = 0.0
            return self._json({"ok": False, "error": f"发送失败: {e}"}, 502)

        if r.get("code") != 1:
            with LOCK:
                STATE["sms"]["at"] = 0.0
            return self._json({"ok": False, "error": r.get("message") or "发送失败"})
        self._json({"ok": True, "message": r.get("message") or "验证码已发送",
                    "cooldown": SMS_COOLDOWN})

    def _sms_verify(self, b: dict):
        phone = str(b.get("phone") or "").strip()
        code = str(b.get("code") or "").strip()
        if not PHONE_RE.match(phone):
            return self._json({"ok": False, "error": "手机号格式不对"})
        if not re.fullmatch(r"\d{4,8}", code):
            return self._json({"ok": False, "error": "验证码应为 4-8 位数字"})

        c = core.Ecust()
        try:
            r = c.login_sms(phone, code)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"连不上服务器: {e}"}, 502)
        if r.get("code") != 1 or not c.sid:
            return self._json({"ok": False, "field": "code",
                               "error": r.get("message") or "验证码不正确或已过期"})

        info = c.userinfo()
        if info.get("code") != 1:
            return self._json({"ok": False, "error": info.get("message") or "读取用户信息失败"})
        with LOCK:
            STATE["sms"] = {"phone": None, "at": 0.0}
        adopt(c, info)
        self._json(self._user_bundle())

    def _logout(self, b: dict):
        purge = b.get("purge", True)
        with LOCK:
            STATE["cli"] = None
            STATE["user"] = None
            STATE["expired"] = False
            STATE["restore_error"] = None
        if purge:
            try:
                os.remove(core.SESSION_FILE)
            except FileNotFoundError:
                pass
            except OSError as e:
                return self._json({"ok": True, "warning": f"会话文件删除失败: {e}"})
        self._json({"ok": True, "purged": bool(purge)})

    def _run_start(self, b: dict):
        try:
            require_login()
        except SessionExpired as e:
            return self._relogin(str(e))
        with LOCK:
            if STATE["run"]["running"]:
                return self._json({"ok": False, "error": "已有任务在跑"})
        mileage = max(2000, int(b.get("mileage") or 2000))
        wait = max(360, int(b.get("wait") or 400))
        threading.Thread(target=run_job, args=(mileage, wait), daemon=True).start()
        self._json({"ok": True})


def main():
    ap = argparse.ArgumentParser(description="华东理工健康跑 WebUI")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--browser", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}"
    open_browser = not a.no_browser and (core.PACKAGED or a.browser)

    # 双击重复启动时复用已有实例，只把页面顶到前面
    if port_in_use(a.port):
        print(f"已有实例在运行: {url}")
        if open_browser:
            webbrowser.open(url)
        return

    s = core.load_session()
    if s.get("sid"):
        try:
            c = core.Ecust(s["sid"], s.get("uid"))
            info = c.userinfo()
            if info.get("code") == 1:
                adopt(c, info)
                print(f"已恢复会话: {STATE['user'].get('nickname')} (id={c.uid})")
            elif core.is_expired(info):
                STATE["expired"] = True
                STATE["restore_error"] = "缓存会话已过期"
                print("缓存会话已过期，需要重新登录")
            else:
                STATE["restore_error"] = info.get("message") or "缓存会话不可用"
                print("缓存会话不可用:", STATE["restore_error"])
        except Exception as e:  # noqa: BLE001
            STATE["restore_error"] = f"恢复会话失败: {e}"
            print(STATE["restore_error"])

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    print(f"WebUI  {url}")
    print(f"数据目录 {core.data_dir()}")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
