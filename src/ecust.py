#!/usr/bin/env python3
"""ecust.py — 华东理工健康跑 CLI（本地研究）

认证三条通道（均来自小程序源码还原）:
  密码登录   POST /xcxapi/userLogin/   {"iphone","password"}   明文密码，无哈希无签名
  短信登录   POST /xcxapi/sendSMS/     {"iphone"}  → POST /xcxapi/login/ {"iphone","code"}
  会话复用   X-Session-ID 头，服务端以 code=-2 表示过期

模拟指标口径（与小程序一致）:
  点位      必须取当次 GET /xcxtapi/activity/randrunInfo?lat&lng 的 data
            本地不编坐标，只负责把这三个点跑完
  配速      pace 字段单位为「分钟/公里」= 时长秒 / 60 / 公里数
  步频      step_count / (时长分钟)，用 getUserInfo 的上下限夹取
  预校验    shortest_distance / min_pace / max_pace / min_step_frequency /
            max_step_frequency 缺失即跳过该项，不臆造约束

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
import re
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

# randrunInfo 的查询坐标：只用来告诉服务端「我在奉贤校区」，不参与点位构造。
# 考核点位一律以服务端当次返回的 data 为准（见 parse_randrun）。
QUERY_LAT, QUERY_LNG = 30.830092, 121.504504

# 服务端未给配速区间时的保守取值（分钟/公里），以及步频的默认中位值。
PACE_FALLBACK = 5.5
FREQ_FALLBACK = 172.0

# Header 只放这些键；值是本地会话文件读回来的，必须过一遍净化。
HEADER_KEYS = ("X-Session-ID", "Authorization")
HEADER_VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=\-]{1,512}$")


# ---------------- 加密 ----------------
def enc(obj: dict) -> str:
    """base64(iv) : base64(AES-128-CBC/PKCS7(json))"""
    return pure_aes.enc(obj, KEY)


def dec(text: str) -> dict:
    return pure_aes.dec(text, KEY)


# ---------------- 指标口径 ----------------
def _num(value):
    """服务端字段可能是数字、数字字符串或 null。转不出来就返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _range(lo, hi, cap):
    """取服务端给的双端区间；不合法（缺、反向、离谱）就当没给。"""
    a, b = _num(lo), _num(hi)
    if a is None or b is None or not (0 < a <= b <= cap):
        return None
    return a, b


def pace_range(profile: dict):
    """配速区间（分钟/公里）。服务端字段缺失时返回 None。"""
    p = profile or {}
    return _range(p.get("min_pace"), p.get("max_pace"), 30.0)


def freq_range(profile: dict):
    """步频区间（步/分钟）。"""
    p = profile or {}
    return _range(p.get("min_step_frequency"), p.get("max_step_frequency"), 300.0)


def parse_randrun(payload):
    """解析 randrunInfo，返回 (points, origin)。

    points 为 3 个点位的原样字典（保留服务端给的 id / point_name 等字段），
    origin 为服务端返回的起点 lat/lng（缺则退回第一个点位）。
    形状容错：data 可能是 [3 点] 也可能是 {"points": [...], "lat":.., "lng":..}。
    """
    if not isinstance(payload, dict):
        raise ValueError("randrunInfo 响应不是 JSON 对象")
    if payload.get("code") != 1:
        msg = payload.get("message") or payload.get("msg") or "未知原因"
        raise ValueError(f"randrunInfo 失败：{msg}")

    data = payload.get("data")
    if isinstance(data, list):
        raw, meta = data, payload
    elif isinstance(data, dict):
        raw = data.get("points") or data.get("pass_point") or []
        meta = {**payload, **data}
    else:
        raise ValueError("randrunInfo 响应缺少 data")

    points = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        lat, lng = _num(item.get("lat")), _num(item.get("lng"))
        if lat is None or lng is None or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            continue
        name = str(item.get("point_name") or item.get("name") or "").strip()[:40] or "考核点"
        # 原样回传服务端字段，只把坐标归一成字符串（小程序侧就是 string）
        point = dict(item)
        point["lat"], point["lng"], point["point_name"] = str(lat), str(lng), name
        points.append(point)

    if len(points) != 3:
        raise ValueError(f"randrunInfo 返回 {len(points)} 个有效点位，需要 3 个")

    olat, olng = _num(meta.get("lat")), _num(meta.get("lng"))
    if olat is None or olng is None:
        olat, olng = float(points[0]["lat"]), float(points[0]["lng"])
    return points, {"lat": olat, "lng": olng}


