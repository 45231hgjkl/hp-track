"""ecust_run.py — 华东理工健康跑 API 直连提交（本地研究用）

结论来自实测:
  * 服务端只校验 mileage / running_time / pass_point 三个数字, 不校验轨迹真假
  * running_time 被服务端按 "记录创建 -> 提交" 的真实间隔重算, 所以必须真等
  * pass_point 必须是必经点数量, 否则 checkRecord 直接判不合格
  * 最短组合: 2000m + 配速 200s/km = 400 秒

用法:
    python ecust_run.py --sid <X-Session-ID> --student-id 13101 --wait 400
"""
import argparse
import base64
import datetime
import json
import math
import os
import random
import time
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = b"A7fK92LmXyQw8vRp"
BASE = "https://run.ecust.edu.cn"
R = 6378137.0

# 奉贤校区三个必经点（randrunInfo 实测返回）
FENGXIAN_RING = [(30.830092, 121.504504), (30.831815, 121.501978),
                 (30.8286, 121.505815), (30.830092, 121.504504)]


def hav(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[1], a[0], b[1], b[0]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def enc(obj):
    iv = os.urandom(16)
    d = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    pad = 16 - len(d) % 16
    d += bytes([pad]) * pad
    e = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv).decode() + ":" + base64.b64encode(e.update(d) + e.finalize()).decode()


def make_track(ring, total_m, seconds, accuracy=(4.0, 9.0)):
    """沿闭合环生成 seconds 个点，带随机漂移。"""
    per = sum(hav(a, b) for a, b in zip(ring, ring[1:]))
    laps = total_m / per
    dense = []
    steps = len(ring) - 1
    for i in range(int(laps * 600) + 1):
        t = (i / 600) % 1.0
        seg, nxt = ring[int(t * steps)], ring[int(t * steps) + 1]
        f = t * steps - int(t * steps)
        dense.append((seg[0] + (nxt[0] - seg[0]) * f, seg[1] + (nxt[1] - seg[1]) * f))
    rng = random.Random()
    step = len(dense) / seconds
    out = []
    for i in range(seconds):
        p = dense[int(i * step) % len(dense)]
        out.append((round(p[0] + rng.gauss(0, 1.2e-5), 6),
                    round(p[1] + rng.gauss(0, 1.2e-5), 6),
                    round(rng.uniform(*accuracy), 1)))
    return out


class Client:
    def __init__(self, sid, student_id):
        self.h = {"X-Session-ID": sid, "Content-Type": "application/json",
                  "User-Agent": "MicroMessenger/7.0.20"}
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.student_id = student_id

    def _req(self, path, data=None, method="POST"):
        req = urllib.request.Request(BASE + path, data=data, headers=self.h, method=method)
        with self.op.open(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def post(self, path, obj):
        return self._req(path, json.dumps(obj).encode())

    def get(self, path):
        return self._req(path, method="GET")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", required=True)
    ap.add_argument("--student-id", type=int, default=13101)
    ap.add_argument("--mileage", type=int, default=2000)
    ap.add_argument("--wait", type=int, default=400)
    ap.add_argument("--pass-points", type=int, default=3)
    ap.add_argument("--step-count", type=int, default=400)
    a = ap.parse_args()

    c = Client(a.sid, a.student_id)

    pts_meta = [{"lat": str(la), "lng": str(lo), "point_name": n}
                for (la, lo), n in zip(FENGXIAN_RING[:3], ("图书馆北", "信息楼南", "南大门"))]
    t0 = datetime.datetime.now()
    r = c.post("/xcxapi/createLine/", {"student_id": a.student_id, "pass_point": pts_meta})
    rid = r["data"]["record_id"]
    print(f"[{t0:%H:%M:%S}] createLine -> record_id={rid}", flush=True)

    track = make_track(FENGXIAN_RING, a.mileage, a.wait)
    print(f"track: {len(track)} 点, 目标 {a.mileage}m / {a.wait}s", flush=True)

    while (datetime.datetime.now() - t0).total_seconds() < a.wait:
        time.sleep(2)

    now = datetime.datetime.now()
    body = {"record_id": rid, "pace": round(a.wait / (a.mileage / 1000)),
            "running_time": a.wait, "mileage": a.mileage,
            "start_time": (t0 + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "pass_point": a.pass_points, "step_count": a.step_count}

    print(f"[{now:%H:%M:%S}] check:", json.dumps(c.post("/xcxapi/checkRecord/", {"a": enc(body)}),
                                                ensure_ascii=False), flush=True)
    print("update:", json.dumps(c.post("/xcxapi/updateRecordNew/", {"a": enc(body)}),
                                ensure_ascii=False), flush=True)

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
            print("  batch fail:", json.dumps(r, ensure_ascii=False), flush=True)
            break
    print(f"track batches: {ok}/{(len(track) + 49) // 50}", flush=True)

    d = c.get(f"/xcxapi/recordInfo/?id={rid}")["data"]
    print("RESULT:", json.dumps({k: d.get(k) for k in
          ("id", "mileage", "running_time", "pace", "speed", "step_frequency",
           "start_time", "end_time", "status", "warning")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
