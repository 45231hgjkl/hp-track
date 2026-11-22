#!/usr/bin/env python3
"""school.py — 校园跑 CLI（本地研究）

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

记录状态机（run_state.json，每次网络副作用前落盘）:
  create_pending → created → checking → finish_pending → record_updated → finished
  异常落到 create_unknown / finish_unknown / check_failed：这两种「不确定」不自动重试，
  必须到小程序核实后跑 abandon 收尾，避免在服务器上留下两条记录。

用法:
  python school.py login --phone 13800000000                 # 交互输入密码
  python school.py login --phone 13800000000 --password xxx
  python school.py login --phone 13800000000 --sms           # 短信验证码登录
  python school.py whoami
  python school.py history
  python school.py run --mileage 2000 --wait 400

会话缓存在脚本同目录 session.json，过期自动提示重新登录。
"""
import argparse
import datetime
import getpass
import itertools
import json
import math
import os
import platform
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

import pure_aes

# 服务端主机：分片段拼装，避免仓库里出现完整可检索的校名串
_BASE_PARTS = ("https://run.", "ecu", "st.edu.cn")
BASE = "".join(_BASE_PARTS)
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
RUN_STATE_FILE = os.path.join(data_dir(), "run_state.json")

# 记录状态机。终态之外都算「服务器上可能已经有这条记录」，不能随手新建。
RUN_TERMINAL = ("finished", "discarded")
RUN_UNRESOLVED = ("create_pending", "create_unknown", "created", "checking",
                  "check_failed", "finish_pending", "record_updated", "finish_unknown")

# randrunInfo 的查询坐标：只用来告诉服务端「我在校区」，不参与点位构造。
# 考核点位一律以服务端当次返回的 data 为准（见 parse_randrun）。
QUERY_LAT, QUERY_LNG = 30.830092, 121.504504

# 服务端未给配速区间时的保守取值（分钟/公里），以及步频的默认中位值。
PACE_FALLBACK = 5.5
FREQ_FALLBACK = 172.0

# Header 只放这些键；值是本地会话文件读回来的，必须过一遍净化。
HEADER_KEYS = ("X-Session-ID", "Authorization")
HEADER_VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=\-]{1,512}$")

# 校区边界（WGS84，OSM 边界数据，ODbL 1.0）。仅用于「点位是否在校区内」的提示，
# 换校区时改这里或直接关掉校验（CLI --no-area / WebUI strict_area=false）。
CAMPUS_BOUNDARY = (
    (30.833463, 121.4919501), (30.8335415, 121.4920781), (30.8336969, 121.4923639),
    (30.8341438, 121.4922949), (30.8342605, 121.4923117), (30.8343413, 121.4923458),
    (30.8344155, 121.4923854), (30.8384256, 121.5001315), (30.8391466, 121.5015652),
    (30.8329235, 121.5073882), (30.8321253, 121.5060542), (30.8276723, 121.4976667),
    (30.8278105, 121.4971228), (30.8314375, 121.4937366),
)


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
class School:
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
            code, hint = classify_error(e)
            return {"code": -98, "message": hint, "error_code": code}

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


PHONE_TEXT_RE = re.compile(r"\b1[3-9]\d{9}\b")
TOKEN_TEXT_RE = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")


def redact(text) -> str:
    """日志脱敏：手机号打码，长 token（sid 形态）整段抹掉。

    日志会被用户复制到聊天里求助，服务端返回体里可能带 session_key 回显。
    """
    s = PHONE_TEXT_RE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], str(text))
    return TOKEN_TEXT_RE.sub("<redacted>", s)


# ---------------- 记录状态机 ----------------
def slot_of(when=None) -> str:
    """时间槽：精确到分钟，用来防止同一分钟重复创建记录。"""
    return (when or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M")


def load_run_state() -> dict:
    if os.path.exists(RUN_STATE_FILE):
        try:
            state = json.load(open(RUN_STATE_FILE, encoding="utf-8"))
            if isinstance(state, dict):
                return state
        except Exception:
            pass
    return {}


def save_run_state(state: dict):
    """原子写：先写 .tmp 再 rename，进程被杀也不会留下半个 json。"""
    tmp = RUN_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RUN_STATE_FILE)


