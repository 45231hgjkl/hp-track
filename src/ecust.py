#!/usr/bin/env python3
"""ecust.py — 华东理工健康跑 CLI（本地研究）

认证三条通道（均来自小程序源码还原）:
  密码登录   POST /xcxapi/userLogin/   {"iphone","password"}   明文密码，无哈希无签名
  短信登录   POST /xcxapi/sendSMS/     {"iphone"}  → POST /xcxapi/login/ {"iphone","code"}
  会话复用   X-Session-ID 头，服务端以 code=-2 表示过期

用法:
  python ecust.py login --phone 15583910181                 # 交互输入密码
  python ecust.py login --phone 15583910181 --password xxx
  python ecust.py login --phone 15583910181 --sms           # 短信验证码登录
  python ecust.py whoami
  python ecust.py history
  python ecust.py run --mileage 2000 --wait 400

会话缓存在脚本同目录 session.json，过期自动提示重新登录。
"""
import argparse
import datetime
import getpass
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request

import pure_aes

BASE = "https://run.ecust.edu.cn"
KEY = b"A7fK92LmXyQw8vRp"
R = 6378137.0
HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))          # PyInstaller
ZIPAPP = bool(sys.argv) and os.path.basename(sys.argv[0]).lower().endswith(".pyz")
PACKAGED = FROZEN or ZIPAPP


def data_dir() -> str:
    """可写数据目录。

    四种运行形态各有各的写位置：
      HP_TRACK_HOME 显式指定（安卓 APK / Chaquopy、只读安装目录、便携 U 盘）
      PyInstaller  __file__ 指向临时解压目录，每次启动都变 → 必须放用户目录
      zipapp       __file__ 是 zip 内的虚拟路径，不是真实目录 → 放 pyz 同目录
      源码         就是脚本目录
    """
    override = os.environ.get("HP_TRACK_HOME")
    if override:
        d = os.path.abspath(override)
        os.makedirs(d, exist_ok=True)
        return d
    if FROZEN:
        base = (os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
                or os.path.expanduser("~/.config"))
        d = os.path.join(base, "hp-track")
    elif ZIPAPP:
        d = os.path.dirname(os.path.abspath(sys.argv[0]))
    else:
        d = HERE
    os.makedirs(d, exist_ok=True)
    return d


SESSION_FILE = os.path.join(data_dir(), "session.json")

# 奉贤校区必经点（randrunInfo 实测）；换校区改这里
RING = [(30.830092, 121.504504), (30.831815, 121.501978),
        (30.8286, 121.505815), (30.830092, 121.504504)]
POINT_NAMES = ["图书馆北", "信息楼南", "南大门"]


# ---------------- 加密 ----------------
def enc(obj: dict) -> str:
    """base64(iv) : base64(AES-128-CBC/PKCS7(json))"""
    return pure_aes.enc(obj, KEY)


def dec(text: str) -> dict:
    return pure_aes.dec(text, KEY)


