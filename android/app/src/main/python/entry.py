"""entry.py — Chaquopy 入口，给 Kotlin/Java 壳调用

壳在启动时先建好两个路径，再把环境变量塞进来，业务代码零改动：
    HP_TRACK_HOME   可写目录（session.json 落这里，替代 %APPDATA%）
    HP_TRACK_INDEX  index.html 的实体路径（从 assets 复制到私有目录）

之后直接把 ecust_web.main() 跑在调用线程里（阻塞，等于 serve_forever）。
"""


def start(port: int, home: str, index: str) -> None:
    import os
    import sys

    os.environ["HP_TRACK_HOME"] = home
    os.environ["HP_TRACK_INDEX"] = index
    sys.argv = ["ecust_web", "--port", str(port), "--no-browser"]

    import ecust_web          # 必须在设置环境变量之后导入
    ecust_web.main()