def plan_metrics(profile, mileage: int, seconds: int):
    """按服务端区间产出一组能过预校验的指标。

    返回 (steps, pace, notes)：
      steps  步数（步频夹进区间）
      pace   配速，单位分钟/公里，已 round
      notes  不合规说明；空列表表示可以提交
    """
    km = mileage / 1000.0
    if km <= 0 or seconds <= 0:
        raise ValueError("里程和时长必须为正数")

    pace = seconds / 60.0 / km
    notes = []
    pr = pace_range(profile)
    if pr and not (pr[0] <= pace <= pr[1]):
        notes.append(f"配速 {pace:.2f} min/km 不在服务端区间 {pr[0]:.2f}–{pr[1]:.2f}")

    freq = FREQ_FALLBACK
    fr = freq_range(profile)
    if fr:
        freq = min(max(freq, fr[0]), fr[1])
    steps = max(1, int(round(freq * seconds / 60.0)))
    return steps, round(pace, 2), notes


def plan_wait(profile, mileage: int) -> int:
    """给定里程，按服务端中值配速（或保守默认）推出时长秒数。"""
    pr = pace_range(profile)
    pace = (pr[0] + pr[1]) / 2 if pr else PACE_FALLBACK
    return max(360, int(round(pace * mileage / 1000.0 * 60.0)))


def clean_headers(headers: dict) -> dict:
    """只保留白名单 header，并拒绝 CR/LF 等注入字符。

    会话是从 session.json 读回来的，等于信任本地文件；这里加一道闸，
    防止被改过的会话文件把任意头带进请求。
    """
    out = {}
    for key, value in (headers or {}).items():
        if key not in HEADER_KEYS:
            continue
        text = str(value)
        if not HEADER_VALUE_RE.match(text):
            continue
        out[key] = text
    return out


# ---------------- 客户端 ----------------
class Ecust:
    def __init__(self, sid: str = None, uid: int = None):
        self.sid = sid
        self.uid = uid
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "User-Agent": "MicroMessenger/7.0.20"}
        # 会话来自 session.json，先过白名单/字符净化再拼进请求
        if self.sid:
            h.update(clean_headers({"X-Session-ID": self.sid}))
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
        lat = lat if lat is not None else QUERY_LAT
        lng = lng if lng is not None else QUERY_LNG
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


def ring_from(points, origin):
    """把「起点 + 三点」拼成闭环折线，首尾都是起点。"""
    ring = [(float(origin["lat"]), float(origin["lng"]))]
    ring += [(float(p["lat"]), float(p["lng"])) for p in points]
    ring.append(ring[0])
    return ring


