"""hold_inject.py — 长期保持 SSL bypass 注入，断线自动重连。"""
import time

import frida

HOST = "127.0.0.1:39141"
SCRIPT = r"C:\Users\forestree\Desktop\hp-track\wechat_ssl.js"
POLL = 5


def attach_all(dev, src, bound):
    procs = [p for p in dev.enumerate_processes() if "tencent.mm" in p.name]
    for p in procs:
        if p.pid in bound:
            continue
        try:
            sess = dev.attach(p.pid)
            sc = sess.create_script(src)
            sc.on("message", lambda m, d: print(f"[{p.name}] {m}", flush=True))
            sc.load()
            bound.add(p.pid)
            print(f"[+] attached {p.name} (pid={p.pid})", flush=True)
        except Exception as e:
            print(f"[!] {p.name} fail: {type(e).__name__} {str(e)[:60]}", flush=True)


def main():
    src = open(SCRIPT, encoding="utf-8").read()
    bound = set()
    print("=== hold injection started ===", flush=True)
    while True:
        try:
            dev = frida.get_device_manager().add_remote_device(HOST)
            attach_all(dev, src, bound)
        except Exception as e:
            print(f"[!] reconnect: {type(e).__name__} {str(e)[:60]}", flush=True)
            bound.clear()
        time.sleep(POLL)


if __name__ == "__main__":
    main()
