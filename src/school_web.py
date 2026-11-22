#!/usr/bin/env python3
"""school_web.py — 校园跑 WebUI

启动:  python school_web.py [--port 8737]
访问:  http://127.0.0.1:8737

接口全部在本地回环，不对外监听。认证与会话复用 school.py。

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
import math
import os
import random
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
import school as core

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
# 单任务令牌：先 claim 再起线程，避免「检查 running → 起线程」之间的窗口
RUN_TOKEN = threading.Lock()


def _claim_run() -> bool:
    return RUN_TOKEN.acquire(blocking=False)


def _release_run():
    try:
        RUN_TOKEN.release()
    except RuntimeError:
        pass


STATE = {
    "cli": None,          # core.School
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


def require_login() -> core.School:
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
def _secure_post(c):
    """把桩客户端的 post 包一层会话失效处理，注入给 core.submit_record。"""
    def post(path, body):
        return secure(c.post, path, body)
    return post


def _record_result(info: dict):
    return {k: info.get(k) for k in
            ("id", "mileage", "running_time", "pace", "speed",
             "step_frequency", "start_time", "end_time", "status", "warning")}


def run_state_view(state: dict):
    """给前端的本地记录摘要：能不能续、还差多少秒、已传多少点。"""
    if not state:
        return None
    resumable = core.run_resumable(state)
    return {"status": state.get("status"),
            "record_id": state.get("record_id"),
            "slot": state.get("slot"),
            "uploaded_points": state.get("uploaded_points"),
            "unresolved": core.run_unresolved(state),
            "resumable": resumable,
            "remaining": (core.run_remaining_ms(state) // 1000) if resumable else None,
            "distance": state.get("distance"),
            "duration_ms": state.get("duration_ms")}


def run_job(mileage: int, wait: int, force: bool = False, accept_invalid: bool = False,
            strict_area: bool = False, skip_area: bool = False):
    """新建一次记录并跑到提交。每次网络副作用之前先把状态写进 run_state.json。"""
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
            if core.run_resumable(prev):
                raise RuntimeError(
                    f"上次记录还能续跑（{prev.get('status')} / record_id={prev.get('record_id')}），"
                    "先用「继续提交」把它交完，别新建")
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

        if not skip_area:
            inside, total, outside = core.check_area(points, origin)
            if inside < total:
                msg = f"{total - inside}/{total} 个点位不在已知校区边界内（{'、'.join(outside)}）"
                if strict_area:
                    raise RuntimeError(msg + "；已知边界只覆盖校区，换校区请关掉边界检查")
                rlog(f"提示：{msg}")
            else:
                rlog(f"点位校验 {inside}/{total} 在校区边界内")

        steps, pace, notes = core.plan_metrics(profile, mileage, wait)
        if notes:
            raise RuntimeError("；".join(notes))
        rlog(f"指标 {mileage}m / {wait}s / {steps} 步 / {pace} min/km")

        t0 = time.time()
        start = datetime.datetime.fromtimestamp(t0)
        start_ms = int(t0 * 1000)
        state = {"slot": core.slot_of(start), "mileage": mileage, "distance": mileage,
                 "wait": wait, "duration_ms": wait * 1000, "start_ms": start_ms,
                 "steps": steps, "pace": pace,
                 "start_time": start.isoformat(timespec="seconds"),
                 "points": [p["point_name"] for p in points],
                 "geo": {"origin": origin, "points": points},
                 "student_id": profile.get("id"),
                 "track_seed": random.randrange(1 << 30),
                 "submission_end_ms": None,
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
            raise RuntimeError("createLine 未返回 record_id，已标记 create_unknown；先到小程序核实")
        state.update(status="created", record_id=rid)
        core.save_run_state(state)
        with LOCK:
            st["record_id"] = rid
        rlog(f"record_id = {rid}")

        with LOCK:
            st["phase"] = "wait"
        while True:
            left = core.run_remaining_ms(state)
            with LOCK:
                st["elapsed"] = int((wait * 1000 - left) / 1000)
            if left <= 0:
                break
            time.sleep(min(1.0, max(0.2, left / 1000.0)))

        with LOCK:
            st["phase"] = "submit"
        rlog("提交记录…")

        def on_event(phase):
            if phase.startswith("轨迹批次"):
                with LOCK:
                    st["phase"] = "upload"

        core.submit_record(_secure_post(c), state, profile,
                           accept_invalid=accept_invalid,
                           log=lambda msg: (on_event(msg), rlog(msg))[1])

        info = secure(c.record_info, rid).get("data", {})
        rlog(f"完成  status={info.get('status')}  {info.get('warning') or ''}")
        with LOCK:
            st["result"] = _record_result(info)
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
        core.mark_failure(state, e) if state else None
        # 堆栈里可能带服务端回显的会话串，落 stderr 前先脱敏
        print(core.redact(traceback.format_exc())[-2000:], file=sys.stderr)
    finally:
        with LOCK:
            st["running"] = False
        _release_run()


def resume_job(wait: bool = False, accept_invalid: bool = False):
    """续跑上次没提交完的记录：不新建、不重新计时，按真实时间差判断是否跑够。"""
    st = STATE["run"]
    with LOCK:
        st.update(running=True, phase="resume", elapsed=0, total=0,
                  mileage=0, record_id=None, log=[], result=None, error=None)
    state = {}
    try:
        c = STATE["cli"]
        if c is None:
            raise SessionExpired("未登录")
        state = core.load_run_state()
        if not state or state.get("status") in core.RUN_TERMINAL:
            raise RuntimeError("没有需要续跑的记录")
        if not core.run_resumable(state):
            raise RuntimeError(
                f"状态 {state.get('status')} 不能续跑：只有 created / check_failed 可以；"
                "create_unknown / finish_unknown 请到小程序核实")

        duration_ms = int(state.get("duration_ms") or 0)
        with LOCK:
            st.update(record_id=state.get("record_id"),
                      mileage=int(state.get("distance") or 0),
                      total=max(1, duration_ms // 1000))
        rlog(f"续跑 record_id={state.get('record_id')} status={state.get('status')} "
             f"已跑 {core.run_moving_ms(state) // 1000}s")

        remaining = core.run_remaining_ms(state)
        if remaining > 0:
            if not wait:
                raise RuntimeError(f"还差 {math.ceil(remaining / 1000)} 秒才够时长；"
                                   "点「继续提交」会等到点，或到小程序核实后标记已核实")
            rlog(f"还没跑够，等待 {math.ceil(remaining / 1000)} 秒…")
            with LOCK:
                st["phase"] = "wait"
            while core.run_remaining_ms(state) > 0:
                left = core.run_remaining_ms(state)
                with LOCK:
                    st["elapsed"] = int((duration_ms - left) / 1000)
                time.sleep(min(1.0, max(0.2, left / 1000.0)))
        else:
            with LOCK:
                st["elapsed"] = int(duration_ms / 1000)

        info = secure(c.userinfo)
        if info.get("code") != 1:
            raise RuntimeError(info.get("message") or "读取用户信息失败")
        profile = info.get("data") or {}
        if str(profile.get("id")) != str(state.get("student_id")):
            raise RuntimeError("当前登录账号与这条记录不一致，不能用它续跑")

        with LOCK:
            st["phase"] = "submit"
        rlog("继续提交…")

        def on_event(phase):
            if phase.startswith("轨迹批次"):
                with LOCK:
                    st["phase"] = "upload"

        core.submit_record(_secure_post(c), state, profile,
                           accept_invalid=accept_invalid,
                           log=lambda msg: (on_event(msg), rlog(msg))[1])

        detail = secure(c.record_info, state["record_id"]).get("data", {})
        rlog(f"完成  status={detail.get('status')}  {detail.get('warning') or ''}")
        with LOCK:
            st["result"] = _record_result(detail)
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
        core.mark_failure(state, e) if state else None
        print(core.redact(traceback.format_exc())[-2000:], file=sys.stderr)
    finally:
        with LOCK:
            st["running"] = False
        _release_run()


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "school-web/1.1"
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
                    "run_state": run_state_view(rs),
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
            elif p == "/api/diagnose":
                if not self._origin_ok():
                    return self._json({"ok": False, "error": "来源校验失败"}, 403)
                with LOCK:
                    st = dict(STATE["run"])
                    run_log = list(st.get("log") or [])
                    user = STATE["user"]
                    expired = STATE["expired"]
                text = core.diagnose(extra={
                    "user": user, "port": self.server.server_port,
                    "phase": st.get("phase"), "running": st.get("running"),
                    "expired": expired, "last_error": st.get("error"),
                }, log=run_log)
                self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
            elif p == "/api/run/status":
                with LOCK:
                    st = dict(STATE["run"])
                    st["log"] = list(st["log"])[-120:]
                rs = core.load_run_state()
                self._json({"ok": True, "state": st, "run_state": run_state_view(rs)})
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
            elif p == "/api/run/resume":
                self._run_resume(b)
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

        c = core.School()
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

        c = core.School()
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

        c = core.School()
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
            resumable = core.run_resumable(prev)
            return self._json({
                "ok": False, "unresolved": True, "can_resume": resumable,
                "status": prev.get("status"), "record_id": prev.get("record_id"),
                "remaining": core.run_remaining_ms(prev) // 1000,
                "error": (f"上次记录还能续跑（{prev.get('status')} / record_id={prev.get('record_id')}），"
                          "先把它交完再新建" if resumable else
                          f"上次记录未收尾（{prev.get('status')} / record_id={prev.get('record_id')}），"
                          "先到小程序核实，再点「已核实」；直接重跑会在服务器上多建一条"),
            })

        accept_invalid = bool(b.get("accept_invalid"))
        strict_area = bool(b.get("strict_area"))
        skip_area = bool(b.get("no_area"))
        with LOCK:
            if STATE["run"]["running"] or not _claim_run():
                return self._json({"ok": False, "error": "已有任务在跑"})
        try:
            threading.Thread(target=run_job,
                             args=(mileage, wait, force, accept_invalid, strict_area, skip_area),
                             daemon=True).start()
        except Exception as e:  # noqa: BLE001
            _release_run()
            return self._json({"ok": False, "error": f"启动任务失败：{e}"})
        self._json({"ok": True, "mileage": mileage, "wait": wait,
                    "steps": steps, "pace": pace, "forced": force})

    def _run_resume(self, b: dict):
        """续跑上次没提交完的记录：不新建，按真实时间差判断是否跑够。"""
        try:
            require_login()
        except SessionExpired as e:
            return self._relogin(str(e))
        with LOCK:
            if STATE["run"]["running"]:
                return self._json({"ok": False, "error": "已有任务在跑"})
        state = core.load_run_state()
        if not state:
            return self._json({"ok": False, "error": "没有本地记录状态"})
        if state.get("status") in core.RUN_TERMINAL:
            return self._json({"ok": False, "error": f"这条记录已是终态（{state.get('status')}）"})
        if not core.run_resumable(state):
            return self._json({
                "ok": False, "status": state.get("status"),
                "error": (f"状态 {state.get('status')} 不能续跑：只有 created / check_failed 可以；"
                          "create_unknown / finish_unknown 请到小程序核实"),
            })

        wait = bool(b.get("wait", True))
        accept_invalid = bool(b.get("accept_invalid"))
        remaining = core.run_remaining_ms(state) // 1000
        with LOCK:
            if STATE["run"]["running"] or not _claim_run():
                return self._json({"ok": False, "error": "已有任务在跑"})
        try:
            threading.Thread(target=resume_job, args=(wait, accept_invalid), daemon=True).start()
        except Exception as e:  # noqa: BLE001
            _release_run()
            return self._json({"ok": False, "error": f"启动续跑失败：{e}"})
        self._json({"ok": True, "record_id": state.get("record_id"),
                    "status": state.get("status"), "remaining": remaining, "wait": wait})

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
    ap = argparse.ArgumentParser(description="校园跑 WebUI")
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
            c = core.School(s["sid"], s.get("uid"))
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
