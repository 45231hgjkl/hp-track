"""inject_wechat.py — spawn 微信并注入 SSL 绕过脚本，保持会话存活。"""
import sys
import time

import frida

HOST = "127.0.0.1:39141"
TARGET = "com.tencent.mm"
SCRIPT = r"C:\Users\forestree\Desktop\hp-track\wechat_ssl.js"


def on_message(msg, data):
    if msg.get("type") == "send":
        print("[script]", msg.get("payload"), flush=True)
    elif msg.get("type") == "error":
        print("[error]", msg.get("stack") or msg.get("description"), flush=True)
    else:
        print("[msg]", msg, flush=True)


def main():
    dev = frida.get_device_manager().add_remote_device(HOST)
    print("device ok:", dev, flush=True)

    print("spawning", TARGET, "...", flush=True)
    pid = dev.spawn([TARGET])
    print("pid =", pid, flush=True)

    session = dev.attach(pid)
    with open(SCRIPT, encoding="utf-8") as f:
        src = f.read()

    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print("script loaded, resuming", flush=True)
    dev.resume(pid)

    print("=== INJECTION ACTIVE ===", flush=True)
    while True:
        time.sleep(10)


if __name__ == "__main__":
    main()
