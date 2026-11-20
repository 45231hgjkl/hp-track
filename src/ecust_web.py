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

    顺序上先查 HP_TRACK_INDEX（安卓 APK 由壳把 index.html 落到私有目录后指过来），
    再查 exe 或 pyz 同目录，让用户能直接改 index.html 定制。
    """
    override = os.environ.get("HP_TRACK_INDEX")
    if override and os.path.isfile(override):
        with open(override, "rb") as f:
            return f.read()

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
        STATE["run"]["log"].append(f"{ts}  {core.redact(msg)}")
        if len(STATE["run"]["log"]) > 300:
            STATE["run"]["log"] = STATE["run"]["log"][-300:]


# ---------------- 后台跑步任务 ----------------
def run_job(mileage: int, wait: int, force: bool = False, accept_invalid: bool = False):
    """跑一次记录。每次网络副作用之前先把状态写进 run_state.json。

    状态和恢复策略：
      create_pending 未拿到 record_id ⇒ 可以重新准备（请求没发出去的证据）
      create_unknown / finish_unknown ⇒ 结果不确定，绝不自动重试
    """
    st = STATE["run"]
    with LOCK:
        st.update(running=True, phase="create", elapsed=0, total=wait,
                  mileage=mileage, record_id=None, log=[], result=None, error=None)
    state = {}
    try:
        c = STATE["cli"]
        if c is None:
            raise SessionExpired("未登录")
        profile = STATE["user"] or {}

        prev = core.load_run_state()
        if core.run_unresolved(prev) and not force:
            raise RuntimeError(
                f"上次记录未收尾（{prev.get('status')} / record_id={prev.get('record_id')}），"
                "先到小程序核实，再点「已核实」或加 force 重跑")

        rlog("获取本次考核点位…")
        try:
            points, origin = core.parse_randrun(
                secure(c.random_points, core.QUERY_LAT, core.QUERY_LNG))
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        rlog("点位 " + " / ".join(p["point_name"] for p in points))

        steps, pace, notes = core.plan_metrics(profile, mileage, wait)
        if notes:
            raise RuntimeError("；".join(notes))
        rlog(f"指标 {mileage}m / {wait}s / {steps} 步 / {pace} min/km")

        t0 = time.time()
        start = datetime.datetime.fromtimestamp(t0)
        state = {"slot": core.slot_of(start), "mileage": mileage, "wait": wait,
                 "steps": steps, "pace": pace,
                 "start_time": start.isoformat(timespec="seconds"),
                 "points": [p["point_name"] for p in points],
                 "record_id": None, "status": "create_pending"}
        core.save_run_state(state)

        rlog("创建跑步记录…")
        try:
            rid = secure(c.create_line, points)
        except SessionExpired:
            state.update(status="create_unknown", error="会话失效，创建结果未知")
            core.save_run_state(state)
            raise
        except Exception as e:  # noqa: BLE001
            state.update(status="create_unknown", error=type(e).__name__)
            core.save_run_state(state)
            raise RuntimeError(f"createLine 未完成：{e}") from e
        if not rid:
            state.update(status="create_unknown", error="createLine 未返回 record_id")
            core.save_run_state(state)
            raise RuntimeError("createLine 未返回 record_id，已标记 create_unknown")
        state.update(status="created", record_id=rid)
        core.save_run_state(state)
        with LOCK:
            st["record_id"] = rid
        rlog(f"record_id = {rid}")

        track = core.make_track(points, origin, mileage, wait)
        rlog(f"生成轨迹 {len(track)} 点 / {mileage}m / {wait}s")

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
        body = {"record_id": rid, "pace": pace,
                "running_time": wait, "mileage": mileage,
                "start_time": (start + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "pass_point": len(points), "step_count": steps}

        state["status"] = "checking"
        core.save_run_state(state)
        try:
            ck = secure(c.post, "/xcxapi/checkRecord/", {"a": core.enc(body)})
        except SessionExpired:
            # 会话失效不等于记录有问题：退回 created，重新登录后还能再提
            state.update(status="created", error="会话失效，可重新登录后重提")
            core.save_run_state(state)
            raise
        kind, reason, _ = core.check_verdict(ck)
        if kind == "unknown":
            state.update(status="check_failed", reason=reason)
            core.save_run_state(state)
            raise RuntimeError(f"校验状态无法识别：{reason}")
        if kind == "invalid":
            if not accept_invalid:
                state.update(status="check_failed", reason=reason)
                core.save_run_state(state)
                raise RuntimeError(f"服务端预校验不通过：{reason}")
            rlog(f"服务端判为不合格（{reason}），按确认继续提交")
        else:
            rlog("checkRecord → 合格")

        state["status"] = "finish_pending"
        core.save_run_state(state)
        try:
            up = secure(c.post, "/xcxapi/updateRecordNew/", {"a": core.enc(body)})
        except BaseException as e:
            state.update(status="finish_unknown", error=f"updateRecordNew 未完成：{type(e).__name__}")
            core.save_run_state(state)
            raise
        rlog(f"updateRecordNew → code={up.get('code')}")
        if up.get("code") != 1:
            state.update(status="finish_unknown", error=str(up.get("message") or "")[:200])
            core.save_run_state(state)
            raise RuntimeError(f"updateRecordNew 未返回成功：{up.get('message') or '未知'}")
        state["status"] = "record_updated"
        core.save_run_state(state)

        with LOCK:
            st["phase"] = "upload"
        done = 0
        total_b = (len(track) + 49) // 50
        for i in range(0, len(track), 50):
            batch = [{"name": "", "lat": lat, "lng": lng, "accuracy": acc,
                      "timestamp": int((start + datetime.timedelta(seconds=off)).timestamp() * 1000)}
                     for (lat, lng, acc, off) in track[i:i + 50]]
            try:
                r = secure(c.post, "/xcxapi/uploadPathPointV3/",
                           {"img_url": core.enc({"record_id": rid, "path_point": batch, "path_image": ""})})
            except BaseException as e:
                state.update(status="finish_unknown", error=f"轨迹上传中断：{type(e).__name__}",
                             uploaded_points=min(i, len(track)))
                core.save_run_state(state)
                raise
            if r.get("code") != 1:
                state.update(status="finish_unknown", error="轨迹上传被拒",
                             uploaded_points=min(i, len(track)))
                core.save_run_state(state)
                raise RuntimeError(f"轨迹上传失败: {r.get('message')}")
            done += 1
            state["uploaded_points"] = min(i + 50, len(track))
            core.save_run_state(state)
            rlog(f"轨迹批次 {done}/{total_b}")

        info = secure(c.record_info, rid).get("data", {})
        rlog(f"完成  status={info.get('status')}  {info.get('warning') or ''}")
        state.update(status="finished", uploaded_points=len(track))
        core.save_run_state(state)
        with LOCK:
            st["result"] = {k: info.get(k) for k in
                            ("id", "mileage", "running_time", "pace", "speed",
                             "step_frequency", "start_time", "end_time", "status", "warning")}
            st["phase"] = "done"
    except SessionExpired as e:
        rlog(f"失败: {e}")
        with LOCK:
            st["error"] = core.redact(str(e))
            st["phase"] = "error"
    except Exception as e:  # noqa: BLE001
        rlog(f"失败: {e}")
        with LOCK:
            st["error"] = core.redact(str(e))
            st["phase"] = "error"
        # 堆栈里可能带服务端回显的会话串，落 stderr 前先脱敏
        print(core.redact(traceback.format_exc())[-2000:], file=sys.stderr)
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
        pace = None
        if user:
            pr = core.pace_range(user)
            pace = {"range": list(pr) if pr else None,
                    "suggest": (pr[0] + pr[1]) / 2 if pr else core.PACE_FALLBACK}
        return {"ok": bool(user), "user": user,
                "phone": core.mask_phone(phone) if phone else None,
                "pace": pace}

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
                rs = core.load_run_state()
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
                    "run_state": ({
                        "status": rs.get("status"),
                        "record_id": rs.get("record_id"),
                        "slot": rs.get("slot"),
                        "uploaded_points": rs.get("uploaded_points"),
                        "unresolved": core.run_unresolved(rs),
                    } if rs else None),
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
                rs = core.load_run_state()
                self._json({"ok": True, "state": st, "run_state": ({
                    "status": rs.get("status"),
                    "record_id": rs.get("record_id"),
                    "slot": rs.get("slot"),
                    "uploaded_points": rs.get("uploaded_points"),
                    "unresolved": core.run_unresolved(rs),
                } if rs else None)})
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
            elif p == "/api/run/resolve":
                self._run_resolve(b)
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
            profile = STATE["user"] or {}

        try:
            mileage = max(2000, int(b.get("mileage") or 2000))
            wait = int(b.get("wait") or 0)
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "里程和时长必须是整数"})
        if wait <= 0:
            wait = core.plan_wait(profile, mileage)
        wait = max(360, wait)

        steps, pace, notes = core.plan_metrics(profile, mileage, wait)
        if notes:
            return self._json({"ok": False, "error": "参数不合规：" + "；".join(notes),
                               "suggest": {"wait": core.plan_wait(profile, mileage)}})

        force = bool(b.get("force"))
        prev = core.load_run_state()
        if core.run_unresolved(prev) and not force:
            return self._json({
                "ok": False, "unresolved": True,
                "status": prev.get("status"), "record_id": prev.get("record_id"),
                "error": (f"上次记录未收尾（{prev.get('status')} / record_id={prev.get('record_id')}），"
                          "先到小程序核实，再点「已核实」；直接重跑会在服务器上多建一条"),
            })

        accept_invalid = bool(b.get("accept_invalid"))
        threading.Thread(target=run_job, args=(mileage, wait, force, accept_invalid),
                         daemon=True).start()
        self._json({"ok": True, "mileage": mileage, "wait": wait,
                    "steps": steps, "pace": pace, "forced": force})

    def _run_resolve(self, b: dict):
        """把未收尾的本地记录标记为已核实：只动本地状态，不碰服务器记录。"""
        with LOCK:
            if STATE["run"]["running"]:
                return self._json({"ok": False, "error": "任务进行中，先等它结束"})
        state = core.load_run_state()
        if not state:
            return self._json({"ok": True, "cleared": False, "reason": "没有本地记录状态"})
        if state.get("status") in core.RUN_TERMINAL:
            return self._json({"ok": True, "cleared": False, "status": state.get("status")})
        previous = state.get("status")
        state.update(status="discarded", previous_status=previous,
                     discarded_at=datetime.datetime.now().isoformat(timespec="seconds"))
        core.save_run_state(state)
        self._json({"ok": True, "cleared": True, "previous_status": previous,
                    "record_id": state.get("record_id")})


def _force_utf8_stdio():
    """Windows 控制台/CI 的默认代码页可能是 cp1252/GBK，中文 print 会 UnicodeEncodeError。

    冻结产物（exe）里绝对不能因为一行日志把服务打死，所以统一把 stdio 设成 UTF-8；
    失败就退化成 replace，不抛异常。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _force_utf8_stdio()
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