def make_track(points, origin, total_m, seconds, sigma=1.2e-5):
    """沿闭环折线铺满 total_m 里程，每秒取一个采样点。

    返回 [(lat, lng, accuracy, offset_seconds), ...]。offset 是相对起跑的秒数，
    上传时用它算 timestamp —— 采样密度由这里决定，调用方不再假设「每秒一点」。

    点位来自 randrunInfo，本函数只负责「怎么把这三个点跑完」。
    """
    if not points:
        raise ValueError("缺少考核点位")
    ring = ring_from(points, origin)
    seg = [hav(a, b) for a, b in zip(ring, ring[1:])]
    per = sum(seg)
    if per <= 0:
        raise ValueError("点位重合，无法生成轨迹")

    total_m = max(float(total_m), 1.0)
    seconds = max(1, int(seconds))
    count = max(2, int(total_m / 0.5) + 1)
    dense = []
    for k in range(count):
        s = total_m * k / (count - 1)
        d, acc = s % per, 0.0
        for i, seg_len in enumerate(seg):
            if d <= acc + seg_len or i == len(seg) - 1:
                f = (d - acc) / seg_len if seg_len > 0 else 0.0
                f = min(1.0, max(0.0, f))
                a, b = ring[i], ring[i + 1]
                dense.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
                break
            acc += seg_len

    rng = random.Random()
    out = []
    for i in range(seconds):
        lat, lng = dense[int(i * (len(dense) - 1) / max(1, seconds - 1))]
        out.append((round(lat + rng.gauss(0, sigma), 6),
                    round(lng + rng.gauss(0, sigma), 6),
                    5.0, i))
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
    info = c.userinfo()
    if info.get("code") != 1:
        sys.exit(f"读取用户信息失败：{info.get('message') or info}")
    profile = info.get("data") or {}

    print("获取本次考核点位…")
    try:
        points, origin = parse_randrun(c.random_points(QUERY_LAT, QUERY_LNG))
    except ValueError as e:
        sys.exit(f"{e}（未到开放时段或会话已过期）")
    print("点位:", " / ".join(p["point_name"] for p in points))

    mileage = max(2000, int(a.mileage))
    wait = int(a.wait) if a.wait > 0 else plan_wait(profile, mileage)
    wait = max(360, wait)
    steps, pace, notes = plan_metrics(profile, mileage, wait)
    if notes:
        sys.exit("参数不合规：" + "；".join(notes) + f"。建议 --wait ≈ {plan_wait(profile, mileage)} 秒")
    print(f"指标 {mileage}m / {wait}s / {steps} 步 / 配速 {pace} min/km")

    t0 = datetime.datetime.now()
    rid = c.create_line(points)
    if not rid:
        sys.exit("createLine 失败，会话可能过期")
    print(f"[{t0:%H:%M:%S}] record_id={rid}  目标 {mileage}m / {wait}s")

    track = make_track(points, origin, mileage, wait)
    left = wait - (datetime.datetime.now() - t0).total_seconds()
    while left > 0:
        time.sleep(min(5, max(0.5, left)))
        left = wait - (datetime.datetime.now() - t0).total_seconds()
        print(f"\r  剩余 {int(left):>4}s", end="", flush=True)
    print()

    now = datetime.datetime.now()
    body = {"record_id": rid, "pace": pace,
            "running_time": wait, "mileage": mileage,
            "start_time": (t0 + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "pass_point": len(points), "step_count": steps}

    ck = c.post("/xcxapi/checkRecord/", {"a": enc(body)})
    print("checkRecord:", json.dumps(ck, ensure_ascii=False))
    if (ck.get("data") or {}).get("status") == 0:
        print("服务端预校验不通过，已中止（未落库）")
        return

    print("update:", json.dumps(c.post("/xcxapi/updateRecordNew/", {"a": enc(body)}), ensure_ascii=False))

    ok = 0
    total_batch = (len(track) + 49) // 50
    for i in range(0, len(track), 50):
        batch = [{"name": "", "lat": lat, "lng": lng, "accuracy": acc,
                  "timestamp": int((t0 + datetime.timedelta(seconds=off)).timestamp() * 1000)}
                 for (lat, lng, acc, off) in track[i:i + 50]]
        r = c.post("/xcxapi/uploadPathPointV3/",
                   {"img_url": enc({"record_id": rid, "path_point": batch, "path_image": ""})})
        if r.get("code") == 1:
            ok += 1
        else:
            print("  batch fail:", json.dumps(r, ensure_ascii=False))
            break
    print(f"track: {ok}/{total_batch} 批")

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
    rp.add_argument("--wait", type=int, default=0, help="时长秒；0 表示按服务端配速区间自动推算")
    rp.set_defaults(func=cmd_run)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
