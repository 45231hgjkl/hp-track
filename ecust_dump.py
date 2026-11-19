"""ecust_dump.py — mitmproxy addon: 自动解密华东理工健康跑的加密请求体。

用法:
    mitmdump -s ecust_dump.py -p 8080 --set upstream_cert=false

抓到的明文会写到同目录的 ecust_flows.log，同时也实时打印到控制台。
"""
import base64
import json
import os
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mitmproxy import http

AES_KEY = b"A7fK92LmXyQw8vRp"          # 从小程序 app-service.js 里抠出来的硬编码密钥
HOST_HINT = "run.ecust.edu.cn"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecust_flows.log")

# 请求体里哪些字段是加密的
ENC_FIELDS = ("a", "img_url")


def _unpad(b: bytes) -> bytes:
    if b and 1 <= b[-1] <= 16:
        return b[: -b[-1]]
    return b


def decrypt_blob(v: str):
    """'base64(iv):base64(ciphertext)' -> dict"""
    iv_b64, sep, ct_b64 = v.partition(":")
    if not sep:
        return {"_raw": v[:120]}
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ct_b64)
    d = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).decryptor()
    plain = _unpad(d.update(ct) + d.finalize()).decode("utf-8", "replace")
    try:
        return json.loads(plain)
    except Exception:
        return {"_plain": plain}


def encrypt_blob(obj) -> str:
    """dict -> 'base64(iv):base64(ciphertext)'，复现小程序侧加密。"""
    iv = os.urandom(16)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    e = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv).decode() + ":" + base64.b64encode(e.update(data) + e.finalize()).decode()


def _write(line: str):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


class EcustDecrypt:
    def request(self, flow: http.HTTPFlow):
        if HOST_HINT not in flow.request.pretty_host:
            return
        body = flow.request.get_text(strict=False) or ""
        parsed = None
        if body:
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = None

        entry = {
            "t": time.strftime("%H:%M:%S"),
            "method": flow.request.method,
            "url": flow.request.pretty_url,
            "auth": flow.request.headers.get("Authorization", ""),
            "session": flow.request.headers.get("X-Session-ID", ""),
        }

        decrypted = None
        if isinstance(parsed, dict):
            for f in ENC_FIELDS:
                v = parsed.get(f)
                if isinstance(v, str) and ":" in v:
                    decrypted = {f: decrypt_blob(v)}
                    break

        _write(json.dumps({**entry, "plain": parsed if decrypted is None else None,
                           "decrypted": decrypted}, ensure_ascii=False))

    def response(self, flow: http.HTTPFlow):
        if HOST_HINT not in flow.request.pretty_host:
            return
        text = flow.response.get_text(strict=False) or ""
        if len(text) > 4000:
            text = text[:4000] + "...<truncated>"
        _write(json.dumps({"t": time.strftime("%H:%M:%S"), "resp": flow.request.path,
                           "body": text}, ensure_ascii=False))


addons = [EcustDecrypt()]
