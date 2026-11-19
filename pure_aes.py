#!/usr/bin/env python3
"""pure_aes.py — AES-128-CBC 纯标准库实现

替换 cryptography 依赖：只用到内置的 base64/json。
接口与 ecust.py 里的 enc/dec 完全等价。

    python pure_aes.py      # 与 cryptography 对拍

性能：单块约 40us，本项目 payload 通常 < 8KB，单次请求开销 < 30ms。
"""
import base64
import json
import os

__all__ = ["enc", "dec", "cbc_encrypt", "cbc_decrypt", "SBOX"]


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a


def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _build_sbox():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x ^= _xtime(x)          # 生成元 3
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    inv = [0] * 256
    for a in range(1, 256):
        inv[a] = exp[255 - log[a]]
    box = []
    for a in range(256):
        b = inv[a]
        box.append(b ^ _rotl8(b, 1) ^ _rotl8(b, 2) ^ _rotl8(b, 3) ^ _rotl8(b, 4) ^ 0x63)
    return box


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i


def _gmul(a: int, b: int) -> int:
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        b >>= 1
        a = _xtime(a)
    return r


# 预先算好 MixColumns 用到的乘法表，避免每块重复循环
_MUL = {k: [_gmul(a, k) for a in range(256)] for k in (2, 3, 9, 11, 13, 14)}


def _key_expand(key: bytes):
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [sum(w[4 * i:4 * i + 4], []) for i in range(11)]


def _shift_rows(s):
    return [s[0], s[5], s[10], s[15],
            s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7],
            s[12], s[1], s[6], s[11]]


def _inv_shift_rows(s):
    return [s[0], s[13], s[10], s[7],
            s[4], s[1], s[14], s[11],
            s[8], s[5], s[2], s[15],
            s[12], s[9], s[6], s[3]]


def _mix_columns(s):
    m2, m3 = _MUL[2], _MUL[3]
    out = []
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out += [m2[a0] ^ m3[a1] ^ a2 ^ a3,
                a0 ^ m2[a1] ^ m3[a2] ^ a3,
                a0 ^ a1 ^ m2[a2] ^ m3[a3],
                m3[a0] ^ a1 ^ a2 ^ m2[a3]]
    return out


def _inv_mix_columns(s):
    m9, m11, m13, m14 = _MUL[9], _MUL[11], _MUL[13], _MUL[14]
    out = []
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out += [m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3],
                m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3],
                m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3],
                m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3]]
    return out


def _encrypt_block(rk, block):
    s = [b ^ k for b, k in zip(block, rk[0])]
    for rnd in range(1, 11):
        s = [SBOX[b] for b in _shift_rows(s)]
        if rnd != 10:
            s = _mix_columns(s)
        s = [b ^ k for b, k in zip(s, rk[rnd])]
    return s


def _decrypt_block(rk, block):
    s = [b ^ k for b, k in zip(block, rk[10])]
    for rnd in range(9, -1, -1):
        s = [INV_SBOX[b] for b in _inv_shift_rows(s)]
        s = [b ^ k for b, k in zip(s, rk[rnd])]
        if rnd != 0:
            s = _inv_mix_columns(s)
    return s


def cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("data 必须是 16 字节整数倍")
    rk = _key_expand(key)
    out = bytearray()
    prev = list(iv)
    for i in range(0, len(data), 16):
        prev = _encrypt_block(rk, [data[i + j] ^ prev[j] for j in range(16)])
        out += bytes(prev)
    return bytes(out)


def cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("data 必须是 16 字节整数倍")
    rk = _key_expand(key)
    out = bytearray()
    prev = list(iv)
    for i in range(0, len(data), 16):
        blk = list(data[i:i + 16])
        out += bytes(b ^ p for b, p in zip(_decrypt_block(rk, blk), prev))
        prev = blk
    return bytes(out)


# ---------------- 与 ecust.py 同签名的 enc/dec ----------------
KEY = b"A7fK92LmXyQw8vRp"


def enc(obj: dict, key: bytes = KEY) -> str:
    """base64(iv) : base64(AES-128-CBC/PKCS7(json))"""
    iv = os.urandom(16)
    d = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    pad = 16 - len(d) % 16
    d += bytes([pad]) * pad
    return base64.b64encode(iv).decode() + ":" + base64.b64encode(cbc_encrypt(key, iv, d)).decode()


def dec(text: str, key: bytes = KEY) -> dict:
    iv_b64, _, ct_b64 = text.partition(":")
    plain = cbc_decrypt(key, base64.b64decode(iv_b64), base64.b64decode(ct_b64))
    return json.loads(plain[:-plain[-1]].decode())


# ---------------- 对拍 ----------------
def _selftest():
    assert SBOX[0x00] == 0x63 and SBOX[0x01] == 0x7C and SBOX[0x53] == 0xED, "S-box 不匹配 FIPS-197"
    assert INV_SBOX[0x63] == 0x00

    # FIPS-197 附录 B 官方测试向量
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    pt = bytes.fromhex("3243f6a8885a308d313198a2e0370734")
    ct = bytes.fromhex("3925841d02dc09fbdc118597196a0b32")
    rk = _key_expand(key)
    assert bytes(_encrypt_block(rk, list(pt))) == ct, "单块加密与 FIPS-197 向量不符"
    assert bytes(_decrypt_block(rk, list(ct))) == pt, "单块解密与 FIPS-197 向量不符"
    print("  ok   FIPS-197 附录 B 向量")

    # 与 cryptography 全链路对拍
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        print("  --   cryptography 未安装，跳过对拍")
        return 0

    import random
    rnd = random.Random(20261119)
    n = 0
    for _ in range(200):
        k = bytes(rnd.getrandbits(8) for _ in range(16))
        iv = bytes(rnd.getrandbits(8) for _ in range(16))
        size = 16 * rnd.randint(1, 64)
        data = bytes(rnd.getrandbits(8) for _ in range(size))

        e = Cipher(algorithms.AES(k), modes.CBC(iv)).encryptor()
        ref_ct = e.update(data) + e.finalize()
        mine_ct = cbc_encrypt(k, iv, data)
        assert mine_ct == ref_ct, f"加密不一致 key={k.hex()} iv={iv.hex()}"

        d = Cipher(algorithms.AES(k), modes.CBC(iv)).decryptor()
        assert cbc_decrypt(k, iv, ref_ct) == d.update(ref_ct) + d.finalize(), "解密不一致"
        n += 1
    print(f"  ok   {n} 组随机向量与 cryptography 双向一致")

    # enc/dec 往返
    obj = {"record_id": 5548410, "mileage": 2000, "path_point": [
        {"name": "", "lat": "30.830092", "lng": "121.504504", "accuracy": 5.0, "timestamp": 1763568000000}],
        "path_image": "", "note": "中文与 emoji 🏃 也要能过"}
    assert dec(enc(obj)) == obj, "enc/dec 往返失败"
    print("  ok   enc/dec 往返（含中文/emoji）")

    import time
    big = {"path_point": [{"lat": "30.83", "lng": "121.50", "accuracy": 5.0,
                           "timestamp": 1763568000000, "name": ""} for _ in range(50)]}
    t0 = time.perf_counter()
    for _ in range(20):
        enc(big)
    ms = (time.perf_counter() - t0) / 20 * 1000
    print(f"  ok   50 点轨迹 enc() 单次 {ms:.1f} ms")
    return 0


if __name__ == "__main__":
    print("pure_aes selftest")
    raise SystemExit(_selftest())
