#!/usr/bin/env python3
"""analyze_capture.py — 把 capture.jsonl 和已知规格做差分

用法:
    python analyze_capture.py [capture.jsonl] [-v]

输出:
    1. 时间线        登录 -> 建记录 -> 跑步中上报 -> 提交 的完整顺序与间隔
    2. 接口清单      每个 endpoint 的方法、加密字段、字段集合
    3. 差分报告      实际 vs 已知规格，新增字段标 [NEW]，缺失字段标 ✗
    4. 头部指纹      自定义 header 集合（X-Session-ID 之外的意外项）
    5. 上报节奏      uploadPathPointV3 的批大小与间隔
"""
import json
import sys
from collections import defaultdict, OrderedDict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 已知基线（来自 PC 端 wxapkg 静态还原 + PC 端抓包实测）
KNOWN = {
    "/xcxapi/userLogin/":        {"m": "POST", "enc": None,      "f": {"iphone", "password"}},
    "/xcxapi/login/":            {"m": "POST", "enc": None,      "f": {"iphone", "code"}},
    "/xcxapi/sendSMS/":          {"m": "POST", "enc": None,      "f": {"iphone"}},
    "/xcxapi/checkCode/":        {"m": "POST", "enc": None,      "f": {"iphone", "code"}},
    "/xcxtapi/activity/randrunInfo": {"m": "GET", "enc": None,   "f": {"lat", "lng"}},
    "/xcxapi/createLine/":       {"m": "POST", "enc": None,      "f": {"student_id", "pass_point"}},
    "/xcxapi/checkRecord/":      {"m": "POST", "enc": "a",       "f": {"record_id", "pace", "running_time",
                                                                    "mileage", "start_time", "end_time",
                                                                    "pass_point", "step_count"}},
    "/xcxapi/updateRecordNew/":  {"m": "POST", "enc": "a",       "f": set()},
    "/xcxapi/uploadPathPointV3/": {"m": "POST", "enc": "img_url", "f": {"record_id", "path_point", "path_image"}},
    "/xcxapi/recordInfo/":       {"m": "GET",  "enc": None,      "f": {"id"}},
    "/xcxapi/GetPathPoints/":    {"m": "GET",  "enc": None,      "f": {"record_id"}},
    "/xcxapi/getUserInfo/":      {"m": "GET",  "enc": None,      "f": {"id"}},
    "/xcxapi/recordListNew/":    {"m": "GET",  "enc": None,      "f": {"page", "page_size"}},
    "/xcxapi/createLogs/":       {"m": "POST", "enc": None,      "f": set()},
}


def norm_path(p: str) -> str:
    return p.split("?")[0]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "capture.jsonl"
    verbose = "-v" in sys.argv
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    print(f"共 {len(rows)} 条请求\n")

    # ---------- 1. 时间线 ----------
    print("=" * 78)
    print("1. 时间线")
    print("=" * 78)
    t0 = None
    for r in rows:
        p = norm_path(r["path"])
        if "ecust" not in r["host"]:
            continue
        if t0 is None:
            t0 = r["ts"]
        dec = r.get("req_decrypted") or {}
        hint = ""
        for k, v in dec.items():
            if isinstance(v, dict):
                hint = json.dumps(v, ensure_ascii=False)[:90]
        print(f"  {r['ts'][11:]}  {r['method']:<5} {r['status']:<3} {p:<46} {hint}")

    # ---------- 2. 接口清单 ----------
    print("\n" + "=" * 78)
    print("2. 接口清单")
    print("=" * 78)
    agg = OrderedDict()
    for r in rows:
        p = norm_path(r["path"])
        a = agg.setdefault(p, {"n": 0, "methods": set(), "enc": set(), "fields": set(),
                               "status": set(), "maxbody": 0})
        a["n"] += 1
        a["methods"].add(r["method"])
        a["status"].add(r.get("status"))
        for k in (r.get("req_json") or {}):
            if k in ("a", "img_url"):
                a["enc"].add(k)
            else:
                a["fields"].add(k)
        for k, v in (r.get("req_decrypted") or {}).items():
            if isinstance(v, dict):
                a["fields"].update(v.keys())
                a["maxbody"] = max(a["maxbody"], len(json.dumps(v)))

    for p, a in sorted(agg.items(), key=lambda x: -x[1]["n"]):
        print(f"  {p}")
        print(f"     次数={a['n']:<4} 方法={'/'.join(sorted(a['methods']))} "
              f"状态={sorted(a['status'])} 加密={sorted(a['enc']) or '无'}")
        if a["fields"]:
            print(f"     字段: {', '.join(sorted(a['fields']))}")

    # ---------- 3. 差分 ----------
    print("\n" + "=" * 78)
    print("3. 差分报告（实际 vs 已知基线）")
    print("=" * 78)
    diffs = 0
    for p, a in agg.items():
        base = KNOWN.get(p)
        if not base:
            print(f"  [新接口] {p}  → 已知基线里没有，需要补进规格")
            diffs += 1
            continue
        extra = a["fields"] - base["f"]
        missing = base["f"] - a["fields"]
        encbad = a["enc"] and a["enc"] != {base["enc"]}
        if extra or missing or encbad:
            diffs += 1
            print(f"  {p}")
            if extra:
                print(f"     [NEW] 新增字段: {', '.join(sorted(extra))}")
            if missing:
                print(f"     [MISS] 缺失字段: {', '.join(sorted(missing))}  (可能只在特定分支出现)")
            if encbad:
                print(f"     ! 加密字段变化: 基线={base['enc']} 实际={sorted(a['enc'])}")
    for p in KNOWN:
        if p not in agg:
            print(f"  [未出现] {p}  → 本次流程没有触发")
            diffs += 1
    if not diffs:
        print("  无差异，基线完全吻合")

    # ---------- 4. 头部指纹 ----------
    print("\n" + "=" * 78)
    print("4. 头部指纹")
    print("=" * 78)
    hdr = defaultdict(int)
    for r in rows:
        for k in (r.get("req_headers") or {}):
            hdr[k.lower()] += 1
    known_h = {"content-type", "content-length", "user-agent", "host", "accept", "accept-encoding",
               "connection", "x-session-id", "authorization", "referer", "origin"}
    for k, n in sorted(hdr.items(), key=lambda x: -x[1]):
        mark = "" if k in known_h else "  [NEW]"
        print(f"  {k:<40} {n:>4}{mark}")

    # ---------- 5. 上报节奏 ----------
    print("\n" + "=" * 78)
    print("5. 轨迹上报节奏")
    print("=" * 78)
    ups = [r for r in rows if norm_path(r["path"]).endswith("uploadPathPointV3/")]
    if not ups:
        print("  本次没有上传轨迹")
    else:
        prev = None
        for i, r in enumerate(ups, 1):
            d = r.get("req_decrypted", {}).get("img_url", {})
            n = len(d.get("path_point") or []) if isinstance(d, dict) else 0
            gap = ""
            if prev:
                try:
                    gap = f" 间隔={int((_ts(r['ts']) - _ts(prev['ts'])))}s"
                except Exception:
                    pass
            print(f"  #{i:<3} {r['ts'][11:]}  {n:>3} 点{gap}")
            prev = r
        if verbose:
            print("\n  首点样本:")
            d = ups[0].get("req_decrypted", {}).get("img_url", {})
            pp = d.get("path_point") or []
            if pp:
                print("   ", json.dumps(pp[0], ensure_ascii=False))


def _ts(s):
    h, m, sec = s[11:].split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


if __name__ == "__main__":
    main()
