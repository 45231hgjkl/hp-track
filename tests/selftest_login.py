#!/usr/bin/env python3
"""selftest_login.py — ecust_web 登录链路离线自测

不连真实服务器：用 Fake 客户端替换 core.Ecust，验证登录成功/失败、
会话过期、字段脱敏、Origin 防护与 SMS 冷却逻辑。

    python selftest_login.py
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import ecust as core
import ecust_web as W

GOOD_PWD = "good-pass"
GOOD_CODE = "123456"
PHONE = "15583910181"

PASSED, FAILED = [], []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"   {extra}"))


class Fake:
    """每次实例化可指定行为：mode=normal|expired"""

    calls = []

    def __init__(self, sid=None, uid=None):
        self.sid, self.uid = sid, uid
        self.mode = "normal"

    def login_password(self, phone, pwd):
        Fake.calls.append(("password", phone))
        if pwd != GOOD_PWD:
            return {"code": 0, "message": "账号或密码不正确！"}
        self.sid, self.uid = "SID-LIST-CASE", 13101
        return {"code": 1, "data": {"id": 13101, "session_keys": ["SID-LIST-CASE"], "iphone": phone}}

    def send_sms(self, phone):
        Fake.calls.append(("sms_send", phone))
        return {"code": 1, "message": "发送成功"}

    def login_sms(self, phone, code):
        Fake.calls.append(("sms_verify", phone))
        if code != GOOD_CODE:
            return {"code": 0, "message": "验证码错误"}
        self.sid, self.uid = "SID-SMS", 13101
        return {"code": 1, "data": {"id": 13101, "session_keys": ["SID-SMS"], "iphone": phone}}

    def userinfo(self):
        if self.mode == "expired":
            return {"code": -2, "message": "登录超时"}
        return {"code": 1, "data": {
            "id": 13101, "nickname": "ForesTree", "name": "杨林涣",
            "iphone": int(PHONE), "password": "d723cb1ef2a9d290494c63c1eb5176b3",
            "id_number": "310000000000000000", "session_key": "1777810067.161517",
            "contact": "吴老师", "tel": "13917641784", "headimg": "http://x/a.png",
            "school": "奉贤校区", "faculty": "艺术设计与传媒学院", "myclass": "风景2302",
            "mileage": 70579.0, "mileage_km": 70.58, "target_mileage": 60,
            "finished_percent": 4, "shortest_distance": 2000,
        }}

    def history(self, page, size):
        if self.mode == "expired":
            return {"code": -2, "message": "登录超时"}
        return {"code": 1, "data": {"total": 1, "list": [{
            "id": 5548410, "status": 1, "mileage": 2000,
            "running_time": 400, "start_time": "2026-11-19 13:00:00"}]}}


def req(base, path, body=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, headers=h,
                               method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main():
    core.Ecust = Fake
    W.core.Ecust = Fake
    W.SMS_COOLDOWN = 2
    # 不读写真实 session.json
    core.SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest_session.json")
    if os.path.exists(core.SESSION_FILE):
        os.remove(core.SESSION_FILE)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), W.Handler)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        print("\n[1] 未登录态")
        s, j = req(base, "/api/me")
        check("me 返回 ok=false", j.get("ok") is False and j.get("user") is None, j)
        s, j = req(base, "/api/history")
        check("history 未登录 401 relogin", s == 401 and j.get("relogin"), f"{s} {j}")
        s, j = req(base, "/api/run/start", {"mileage": 2000})
        check("run/start 未登录 401 relogin", s == 401 and j.get("relogin"), f"{s} {j}")

        print("\n[2] 密码登录失败")
        s, j = req(base, "/api/login/password", {"phone": PHONE, "password": "wrong"})
        check("错误密码回 field=password", j.get("ok") is False and j.get("field") == "password", j)
        check("错误文案透出", "不正确" in (j.get("error") or ""), j)
        s, j = req(base, "/api/login/password", {"phone": "123", "password": "x"})
        check("手机号格式拦截", j.get("error") == "手机号格式不对", j)

        print("\n[3] 密码登录成功（session_keys 为数组）")
        s, j = req(base, "/api/login/password", {"phone": PHONE, "password": GOOD_PWD})
        u = j.get("user") or {}
        check("ok=true", j.get("ok") is True, j)
        check("sid 从数组取下标量", W.STATE["cli"].sid == "SID-LIST-CASE", W.STATE["cli"].sid)
        check("password 字段不外泄", "password" not in u, list(u))
        check("id_number 不外泄", "id_number" not in u, list(u))
        check("session_key 不外泄", "session_key" not in u, list(u))
        check("iphone 打码", u.get("phone") == "155****0181", u.get("phone"))
        check("业务字段保留", u.get("nickname") == "ForesTree" and u.get("target_mileage") == 60, u)
        check("缓存 session.json 落盘", os.path.exists(core.SESSION_FILE))
        s, j = req(base, "/api/session")
        check("session 显示 active + 打码手机", j.get("active") and j["cached"]["phone"] == "155****0181", j)

        print("\n[4] 已登录业务")
        s, j = req(base, "/api/history")
        check("history ok", j.get("ok") and len(j.get("list") or []) == 1, j)

        print("\n[5] 会话过期统一拦截")
        W.STATE["cli"].mode = "expired"
        s, j = req(base, "/api/refresh", {})
        check("refresh 401 relogin", s == 401 and j.get("relogin"), f"{s} {j}")
        check("过期后内存态清空", W.STATE["cli"] is None and W.STATE["user"] is None, W.STATE["cli"])
        s, j = req(base, "/api/session")
        check("session 标记 expired", j.get("expired") is True and j.get("active") is False, j)

        print("\n[6] SMS 登录 + 冷却")
        s, j = req(base, "/api/login/sms/send", {"phone": PHONE})
        check("首次发送 ok", j.get("ok") is True, j)
        s, j = req(base, "/api/login/sms/send", {"phone": PHONE})
        check("二次发送被冷却", j.get("ok") is False and j.get("cooldown", 0) > 0, j)
        s, j = req(base, "/api/login/sms/verify", {"phone": PHONE, "code": "000000"})
        check("错误验证码回 field=code", j.get("field") == "code", j)
        s, j = req(base, "/api/login/sms/verify", {"phone": PHONE, "code": GOOD_CODE})
        check("正确验证码登录成功", j.get("ok") is True and j["user"]["nickname"] == "ForesTree", j)
        check("SMS 登录后清冷却", W.STATE["sms"]["phone"] is None, W.STATE["sms"])

        print("\n[7] Origin / Host 防护")
        s, j = req(base, "/api/login/password",
                   {"phone": PHONE, "password": GOOD_PWD}, {"Origin": "http://evil.example"})
        check("跨源 Origin 403", s == 403, f"{s} {j}")
        s, j = req(base, "/api/login/password",
                   {"phone": PHONE, "password": GOOD_PWD}, {"Host": "attacker.com"})
        check("非法 Host 403", s == 403, f"{s} {j}")

        print("\n[8] 退出清理")
        s, j = req(base, "/api/logout", {"purge": True})
        check("logout ok", j.get("ok") is True and j.get("purged") is True, j)
        check("session.json 已删除", not os.path.exists(core.SESSION_FILE))
        s, j = req(base, "/api/me")
        check("退出后 me 为空", j.get("ok") is False, j)
    finally:
        srv.shutdown()
        if os.path.exists(core.SESSION_FILE):
            os.remove(core.SESSION_FILE)

    print(f"\n{'='*46}\nPASS {len(PASSED)}  FAIL {len(FAILED)}")
    for f in FAILED:
        print("  未通过:", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

