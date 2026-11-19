#!/usr/bin/env python3
"""生成"像真人跑出来"的 GPS 轨迹，输出 GPX（可被 iOS / Android 定位模拟直接播放）。

用法:
  # 操场跑圈 2.4km，配速 6'00"/km，从 18:30:00 开始
  python gen_run_track.py loop --center 30.2741,120.1551 --length 2400 --pace 360 \
      --start 2026-11-19T18:30:00 --out run.gpx

  # 沿真实道路（BRouter 拉步行路线）跑一段
  python gen_run_track.py route --from 30.2741,120.1551 --to 30.2810,120.1620 \
      --pace 340 --jitter 3.0 --out road.gpx

说明:
  - 输出 1 点/秒，时间戳决定播放速度，是防"配速异常"检测的核心。
  - 配速曲线不是恒定值: 起步慢 -> 中段波动 -> 收尾冲一下, 分段随机再归一化。
  - 横向 jitter 模拟 GPS 漂移; 别调太大, 5m 以上会飘进建筑/河里露馅。
"""
import argparse
import datetime as dt
import math
import random
import urllib.parse
import urllib.request

R = 6378137.0


def _rad(d):
    return d * math.pi / 180.0


def hav(a, b):
    """(lon, lat) 两点间球面距离, 米。"""
    lon1, lat1 = a
    lon2, lat2 = b
    dlat = _rad(lat2 - lat1)
    dlon = _rad(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(_rad(lat1)) * math.cos(_rad(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def densify(pts, step=4.0):
    """线性补点, 保证相邻点 <= step 米。经纬度小范围内线性插值误差可忽略。"""
    out = [pts[0]]
    for p, q in zip(pts, pts[1:]):
        n = max(1, int(hav(p, q) / step))
        for i in range(1, n + 1):
            t = i / n
            out.append((p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t))
    return out


def cum_len(pts):
    acc = [0.0]
    for p, q in zip(pts, pts[1:]):
        acc.append(acc[-1] + hav(p, q))
    return acc


def oval(center, length_m, width_ratio=0.62, n=1440):
    """生成操场型闭环（两直道 + 两半圆），周长近似 length_m。"""
    cx, cy = center
    # 解: 2L + 2*pi*r = length, 且 L/r 由 width_ratio 约束 -> 取 r 使椭圆比例合理
    r = length_m / (2 * math.pi + 2 * (1.0 / width_ratio))
    straight = (1.0 / width_ratio) * r
    pts = []
    steps_semi = n // 4
    # 上直道 从左到右
    for i in range(steps_semi):
        t = i / steps_semi
        pts.append((cx - straight / 2 + straight * t, cy + r))
    # 右半圆
    for i in range(steps_semi):
        ang = math.pi / 2 - math.pi * (i / steps_semi)
        pts.append((cx + straight / 2 + r * math.cos(ang), cy + r * math.sin(ang)))
    # 下直道 从右到左
    for i in range(steps_semi):
        t = i / steps_semi
        pts.append((cx + straight / 2 - straight * t, cy - r))
    # 左半圆
    for i in range(steps_semi):
        ang = -math.pi / 2 - math.pi * (i / steps_semi)
        pts.append((cx - straight / 2 + r * math.cos(ang), cy + r * math.sin(ang)))
    pts.append(pts[0])
    # 米 -> 经纬度（以 center 为原点）
    lat0 = cy
    mlat = 111320.0
    mlon = 111320.0 * math.cos(_rad(lat0))
    return [(cx + (x - cx) / mlon, cy + (y - cy) / mlat) for x, y in pts]


def brouter_route(a, b, profile="foot-walking"):
    """BRouter 取真实步行路线, 返回 [(lon,lat), ...]。"""
    lonlats = f"{a[0]},{a[1]}|{b[0]},{b[1]}"
    url = ("https://brouter.de/brouter?" + urllib.parse.urlencode(
        {"lonlats": lonlats, "profile": profile, "alternativeidx": "0", "format": "geojson"}))
    req = urllib.request.Request(url, headers={"User-Agent": "track-gen/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read().decode("utf-8")
    import json
    gj = json.loads(data)
    pts = []
    for f in gj["features"]:
        g = f["geometry"]
        if g["type"] == "LineString":
            pts += [(c[0], c[1]) for c in g["coordinates"]]
        elif g["type"] == "MultiLineString":
            for line in g["coordinates"]:
                pts += [(c[0], c[1]) for c in line]
    return pts


def pace_profile(n_seg, base_speed, rng):
    """分段速度系数: 起步 0.85 -> 稳定 -> 末段 1.12, 叠加噪声后归一化。"""
    fac = []
    for i in range(n_seg):
        p = i / max(1, n_seg - 1)
        shape = 0.86 + 0.18 * min(1.0, p / 0.12)          # 起步加速
        shape *= 1.0 - 0.05 * max(0.0, (p - 0.75) / 0.25) * 0.0  # 保留位
        shape *= 1.0 + 0.12 * max(0.0, (p - 0.85) / 0.15)  # 收尾冲刺
        shape *= rng.uniform(0.97, 1.03)                   # 逐段抖动
        fac.append(shape)
    mean = sum(fac) / len(fac)
    return [f / mean * base_speed for f in fac]


def build(pts, pace_s, start_dt, jitter, seed):
    rng = random.Random(seed)
    acc = cum_len(pts)
    total = acc[-1]
    if total <= 0:
        raise SystemExit("轨迹长度为 0")
    base_speed = 1000.0 / pace_s  # m/s
    # 每个采样点(1s 一个)对应速度, 用 20 段配速曲线插值
    speeds = pace_profile(120, base_speed, rng)
    out = []
    t = start_dt
    d = 0.0
    k = 0
    nseg = len(speeds)
    while k < len(pts) - 1 and d < total:
        idx = min(nseg - 1, int(d / total * nseg))
        v = speeds[idx]
        dt_s = 1.0
        d_next = min(total, d + v * dt_s)
        # 找对应点
        while k < len(pts) - 2 and acc[k + 1] < d_next:
            k += 1
        d0, d1 = acc[k], acc[k + 1]
        p0, p1 = pts[k], pts[k + 1]
        tt = 0.0 if d1 == d0 else (d_next - d0) / (d1 - d0)
        lon = p0[0] + (p1[0] - p0[0]) * tt
        lat = p0[1] + (p1[1] - p0[1]) * tt
        if jitter > 0:
            # 沿法线方向随机偏移, 模拟接收机漂移
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            n = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / n, dx / n
            off = rng.gauss(0, jitter)
            lon += nx * off / (111320.0 * math.cos(_rad(lat)))
            lat += ny * off / 111320.0
        out.append((t, lon, lat, v))
        t += dt.timedelta(seconds=dt_s)
        d = d_next
    return out, total


def write_gpx(rows, path, name="run"):
    head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="track-gen" xmlns="http://www.topografix.com/GPX/1/1">\n'
            f'<trk><name>{name}</name><trkseg>\n')
    body = []
    for t, lon, lat, _v in rows:
        body.append(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}">'
                    f'<time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>\n')
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "".join(body) + "</trkseg></trk></gpx>\n")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)

    lp = sub.add_parser("loop", help="操场/闭环")
    lp.add_argument("--center", required=True, help="lon,lat")
    lp.add_argument("--length", type=float, default=2400.0)
    lp.add_argument("--width-ratio", type=float, default=0.62)

    rp = sub.add_parser("route", help="真实道路")
    rp.add_argument("--from", dest="src", required=True, help="lon,lat")
    rp.add_argument("--to", dest="dst", required=True, help="lon,lat")
    rp.add_argument("--profile", default="foot-walking")

    for p in (lp, rp):
        p.add_argument("--pace", type=float, default=360.0, help="配速 秒/公里")
        p.add_argument("--start", default=None, help="ISO 起始时间(本地)")
        p.add_argument("--jitter", type=float, default=2.0, help="横向漂移 sigma 米")
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--out", default="run.gpx")

    a = ap.parse_args()
    if a.mode == "loop":
        lon, lat = [float(x) for x in a.center.split(",")]
        pts = oval((lon, lat), a.length, a.width_ratio)
    else:
        s = tuple(float(x) for x in a.src.split(","))
        d = tuple(float(x) for x in a.dst.split(","))
        pts = brouter_route(s, d, a.profile)

    if a.start:
        start = dt.datetime.fromisoformat(a.start)
    else:
        start = dt.datetime.now().replace(microsecond=0)
    rows, total = build(pts, a.pace, start, a.jitter, a.seed)
    write_gpx(rows, a.out)
    dur = rows[-1][0] - rows[0][0]
    print(f"点: {len(rows)}  距离: {total/1000:.2f} km  耗时: {dur}  "
          f"配速: {a.pace:.0f}s/km  输出: {a.out}")
    if total / 1000 > 0:
        print(f"均速: {total/dur.total_seconds()*3.6:.2f} km/h")


if __name__ == "__main__":
    main()