# ---------------- 客户端 ----------------
class Ecust:
    def __init__(self, sid: str = None, uid: int = None):
        self.sid = sid
        self.uid = uid
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "User-Agent": "MicroMessenger/7.0.20"}
        if self.sid:
            h["X-Session-ID"] = self.sid
        if self.uid:
            h["Authorization"] = ""
        return h

    def _req(self, path: str, body=None, method="POST"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, headers=self._headers(), method=method)
        try:
            with self.op.open(req, timeout=30) as r:
                raw = r.read().decode("utf-8", "replace")
            status = r.status
        except urllib.error.HTTPError as e:
            # 4xx/5xx 也要解析 body：会话失效时服务端可能回 401 而 body 里是 code=-2
            raw = e.read().decode("utf-8", "replace")
            status = e.code
        except urllib.error.URLError as e:
            return {"code": -98, "message": f"网络不可达: {e.reason}"}

        try:
            j = json.loads(raw)
        except Exception:
            j = {"code": -99, "message": raw[:300]}
        if status >= 400:
            j.setdefault("code", -status)
            j.setdefault("message", f"HTTP {status}")
            j["_http_status"] = status
        return j

    def get(self, path: str):
        return self._req(path, method="GET")

    def post(self, path: str, body: dict):
        return self._req(path, body, "POST")

    # ---- 认证 ----
    def login_password(self, phone: str, password: str):
        r = self.post("/xcxapi/userLogin/", {"iphone": phone, "password": password})
        if r.get("code") == 1 and r.get("data"):
            self._adopt(r["data"])
            return r
        return r

    def send_sms(self, phone: str):
        return self.post("/xcxapi/sendSMS/", {"iphone": phone})

    def login_sms(self, phone: str, code: str):
        r = self.post("/xcxapi/login/", {"iphone": phone, "code": code})
        if r.get("code") == 1 and r.get("data"):
            self._adopt(r["data"])
            return r
        return r

    def _adopt(self, data: dict):
        raw = data.get("session_keys") or data.get("session_key")
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        self.sid = raw
        self.uid = data.get("id")
        save_session({"sid": self.sid, "uid": self.uid,
                      "phone": data.get("iphone"), "name": data.get("name"),
                      "saved_at": datetime.datetime.now().isoformat(timespec="seconds")})

    # ---- 业务 ----
    def userinfo(self):
        return self.get(f"/xcxapi/getUserInfo/?id={self.uid}")

    def random_points(self, lat=None, lng=None):
        lat = lat if lat is not None else RING[0][0]
        lng = lng if lng is not None else RING[0][1]
        return self.get(f"/xcxtapi/activity/randrunInfo?lat={lat}&lng={lng}")

    def history(self, page=1, size=20):
        return self.get(f"/xcxapi/recordListNew/?page={page}&page_size={size}")

    def record_info(self, rid):
        return self.get(f"/xcxapi/recordInfo/?id={rid}")

    def create_line(self, points):
        r = self.post("/xcxapi/createLine/", {"student_id": self.uid, "pass_point": points})
        return r.get("data", {}).get("record_id") if r.get("code") == 1 else None


# ---------------- 会话 ----------------
EXPIRED_CODE = -2


def is_expired(r) -> bool:
    """会话失效判定。

    服务端常规用 code=-2；碰到中间层直接回 401 时 body 里没有 code，
    按 HTTP 状态也算失效，否则前端会一直拿 500 而不是跳回登录页。
    """
    if not isinstance(r, dict):
        return False
    return r.get("code") == EXPIRED_CODE or r.get("_http_status") == 401


def mask_phone(p) -> str:
    s = str(p or "")
    return s[:3] + "****" + s[-4:] if len(s) == 11 else s