def clear_run_state():
    for path in (RUN_STATE_FILE, RUN_STATE_FILE + ".tmp"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def run_unresolved(state: dict) -> bool:
    """还有没走完的记录？这类状态一律不许自动再建一条。"""
    return bool(state) and state.get("status") in RUN_UNRESOLVED


def run_resumable(state: dict) -> bool:
    """能不能就着原记录继续提交。

    只有 created / check_failed 且已经有 record_id、start_ms 才算「可以续」；
    create_unknown / finish_unknown 的创建或提交结果本身不确定，ECS 同样不允许续。
    """
    if not state:
        return False
    return (state.get("status") in ("created", "check_failed")
            and bool(state.get("record_id")) and bool(state.get("start_ms")))


def _now_ms() -> int:
    return int(time.time() * 1000)


def run_moving_ms(state: dict, now_ms=None) -> int:
    """已跑时长（毫秒）：end - start - 暂停区间。

    与 ECU 的 RunClock.movingMillis 同义——用真实流逝时间判断「跑够没有」，
    不依赖本地记了多少秒这种易错量。
    """
    now_ms = _now_ms() if now_ms is None else int(now_ms)
    start = int(state.get("start_ms") or 0)
    if start <= 0:
        return 0
    end = int(state.get("submission_end_ms") or now_ms)
    paused = 0
    for item in state.get("pauses") or []:
        try:
            from_ms = max(start, int(item.get("start_ms") or 0))
            to_ms = min(end, int(item.get("end_ms") or end))
        except (TypeError, ValueError):
            continue
        paused += max(0, to_ms - from_ms)
    return max(0, end - start - paused)


def run_remaining_ms(state: dict, now_ms=None) -> int:
    duration = int(state.get("duration_ms") or 0)
    if duration <= 0:
        return 0
    return max(0, duration - run_moving_ms(state, now_ms))


def run_end_ms(state: dict, now_ms=None) -> int:
    """本次提交用的 end：已经钉住过就复用，保证重放时 start/end 不漂移。"""
    pinned = state.get("submission_end_ms")
    if pinned:
        return int(pinned)
    return _now_ms() if now_ms is None else int(now_ms)


def _fmt_ms(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


def submit_payload(state: dict) -> dict:
    """由本地状态重建提交体。

    续跑与首次提交走同一个函数：时间全部从 start_ms / end_ms 推导，
    不再用「当前时刻减起跑时刻」这种重算方式。
    """
    record_id = state.get("record_id")
    if not record_id:
        raise ValueError("本地记录缺少 record_id，无法提交")
    start_ms = int(state.get("start_ms") or 0)
    if start_ms <= 0:
        raise ValueError("本地记录缺少 start_ms，无法提交")
    distance = int(state.get("distance") or 0)
    if distance <= 0:
        raise ValueError("本地记录缺少里程，无法提交")
    end_ms = run_end_ms(state)
    seconds = max(1, (end_ms - start_ms) // 1000)
    geo = state.get("geo") or {}
    points = geo.get("points") or []
    return {
        "record_id": record_id,
        "pace": round(seconds / 60.0 / (distance / 1000.0), 2),
        "running_time": seconds,
        "mileage": distance,
        "start_time": _fmt_ms(start_ms + 1000),      # 与首版保持一致：起跑后 1 秒
        "end_time": _fmt_ms(end_ms),
        "pass_point": len(points) or 3,
        "step_count": int(state.get("steps") or 1),
    }


def check_verdict(ck) -> tuple:
    """解析 checkRecord：返回 (kind, reason, data)。

    kind: ok / invalid / unknown
      ok      status==1，可以继续提交
      invalid status==0，服务端明确判不合格
      unknown 结构不认识，不能猜
    """
    data = (ck or {}).get("data") if isinstance(ck, dict) else None
    status = data.get("status") if isinstance(data, dict) else None
    reason = ""
    if isinstance(ck, dict):
        reason = str((data or {}).get("message") or ck.get("message") or "")[:200]
    if status == 1:
        return "ok", reason, data
    if status == 0:
        return "invalid", reason or "服务端判为不合格", data
    return "unknown", reason or "校验状态无法识别", data


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


def client_from_session() -> School:
    s = load_session()
    if not s.get("sid"):
        sys.exit("没有会话，先跑: python school.py login --phone <手机号>")
    return School(s.get("sid"), s.get("uid"))


# ---------------- 坐标系与校区 ----------------
def wgs_to_gcj(lat: float, lng: float):
    """WGS84 → GCJ-02 正算。接口点位是 GCJ-02，边界数据是 WGS84，比对前统一到一边。"""
    if not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lat, lng
    x, y = lng - 105.0, lat - 35.0
    pi = math.pi
    dlat = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    dlat += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
    dlat += (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
    dlat += (160.0 * math.sin(y / 12.0 * pi) + 320.0 * math.sin(y * pi / 30.0)) * 2.0 / 3.0
    dlng = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    dlng += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
    dlng += (20.0 * math.sin(x * pi) + 40.0 * math.sin(x / 3.0 * pi)) * 2.0 / 3.0
    dlng += (150.0 * math.sin(x / 12.0 * pi) + 300.0 * math.sin(x / 30.0 * pi)) * 2.0 / 3.0
    rad = lat * pi / 180.0
    magic = 1 - 0.00669342162296594323 * math.sin(rad) ** 2
    sq = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((6378245.0 * (1 - 0.00669342162296594323)) / (magic * sq) * pi)
    dlng = (dlng * 180.0) / (6378245.0 / sq * math.cos(rad) * pi)
    return lat + dlat, lng + dlng


def in_polygon(lat: float, lng: float, poly) -> bool:
    """射线法。poly 为 [(lat, lng), ...]，首尾不必重复。"""
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        y1, x1 = poly[i]
        y2, x2 = poly[j]
        if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
        j = i
    return inside


_CAMPUS_GCJ = None


def campus_polygon_gcj():
    """边界常量是 WGS84，转成 GCJ-02 缓存一份供比对。"""
    global _CAMPUS_GCJ
    if _CAMPUS_GCJ is None:
        _CAMPUS_GCJ = tuple(wgs_to_gcj(lat, lng) for lat, lng in CAMPUS_BOUNDARY)
    return _CAMPUS_GCJ


def check_area(points, origin):
    """点位与起点是否落在已知校区边界内。返回 (inside_count, total, outside_names)。"""
    poly = campus_polygon_gcj()
    outside = []
    total = 1
    inside = 0
    pairs = [("起点", origin)] + [(p.get("point_name") or "考核点", p) for p in points]
    total = len(pairs)
    for name, item in pairs:
        lat, lng = _num(item.get("lat")), _num(item.get("lng"))
        if lat is None or lng is None:
            outside.append(name)
            continue
        if in_polygon(lat, lng, poly):
            inside += 1
        else:
            outside.append(name)
    return inside, total, outside


# ---------------- 轨迹 ----------------
def hav(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[1], a[0], b[1], b[0]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def ring_from(points, origin, order=None):
    """把「起点 + 三点 + 回起点」拼成闭环折线。order 决定三点的访问顺序。"""
    idx = list(order) if order is not None else list(range(len(points)))
    ring = [(float(origin["lat"]), float(origin["lng"]))]
    ring += [(float(points[i]["lat"]), float(points[i]["lng"])) for i in idx]
    ring.append(ring[0])
    return ring


def ring_length(ring):
    return sum(hav(a, b) for a, b in zip(ring, ring[1:]))


def best_ring(points, origin):
    """三种点位的 6 种访问顺序里选闭环最短的一条。

    只影响怎么跑，不改变提交给服务端的 pass_point 数组 —— 顺序和点位本身
    都是服务端给的，本地只负责「用最少的绕路把这三个点串起来」。
    """
    if not points:
        raise ValueError("缺少考核点位")
    best = None
    for order in itertools.permutations(range(len(points))):
        ring = ring_from(points, origin, order)
        length = ring_length(ring)
        if length <= 0:
            continue
        if best is None or length < best[0]:
            best = (length, order, ring)
    if best is None:
        raise ValueError("点位重合，无法生成轨迹")
    return best


def make_track(points, origin, total_m, seconds, sigma=1.2e-5, seed=None):
    """沿最短闭环铺满 total_m 里程，按速度曲线分配每秒的位置。

    返回 [(lat, lng, accuracy, offset_seconds), ...]。offset 是相对起跑的秒数，
    上传时用它算 timestamp。

    与匀速版的区别：位置按 v(s) = 1 + 0.08·sin(s/90+φ) + 0.04·cos(s/210+φ)
    推进，所以每秒位移不均匀（有快慢起伏），而不是「每秒走固定米数」。
    """
    if not points:
        raise ValueError("缺少考核点位")
    _, _, ring = best_ring(points, origin)
    seg = [hav(a, b) for a, b in zip(ring, ring[1:])]
    per = sum(seg)
    if per <= 0:
        raise ValueError("点位重合，无法生成轨迹")

    total_m = max(float(total_m), 1.0)
    seconds = max(1, int(seconds))
    rng = random.Random(seed)
    phase = rng.random() * 2 * math.pi

    step_m = 0.5
    count = max(2, int(total_m / step_m) + 1)
    ds = total_m / (count - 1)
    pts, weights = [], [0.0]
    for k in range(count):
        s = ds * k
        d, acc = s % per, 0.0
        for i, seg_len in enumerate(seg):
            if d <= acc + seg_len or i == len(seg) - 1:
                f = (d - acc) / seg_len if seg_len > 0 else 0.0
                f = min(1.0, max(0.0, f))
                a, b = ring[i], ring[i + 1]
                pts.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
                break
            acc += seg_len
        if k:
            speed = 1.0 + 0.08 * math.sin(s / 90.0 + phase) + 0.04 * math.cos(s / 210.0 + phase)
            weights.append(weights[-1] + ds / max(speed, 0.5))

    span = weights[-1] or 1.0
    weights = [w / span for w in weights]      # 相对时间轴 0..1
    n = len(pts)
    denom = max(1, seconds - 1)
    out = []
    j = 0
    for sec in range(seconds):
        target = sec / denom
        while j < n - 2 and weights[j + 1] < target:
            j += 1
        t0, t1 = weights[j], weights[j + 1]
        f = 0.0 if t1 <= t0 else (target - t0) / (t1 - t0)
        f = min(1.0, max(0.0, f))
        lat = pts[j][0] + (pts[j + 1][0] - pts[j][0]) * f
        lng = pts[j][1] + (pts[j + 1][1] - pts[j][1]) * f
        out.append((round(lat + rng.gauss(0, sigma), 6),
                    round(lng + rng.gauss(0, sigma), 6),
                    5.0, sec))
    return out


# ---------------- 命令 ----------------
def cmd_login(a):
    c = School()
    phone = a.phone
    if a.sms:
        print("发送验证码...", c.send_sms(phone))
        code = input("短信验证码: ").strip()
        r = c.login_sms(phone, code)
    else:
        pwd = a.password or os.environ.get("HP_TRACK_PASSWORD") or getpass.getpass("密码: ")
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


def cmd_abandon(a):
    """把本地未收尾的记录标记为停止跟踪；服务器上的记录不会被动。"""
    state = load_run_state()
    if not state:
        print("没有本地记录状态，无需处理")
        return
    if state.get("status") in RUN_TERMINAL:
        print(f"本地状态已是 {state.get('status')}，无需处理")
        return
    state.update(status="discarded", previous_status=state.get("status"),
                 discarded_at=datetime.datetime.now().isoformat(timespec="seconds"))
    save_run_state(state)
    print(f"已停止跟踪 record_id={state.get('record_id')}（原状态 {state['previous_status']}）")
    print("服务器上的记录仍然存在，需要处理请到小程序里操作")


def cmd_run(a):
    prev = load_run_state()
    if run_unresolved(prev) and not a.force:
        sys.exit(f"上次记录未收尾：status={prev.get('status')} record_id={prev.get('record_id')} "
                 f"slot={prev.get('slot')}。先到小程序核实；确认无需处理后再用 abandon 收尾，"
                 f"或用 --force 强跑（会在服务器上多建一条）")
    if run_unresolved(prev) and a.force:
        print(f"警告：忽略未收尾记录 status={prev.get('status')} record_id={prev.get('record_id')}")

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

    if not a.no_area:
        inside, total, outside = check_area(points, origin)
        where = "在校区边界内" if inside == total else "不在已知校区边界内"
        if inside < total:
            msg = f"{total - inside}/{total} 个点位{where}（{'、'.join(outside)}）"
            if a.strict_area:
                sys.exit(f"{msg}；已知边界只覆盖校区，换校区请加 --no-area")
            print(f"提示：{msg}；可能不是校区，继续按服务端点位跑（--no-area 可关掉检查）")
        else:
            print(f"点位校验：{inside}/{total} 在校区边界内")

    mileage = max(2000, int(a.mileage))
    wait = int(a.wait) if a.wait > 0 else plan_wait(profile, mileage)
    wait = max(360, wait)
    steps, pace, notes = plan_metrics(profile, mileage, wait)
    if notes:
        sys.exit("参数不合规：" + "；".join(notes) + f"。建议 --wait ≈ {plan_wait(profile, mileage)} 秒")
    print(f"指标 {mileage}m / {wait}s / {steps} 步 / 配速 {pace} min/km")

    t0 = datetime.datetime.now()
    start_ms = int(t0.timestamp() * 1000)
    state = {"slot": slot_of(t0), "mileage": mileage, "distance": mileage,
             "wait": wait, "duration_ms": wait * 1000, "start_ms": start_ms,
             "steps": steps, "pace": pace,
             "start_time": t0.isoformat(timespec="seconds"),
             "points": [p["point_name"] for p in points],
             "geo": {"origin": origin, "points": points},
             "student_id": profile.get("id"),
             "track_seed": random.randrange(1 << 30),
             "submission_end_ms": None,
             "record_id": None, "status": "create_pending"}
    save_run_state(state)                      # 发请求之前先落盘：失败也知道自己发过

    try:
        rid = c.create_line(points)
    except BaseException:
        state.update(status="create_unknown", error="createLine 未完成")
        save_run_state(state)
        raise
    if not rid:
        state.update(status="create_unknown", error="createLine 未返回 record_id")
        save_run_state(state)
        sys.exit("createLine 未返回 record_id，本地已标记 create_unknown；先到小程序核实再决定是否重跑")
    state.update(status="created", record_id=rid)
    save_run_state(state)
    print(f"[{t0:%H:%M:%S}] record_id={rid}  目标 {mileage}m / {wait}s")

    while run_remaining_ms(state) > 0:
        left = run_remaining_ms(state) / 1000.0
        time.sleep(min(5, max(0.5, left)))
        print(f"\r  剩余 {math.ceil(run_remaining_ms(state) / 1000):>4}s", end="", flush=True)
    print()

    try:
        submit_record(c.post, state, profile, accept_invalid=a.accept_invalid, log=print)
    except ValueError as e:                     # 业务性中止，状态已落盘
        print(f"中止：{e}")
        return
    except BaseException as e:                  # 网络/会话中断，按阶段落状态
        mark_failure(state, e)
        raise

    d = c.record_info(state["record_id"]).get("data", {})
    print("RESULT:", json.dumps({k: d.get(k) for k in
          ("id", "mileage", "running_time", "pace", "step_frequency", "status", "warning")},
          ensure_ascii=False))


def cmd_resume(a):
    """续跑上次没提交完的记录：不新建、不重新计时，按真实时间差判断是否跑够。"""
    state = load_run_state()
    if not state:
        sys.exit("没有本地记录状态，无需续跑")
    if state.get("status") in RUN_TERMINAL:
        sys.exit(f"这条记录已是终态（{state.get('status')}），无需续跑")
    if not run_resumable(state):
        sys.exit(f"当前状态 {state.get('status')} 不能续跑：只有 created / check_failed "
                 f"且已拿到 record_id 的记录可以继续；create_unknown / finish_unknown 请到小程序核实")

    remaining = run_remaining_ms(state)
    if remaining > 0:
        if not a.wait:
            sys.exit(f"还差 {math.ceil(remaining / 1000)} 秒才够时长；"
                     f"加 --wait 等到点自动续，或到小程序核实后跑 abandon")
        print(f"这条记录还没跑够，等待 {math.ceil(remaining / 1000)} 秒…")
        while run_remaining_ms(state) > 0:
            time.sleep(min(5, max(0.5, run_remaining_ms(state) / 1000.0)))
            print(f"\r  剩余 {math.ceil(run_remaining_ms(state) / 1000):>4}s", end="", flush=True)
        print()

    c = client_from_session()
    info = c.userinfo()
    if info.get("code") != 1:
        sys.exit(f"读取用户信息失败：{info.get('message') or info}")
    profile = info.get("data") or {}
    if str(profile.get("id")) != str(state.get("student_id")):
        sys.exit("当前登录账号与这条记录不一致，不能用它续跑（换回原账号，或到小程序处理这条记录）")

    print(f"续跑 record_id={state.get('record_id')} slot={state.get('slot')} "
          f"status={state.get('status')} 已跑 {run_moving_ms(state) // 1000}s")
    try:
        submit_record(c.post, state, profile, accept_invalid=a.accept_invalid, log=print)
    except ValueError as e:
        print(f"中止：{e}")
        return
    except BaseException as e:
        mark_failure(state, e)
        raise

    d = c.record_info(state["record_id"]).get("data", {})
    print("RESULT:", json.dumps({k: d.get(k) for k in
          ("id", "mileage", "running_time", "pace", "step_frequency", "status", "warning")},
          ensure_ascii=False))


def mark_failure(state: dict, err) -> str:
    """按当前阶段把中断落成「可续」或「不可续」。

    checking 阶段只是没拿到校验结果 → 退回 created，重新登录后还能续；
    finish_pending / record_updated 阶段 updateRecordNew 可能已经落库 → finish_unknown，不再重试。
    """
    text = redact(str(err))[:300]
    status = state.get("status")
    if status == "checking":
        state.update(status="created", error=text)
        save_run_state(state)
        return "created"
    if status in ("finish_pending", "record_updated"):
        state.update(status="finish_unknown", error=text)
        save_run_state(state)
        return "finish_unknown"
    return status or ""


def submit_record(post, state: dict, profile: dict, accept_invalid: bool = False, log=None) -> dict:
    """把一条已创建的记录提交完——首次提交与续跑共用这一条路径。

    post(path, body) 由调用方注入（CLI 直连，WebUI 走 secure 包装），
    这样状态机、时间口径、payload 只有一份实现。
    """
    log = log or (lambda msg: None)
    if not state.get("record_id"):
        raise ValueError("本地记录缺少 record_id")
    distance = int(state.get("distance") or 0)
    if distance <= 0:
        raise ValueError("本地记录缺少里程")

    remaining = run_remaining_ms(state)
    if remaining > 0:
        raise ValueError(f"还不能提交：还差 {math.ceil(remaining / 1000)} 秒")

    end_ms = run_end_ms(state)
    state["submission_end_ms"] = end_ms
    save_run_state(state)                       # 先钉住 end，重试时 start/end 不再漂移

    body = submit_payload(state)
    steps, pace, notes = plan_metrics(profile, distance, body["running_time"])
    if notes:
        raise ValueError("这条记录已无法合规提交：" + "；".join(notes))
    body["step_count"], body["pace"] = steps, pace
    log(f"提交 {distance}m / {body['running_time']}s / {steps} 步 / 配速 {pace} min/km")

    state["status"] = "checking"
    save_run_state(state)
    ck = post("/xcxapi/checkRecord/", {"a": enc(body)})
    kind, reason, _ = check_verdict(ck)
    log(f"checkRecord → {kind}" + (f"（{reason}）" if reason else ""))
    if kind == "unknown":
        state.update(status="check_failed", reason=reason)
        save_run_state(state)
        raise ValueError(f"校验状态无法识别：{reason}")
    if kind == "invalid" and not accept_invalid:
        state.update(status="check_failed", reason=reason)
        save_run_state(state)
        raise ValueError(f"服务端预校验不通过：{reason}")
    if kind == "invalid":
        log("服务端判为不合格，按确认继续提交")

    state["status"] = "finish_pending"
    save_run_state(state)
    up = post("/xcxapi/updateRecordNew/", {"a": enc(body)})
    if (up or {}).get("code") != 1:
        message = str((up or {}).get("message") or "")[:200]
        state.update(status="finish_unknown", error=message or "updateRecordNew 未返回成功")
        save_run_state(state)
        raise ValueError(f"updateRecordNew 未返回成功：{message or '未知'}")
    state["status"] = "record_updated"
    save_run_state(state)
    log("updateRecordNew → ok")

    geo = state.get("geo") or {}
    points = geo.get("points") or []
    if not points:
        raise ValueError("本地记录缺少点位，无法重建轨迹")
    start_ms = int(state["start_ms"])
    track = make_track(points, geo.get("origin") or points[0], distance,
                       body["running_time"], seed=state.get("track_seed"))
    total_batch = (len(track) + 49) // 50
    done = 0
    for i in range(0, len(track), 50):
        batch = [{"name": "", "lat": lat, "lng": lng, "accuracy": acc,
                  "timestamp": start_ms + off * 1000}
                 for (lat, lng, acc, off) in track[i:i + 50]]
        r = post("/xcxapi/uploadPathPointV3/",
                 {"img_url": enc({"record_id": state["record_id"],
                                  "path_point": batch, "path_image": ""})})
        if (r or {}).get("code") != 1:
            state.update(status="finish_unknown", error="轨迹上传被拒",
                         uploaded_points=min(i, len(track)))
            save_run_state(state)
            raise ValueError(f"轨迹上传失败：{(r or {}).get('message')}")
        done += 1
        state["uploaded_points"] = min(i + 50, len(track))
        save_run_state(state)
        log(f"轨迹批次 {done}/{total_batch}")

    state.update(status="finished", uploaded_points=len(track))
    save_run_state(state)
    return body


# ---------------- 错误分类 ----------------
def classify_error(exc) -> tuple:
    """把网络/会话异常归类成固定码 + 可操作提示。

    返回 (code, hint)。code 进日志与诊断报告，hint 给用户看；
    永远不回显原始异常文本（里面可能有代理/证书细节或响应体）。
    """
    if isinstance(exc, ssl.SSLCertVerificationError):
        return ("cert_invalid",
                "服务器证书校验失败：先确认设备日期时间正确，再关掉抓包代理或 VPN 重试；"
                "仍失败请更新系统根证书。账号会话未因此清除。")
    if isinstance(exc, ssl.SSLError):
        return ("tls_failed",
                "无法完成 HTTPS 握手：关掉抓包代理或 VPN，换网络重试；仍失败请更新系统。"
                "账号会话未因此清除。")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ("network_timeout", "连接服务器超时：检查网络后重试，账号会话未因此清除。")
    if isinstance(exc, socket.gaierror):
        return ("dns_failed", "域名解析失败：检查网络或 DNS 设置后重试。")
    if isinstance(exc, ConnectionRefusedError):
        return ("connection_refused", "服务器拒绝连接：可能正在维护，稍后重试。")
    if isinstance(exc, urllib.error.HTTPError):
        return (f"http_{exc.code}", f"服务器返回 HTTP {exc.code}：稍后重试；持续出现请导出诊断报告。")
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
            return classify_error(reason)
        if isinstance(reason, BaseException):
            return classify_error(reason)
        text = str(reason)
        if "certificate" in text.lower():
            return ("cert_invalid", "服务器证书校验失败：检查设备时间、关闭抓包代理后重试。")
        return ("network_unreachable", f"网络不可达（{text[:80]}）：检查网络后重试。")
    if isinstance(exc, OSError):
        return ("network_unreachable", f"网络异常（{type(exc).__name__}）：检查网络后重试。")
    return ("unknown", f"未归类的错误（{type(exc).__name__}）：导出诊断报告后再看。")


def runtime_kind() -> str:
    if FROZEN:
        return "exe（PyInstaller onefile）"
    if ZIPAPP:
        return "pyz（zipapp）"
    return "源码"


# ---------------- 诊断报告 ----------------
def diagnose(session: dict = None, run_state: dict = None, log=None, extra: dict = None) -> str:
    """生成可粘贴的纯文本诊断报告。

    只放「排查需要、且不含凭据」的信息：会话只报有无与打码手机号，
    记录只报状态与时间，坐标一律不出现（只报点位数）。
    """
    session = load_session() if session is None else session
    run_state = load_run_state() if run_state is None else run_state
    extra = extra or {}
    now = datetime.datetime.now()

    lines = ["hp-track 诊断报告",
             f"生成时间：{now:%Y-%m-%d %H:%M:%S}（本机时区）",
             f"运行形态：{runtime_kind()}",
             f"Python：{platform.python_version()} · {platform.platform()}",
             f"数据目录：{data_dir()}",
             f"监听端口：{extra.get('port') or 8737}",
             f"环境变量：HP_TRACK_HOME={'已设置' if os.environ.get('HP_TRACK_HOME') else '未设置'}"
             f" / HP_TRACK_INDEX={'已设置' if os.environ.get('HP_TRACK_INDEX') else '未设置'}"]

    lines.append("")
    lines.append("会话")
    if session.get("sid"):
        lines.append(f"  缓存：有（{mask_phone(session.get('phone'))}，保存于 {session.get('saved_at') or '未知'}）")
    else:
        lines.append("  缓存：无")
    lines.append(f"  内存：{'已登录' if extra.get('user') else '未登录'}")
    expired = extra.get("expired")
    lines.append(f"  服务端校验：{'已过期（code=-2）' if expired else '未报告过期'}")

    user = extra.get("user") or {}
    if user:
        lines.append("")
        lines.append("账号指标")
        pr = pace_range(user)
        fr = freq_range(user)
        lines.append(f"  shortest_distance={user.get('shortest_distance')}"
                     f"  min_pace={user.get('min_pace')}  max_pace={user.get('max_pace')}")
        lines.append(f"  min_step_frequency={user.get('min_step_frequency')}"
                     f"  max_step_frequency={user.get('max_step_frequency')}")
        lines.append(f"  配速区间解析：{'–'.join(f'{v:.2f}' for v in pr) + ' min/km' if pr else '缺失，按默认 5.5'}"
                     f" / 步频区间：{'–'.join(f'{v:.0f}' for v in fr) if fr else '缺失'}")

    lines.append("")
    lines.append("上次记录")
    if not run_state:
        lines.append("  无本地记录状态")
    else:
        lines.append(f"  status={run_state.get('status')}  record_id={run_state.get('record_id')}"
                     f"  slot={run_state.get('slot')}")
        lines.append(f"  里程={run_state.get('distance') or run_state.get('mileage')}"
                     f"  计划时长={(run_state.get('duration_ms') or 0) // 1000}s"
                     f"  已跑={run_moving_ms(run_state) // 1000}s"
                     f"  剩余={run_remaining_ms(run_state) // 1000}s"
                     f"  已传点={run_state.get('uploaded_points') or 0}")
        lines.append(f"  可续跑：{'是' if run_resumable(run_state) else '否'}"
                     f"  未收尾：{'是' if run_unresolved(run_state) else '否'}")
        reason = run_state.get("reason") or run_state.get("error")
        lines.append(f"  失败原因：{redact(reason) if reason else '（无）'}")
        if run_state.get("submission_end_ms"):
            lines.append(f"  submission_end_ms={run_state.get('submission_end_ms')}（已钉住）")

    lines.append("")
    lines.append("检查项")
    lines.append(f"  会话文件：{'存在' if os.path.exists(SESSION_FILE) else '不存在'}")
    lines.append(f"  记录状态文件：{'存在' if os.path.exists(RUN_STATE_FILE) else '不存在'}")
    writable = "否"
    try:
        probe = os.path.join(data_dir(), ".diag-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        writable = "是"
    except OSError:
        pass
    lines.append(f"  数据目录可写：{writable}")
    lines.append(f"  当前阶段：{extra.get('phase') or 'idle'}  任务运行中：{'是' if extra.get('running') else '否'}")
    if extra.get("last_error"):
        lines.append(f"  最近一次任务错误：{redact(extra['last_error'])[:200]}")

    entries = list(log or [])[-30:]
    lines.append("")
    lines.append(f"最近事件（最多 30 条，共 {len(log or [])} 条）")
    if entries:
        lines.extend(f"  {redact(line)}" for line in entries)
    else:
        lines.append("  （无）")

    lines.append("")
    lines.append("报告不含会话串、密码、手机号原文、坐标或请求正文。")
    return "\n".join(lines) + "\n"


def cmd_diagnose(a):
    text = diagnose()
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"诊断报告已写入 {os.path.abspath(a.out)}")
    else:
        print(text, end="")


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

    dg = sub.add_parser("diagnose", help="导出可粘贴的诊断报告（不含会话与坐标）")
    dg.add_argument("--out", help="写入文件；省略则打印到标准输出")
    dg.set_defaults(func=cmd_diagnose)

    hp = sub.add_parser("history")
    hp.add_argument("--page", type=int, default=1)
    hp.add_argument("--size", type=int, default=20)
    hp.set_defaults(func=cmd_history)

    rp = sub.add_parser("run")
    rp.add_argument("--mileage", type=int, default=2000)
    rp.add_argument("--wait", type=int, default=0, help="时长秒；0 表示按服务端配速区间自动推算")
    rp.add_argument("--force", action="store_true", help="忽略未收尾的本地记录（会在服务器上多建一条）")
    rp.add_argument("--accept-invalid", action="store_true", help="接受服务端明确判不合格的结果，继续提交")
    rp.add_argument("--strict-area", action="store_true", help="点位不在已知校区边界内就中止")
    rp.add_argument("--no-area", action="store_true", help="跳过校区边界检查（换校区时用）")
    rp.set_defaults(func=cmd_run)

    sub.add_parser("abandon", help="停止跟踪未收尾的记录（不删除服务器上的记录）").set_defaults(func=cmd_abandon)

    rsm = sub.add_parser("resume", help="续跑上次没提交完的记录（不新建、不重新计时）")
    rsm.add_argument("--wait", action="store_true", help="还没到时长就等到点再提交")
    rsm.add_argument("--accept-invalid", action="store_true", help="接受服务端明确判不合格的结果，继续提交")
    rsm.set_defaults(func=cmd_resume)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
