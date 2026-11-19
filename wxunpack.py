import sys, os, struct, hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

def decrypt(data: bytes, appid: str) -> bytes:
    if data[:6] != b"V1MMWX":
        return data
    key = hashlib.pbkdf2_hmac("sha1", appid.encode(), b"saltiest", 1000, 32)
    iv = b"the iv: 16 bytes"
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    head = dec.update(data[6:6 + 1024]) + dec.finalize()
    xor = ord(appid[-2])
    tail = bytes(b ^ xor for b in data[6 + 1024:])
    return head[:1023] + tail

def unpack(raw: bytes, outdir: str):
    if raw[0] != 0xBE:
        raise SystemExit(f"bad magic {raw[0]:#x}")
    index_len, body_len = struct.unpack(">II", raw[5:13])
    count = struct.unpack(">I", raw[14:18])[0]
    p, files = 18, []
    for _ in range(count):
        nl = struct.unpack(">I", raw[p:p + 4])[0]; p += 4
        name = raw[p:p + nl].decode("utf-8", "replace"); p += nl
        off, size = struct.unpack(">II", raw[p:p + 8]); p += 8
        files.append((name, off, size))
    for name, off, size in files:
        dst = os.path.join(outdir, name.lstrip("/"))
        os.makedirs(os.path.dirname(dst) or outdir, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(raw[off:off + size])
    return files

if __name__ == "__main__":
    src, appid, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    data = open(src, "rb").read()
    plain = decrypt(data, appid)
    os.makedirs(outdir, exist_ok=True)
    files = unpack(plain, outdir)
    print(f"decrypted {len(data)} -> {len(plain)} bytes, {len(files)} files")
    for n, o, s in files[:40]:
        print(f"  {s:>8}  {n}")