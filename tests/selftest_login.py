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

# randrunInfo 的构造响应：data 里是当次三个考核点，顶层带起点与账号。
# point_name 刻意与本地不再使用的硬编码点位不同，用来证明点位确实来自服务端。
RANDRUN = {"code": 1, "message": "ok", "lat": 30.830092, "lng": 121.504504, "student_id": 13101,
           "data": [{"id": 11, "point_name": "服务端点甲", "lat": "30.830100", "lng": "121.504500"},
                    {"id": 12, "point_name": "服务端点乙", "lat": "30.831800", "lng": "121.501900"},
                    {"id": 13, "point_name": "服务端点丙", "lat": "30.828600", "lng": "121.505800"}]}

PASSED, FAILED = [], []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"   {extra}"))


class Fake:
    """每次实例化可指定行为：mode=normal|expired"""

    calls = []
    bodies = {}
    points = None
    fail_create = False
    check_status = 1
    check_message = ""

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

    # ---- 跑步链路（run_job 会用到）----
    def random_points(self, lat=None, lng=None):
        Fake.calls.append(("randrunInfo", lat, lng))
        return RANDRUN

    def create_line(self, points):
        Fake.points = points
        Fake.calls.append(("createLine", len(points)))
        if Fake.fail_create:
            raise RuntimeError("fixture createLine failed")
        return 987654

    def post(self, path, body=None):
        Fake.calls.append(("post", path))
        Fake.bodies[path] = body
        if path == "/xcxapi/checkRecord/":
            return {"code": 1, "data": {"status": Fake.check_status,
                                        "message": Fake.check_message}}
        return {"code": 1}

    def record_info(self, rid):
        return {"code": 1, "data": {"id": rid, "status": 1, "mileage": 2000, "running_time": 6}}


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
    # 不读写真实 session.json / run_state.json
    core.SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest_session.json")
    core.RUN_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest_run_state.json")
    for path in (core.SESSION_FILE, core.RUN_STATE_FILE, core.RUN_STATE_FILE + ".tmp"):
        if os.path.exists(path):
            os.remove(path)

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

        print("\n[9] 指标口径（配速单位 / 步频夹取 / 头净化）")
        prof = {"min_pace": 5.0, "max_pace": 6.5,
                "min_step_frequency": 150, "max_step_frequency": 200,
                "shortest_distance": 2000}
        steps, pace, notes = core.plan_metrics(prof, 2000, 660)
        check("pace 按分钟/公里", pace == 5.5, pace)
        check("合规参数无告警", notes == [], notes)
        check("步频落在服务端区间", 150 <= steps / (660 / 60) <= 200, steps)
        _, _, bad = core.plan_metrics(prof, 2000, 400)
        check("配速越界被识别", any("配速" in n for n in bad), bad)
        wait_mid = core.plan_wait(prof, 2000)
        check("plan_wait 落在配速区间内", 600 <= wait_mid <= 780, wait_mid)
        check("缺字段不臆造约束", core.pace_range({}) is None and core.freq_range({}) is None)
        check("header 注入被丢弃", core.clean_headers({"X-Session-ID": "a\r\nB: x"}) == {})
        check("header 正常值保留", core.clean_headers({"X-Session-ID": "SID-1"}) == {"X-Session-ID": "SID-1"})

        print("\n[10] 跑步链路（点位来自 randrunInfo）")
        Fake.calls.clear()
        Fake.bodies.clear()
        W.run_job(2000, 6)
        st = W.STATE["run"]
        check("调用了 randrunInfo", any(c[0] == "randrunInfo" for c in Fake.calls), Fake.calls[:3])
        names = [p.get("point_name") for p in (Fake.points or [])]
        check("createLine 用服务端三点", names == ["服务端点甲", "服务端点乙", "服务端点丙"], names)
        check("createLine 点位数为 3", len(Fake.points or []) == 3, Fake.points)
        body = core.dec(Fake.bodies["/xcxapi/checkRecord/"]["a"])
        check("提交体 pass_point=3", body.get("pass_point") == 3, body)
        check("提交体 pace 为分钟/公里", abs(float(body.get("pace")) - 0.05) < 1e-9, body.get("pace"))
        check("提交体步数为正整数",
              isinstance(body.get("step_count"), int) and body["step_count"] > 0, body.get("step_count"))
        check("任务跑到 done", st["phase"] == "done", st)
        check("日志含服务端点位名", any("服务端点甲" in line for line in st["log"]), st["log"])
        check("轨迹按批上传", any("轨迹批次" in line for line in st["log"]), st["log"])

        print("\n[11] 参数越界在启动前拦截")
        W.STATE["user"]["min_pace"] = 5.0
        W.STATE["user"]["max_pace"] = 6.5
        s, j = req(base, "/api/run/start", {"mileage": 2000, "wait": 400})
        check("越界配速被拒", j.get("ok") is False and "配速" in (j.get("error") or ""), j)
        check("返回建议时长", (j.get("suggest") or {}).get("wait", 0) > 0, j)
        W.STATE["user"].pop("min_pace", None)
        W.STATE["user"].pop("max_pace", None)

        print("\n[12] 状态机：创建结果未知时不重复建")
        core.clear_run_state()
        Fake.calls.clear()
        Fake.fail_create = True
        W.run_job(2000, 2)
        st12 = core.load_run_state()
        check("createLine 失败 → create_unknown", st12.get("status") == "create_unknown", st12)
        check("被判为未收尾", core.run_unresolved(st12) is True, st12.get("status"))
        check("未拿到 record_id", st12.get("record_id") is None, st12.get("record_id"))
        s, j = req(base, "/api/run/start", {"mileage": 2000, "wait": 400})
        check("再次启动被拦", j.get("ok") is False and j.get("unresolved") is True, j)
        check("拦截信息带状态", j.get("status") == "create_unknown", j)
        Fake.fail_create = False
        s, j = req(base, "/api/run/resolve", {})
        check("resolve 清掉未收尾", j.get("ok") is True and j.get("cleared") is True, j)
        check("清掉后可重新开跑", core.run_unresolved(core.load_run_state()) is False)

        print("\n[13] checkRecord 判不合格时不再提交")
        core.clear_run_state()
        Fake.calls.clear()
        Fake.check_status = 0
        W.run_job(2000, 2)
        st13 = core.load_run_state()
        check("status=0 → check_failed", st13.get("status") == "check_failed", st13)
        check("未发 updateRecordNew",
              ("post", "/xcxapi/updateRecordNew/") not in Fake.calls, Fake.calls[-4:])
        check("未上传轨迹",
              ("post", "/xcxapi/uploadPathPointV3/") not in Fake.calls, Fake.calls[-4:])
        Fake.check_status = 1

        print("\n[14] 日志脱敏（服务端回显也不外泄）")
        core.clear_run_state()
        Fake.check_status = 0
        Fake.check_message = f"失败 {PHONE} sid=SID-abcdefghijklmnopqrstuvwxyz012345"
        W.run_job(2000, 2)
        log = "\n".join(W.STATE["run"]["log"])
        check("redact 打码手机号", "155****0181" in core.redact(f"phone={PHONE}"), core.redact(f"phone={PHONE}"))
        check("redact 抹掉长 token", "<redacted>" in core.redact("sid=abcdefghijklmnopqrstuvwx1234"))
        check("日志不含原始手机号", PHONE not in log, log[-300:])
        check("日志不含原始 sid", "SID-abcdefghijklmnopqrstuvwxyz012345" not in log, log[-300:])
        err = W.STATE["run"].get("error") or ""
        check("失败原因也不含原始串", PHONE not in err and "SID-abcdefghijklmnopqrstuvwxyz012345" not in err, err)
        Fake.check_status = 1
        Fake.check_message = ""
        core.clear_run_state()

        print("\n[8] 退出清理")
        s, j = req(base, "/api/logout", {"purge": True})
        check("logout ok", j.get("ok") is True and j.get("purged") is True, j)
        check("session.json 已删除", not os.path.exists(core.SESSION_FILE))
        s, j = req(base, "/api/me")
        check("退出后 me 为空", j.get("ok") is False, j)
    finally:
        srv.shutdown()
        for path in (core.SESSION_FILE, core.RUN_STATE_FILE, core.RUN_STATE_FILE + ".tmp"):
            if os.path.exists(path):
                os.remove(path)

    print(f"\n{'='*46}\nPASS {len(PASSED)}  FAIL {len(FAILED)}")
    for f in FAILED:
        print("  未通过:", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