def save_session(d: dict):
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def load_session() -> dict:
    if os.path.exists(SESSION_FILE):
        try:
            return json.load(open(SESSION_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def client_from_session() -> Ecust:
    s = load_session()
    if not s.get("sid"):
        sys.exit("没有会话，先跑: python ecust.py login --phone <手机号>")
    return Ecust(s.get("sid"), s.get("uid"))


# ---------------- 轨迹 ----------------
def hav(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[1], a[0], b[1], b[0]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def make_track(ring, total_m, seconds, sigma=1.2e-5):
    per = sum(hav(a, b) for a, b in zip(ring, ring[1:]))
    laps = total_m / per
    steps = len(ring) - 1
    dense = []
    for i in range(int(laps * 600) + 1):
        t = (i / 600) % 1.0
        seg, nxt = ring[int(t * steps)], ring[int(t * steps) + 1]
        f = t * steps - int(t * steps)
        dense.append((seg[0] + (nxt[0] - seg[0]) * f, seg[1] + (nxt[1] - seg[1]) * f))
    rng = random.Random()
    step = len(dense) / max(1, seconds)
    out = []
    for i in range(seconds):
        p = dense[int(i * step) % len(dense)]
        out.append((round(p[0] + rng.gauss(0, sigma), 6),
                    round(p[1] + rng.gauss(0, sigma), 6),
                    round(rng.uniform(4.0, 9.0), 1)))
    return out


# ---------------- 命令 ----------------
def cmd_login(a):
    c = Ecust()
    phone = a.phone
    if a.sms:
        print("发送验证码...", c.send_sms(phone))
        code = input("短信验证码: ").strip()
        r = c.login_sms(phone, code)
    else:
        pwd = a.password or os.environ.get("ECUST_PASSWORD") or getpass.getpass("密码: ")
        r = c.login_password(phone, pwd)
    if r.get("code") == 1:
        print(f"登录成功  id={c.uid}  session={c.sid}")
        print("会话已缓存到", SESSION_FILE)
    else:
        print("登录失败:", json.dumps(r, ensure_ascii=False))
        sys.exit(1)


def cmd_whoami(a):
    c = client_from_session()
    r = c.userinfo()
    if r.get("code") != 1:
        print("会话可能已过期:", json.dumps(r, ensure_ascii=False))
        return
    d = r["data"]
    keys = ("id", "name", "nickname", "school", "faculty", "myclass", "id_student",
            "mileage_km", "target_mileage", "finished_percent",
            "shortest_distance", "min_pace", "max_pace",
            "min_step_frequency", "max_step_frequency", "distance_to_marker")
    print(json.dumps({k: d.get(k) for k in keys}, ensure_ascii=False, indent=2))


def cmd_history(a):
    c = client_from_session()
    r = c.history(a.page, a.size)
    if r.get("code") != 1:
        print(json.dumps(r, ensure_ascii=False))
        return
    data = r["data"]
    lst = data.get("list") if isinstance(data, dict) else data
    print(f"total={data.get('total') if isinstance(data, dict) else '?'}")
    for it in (lst or []):
        flag = {1: "合格", 2: "无效"}.get(it.get("status"), it.get("status"))
        print(f"  {it.get('id'):>9}  {str(it.get('start_time')):<20} "
              f"{float(it.get('mileage') or 0)/1000:>6.2f}km  {flag}")


def cmd_run(a):
    c = client_from_session()
    pts = [{"lat": str(la), "lng": str(lo), "point_name": n}
           for (la, lo), n in zip(RING[:3], POINT_NAMES)]
    t0 = datetime.datetime.now()
    rid = c.create_line(pts)
    if not rid:
        sys.exit("createLine 失败，会话可能过期")
    print(f"[{t0:%H:%M:%S}] record_id={rid}  目标 {a.mileage}m / {a.wait}s")

    track = make_track(RING, a.mileage, a.wait)
    left = a.wait - (datetime.datetime.now() - t0).total_seconds()
    while left > 0:
        time.sleep(min(5, max(0.5, left)))
        left = a.wait - (datetime.datetime.now() - t0).total_seconds()
        print(f"\r  剩余 {int(left):>4}s", end="", flush=True)
    print()

    now = datetime.datetime.now()
    body = {"record_id": rid, "pace": round(a.wait / (a.mileage / 1000)),
            "running_time": a.wait, "mileage": a.mileage,
            "start_time": (t0 + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "pass_point": len(pts), "step_count": max(30 * a.wait // 60, a.wait)}

    ck = c.post("/xcxapi/checkRecord/", {"a": enc(body)})
    print("checkRecord:", json.dumps(ck, ensure_ascii=False))
    if ck.get("data", {}).get("status") == 0:
        print("服务端预校验不通过，已中止（未落库）")
        return

    print("update:", json.dumps(c.post("/xcxapi/updateRecordNew/", {"a": enc(body)}), ensure_ascii=False))

    ok = 0
    for i in range(0, len(track), 50):
        batch = [{"name": "", "lat": la, "lng": lo, "accuracy": ac,
                  "timestamp": int((t0 + datetime.timedelta(seconds=j)).timestamp() * 1000)}
                 for j, (la, lo, ac) in enumerate(track[i:i + 50], start=i)]
        r = c.post("/xcxapi/uploadPathPointV3/",
                   {"img_url": enc({"record_id": rid, "path_point": batch, "path_image": ""})})
        if r.get("code") == 1:
            ok += 1
        else:
            print("  batch fail:", json.dumps(r, ensure_ascii=False))
            break
    print(f"track: {ok}/{(len(track) + 49) // 50} 批")

    d = c.record_info(rid).get("data", {})
    print("RESULT:", json.dumps({k: d.get(k) for k in
          ("id", "mileage", "running_time", "pace", "step_frequency", "status", "warning")},
          ensure_ascii=False))


def cmd_session(a):
    print(json.dumps(load_session(), ensure_ascii=False, indent=2))


def main():
    for stream in (sys.stdout, sys.stderr):     # 非 UTF-8 控制台下中文输出会炸
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login")
    p.add_argument("--phone", required=True)
    p.add_argument("--password")
    p.add_argument("--sms", action="store_true")
    p.set_defaults(func=cmd_login)

    sub.add_parser("whoami").set_defaults(func=cmd_whoami)
    sub.add_parser("session").set_defaults(func=cmd_session)

    hp = sub.add_parser("history")
    hp.add_argument("--page", type=int, default=1)
    hp.add_argument("--size", type=int, default=20)
    hp.set_defaults(func=cmd_history)

    rp = sub.add_parser("run")
    rp.add_argument("--mileage", type=int, default=2000)
    rp.add_argument("--wait", type=int, default=400)
    rp.set_defaults(func=cmd_run)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
