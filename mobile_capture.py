"""mobile_capture.py — 手机端微信小程序全量抓包 addon

用法:
    mitmdump -s mobile_capture.py -p 8080 --set flow_detail=0

输出:
    capture.jsonl   逐条 JSONL（请求+响应，加密字段自动解密）
    capture_raw.flow  原始 flow（mitmdump -w 另存，可回放）

关注域可用 --set focus=ecust.edu.cn,weixin.qq.com 覆盖；不设则记录全部 HTTPS。
"""
import base64
import json
import os
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mitmproxy import ctx, http

AES_KEY = b"A7fK92LmXyQw8vRp"          # 从小程序 app-service.js 抠出
ENC_FIELDS = ("a", "img_url")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "capture.jsonl")

# 关注域（子串匹配）；空 = 记录全部
FOCUS = []


def _unpad(b: bytes) -> bytes:
    return b[: -b[-1]] if b and 1 <= b[-1] <= 16 else b


def decrypt_blob(v: str):
    iv_b64, sep, ct_b64 = v.partition(":")
    if not sep:
        return {"_raw": v[:200]}
    try:
        iv = base64.b64decode(iv_b64)
        ct = base64.b64decode(ct_b64)
        d = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).decryptor()
        plain = _unpad(d.update(ct) + d.finalize()).decode("utf-8", "replace")
        try:
            return json.loads(plain)
        except Exception:
            return {"_plain": plain}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def encrypt_blob(obj) -> str:
    iv = os.urandom(16)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    e = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv).decode() + ":" + base64.b64encode(e.update(data) + e.finalize()).decode()


def _interesting(host: str) -> bool:
    if not FOCUS:
        return True
    return any(f in host for f in FOCUS)


def _safe_headers(h) -> dict:
    out = {}
    for k, v in h.items():
        kl = k.lower()
        if kl in ("cookie", "set-cookie"):
            out[k] = v[:400] + ("…" if len(v) > 400 else "")
        else:
            out[k] = v
    return out


class Capture:
    def __init__(self):
        self.seq = 0
        self._t0 = {}

    def load(self, loader):
        loader.add_option("focus", str, "", "逗号分隔的关注域名，空=全部")

    def configure(self, updates):
        global FOCUS
        if "focus" in updates:
            raw = ctx.options.focus or ""
            FOCUS = [x.strip() for x in raw.split(",") if x.strip()]

    def request(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        if not _interesting(host):
            return
        self.seq += 1
        self._t0[flow.id] = time.time()

        text = flow.request.get_text(strict=False) or ""
        parsed = None
        if text:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None

        decrypted = {}
        if isinstance(parsed, dict):
            for f in ENC_FIELDS:
                v = parsed.get(f)
                if isinstance(v, str) and ":" in v:
                    decrypted[f] = decrypt_blob(v)

        rec = {
            "seq": self.seq,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "host": host,
            "method": flow.request.method,
            "path": flow.request.path,
            "url": flow.request.pretty_url,
            "req_headers": _safe_headers(flow.request.headers),
            "req_body": text[:20000],
            "req_json": parsed,
            "req_decrypted": decrypted,
            "client": f"{flow.client_conn.peername[0]}" if flow.client_conn.peername else None,
        }
        flow.metadata["rec"] = rec

    def response(self, flow: http.HTTPFlow):
        rec = flow.metadata.get("rec")
        if not rec:
            return
        t0 = self._t0.pop(flow.id, None)
        body = flow.response.get_text(strict=False) or ""
        rec.update({
            "status": flow.response.status_code,
            "duration_ms": int((time.time() - t0) * 1000) if t0 else None,
            "resp_headers": _safe_headers(flow.response.headers),
            "resp_body": body[:40000],
        })
        line = json.dumps(rec, ensure_ascii=False)
        print(f"[{rec['seq']:>3}] {rec['method']:<6} {rec['status']} {rec['path'][:80]}")
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(line + "\n")


addons = [Capture()]
