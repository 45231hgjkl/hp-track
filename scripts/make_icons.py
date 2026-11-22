#!/usr/bin/env python3
"""make_icons.py — 从仓库根目录 logo.png 生成三端图标

产物（都会提交进仓库，构建时不再依赖 Pillow）：
  assets/icon.ico                                   Windows exe 图标（多尺寸）
  android/.../res/mipmap-*/ic_launcher.png          Android 传统图标 48–192dp
  android/.../res/mipmap-*/ic_launcher_foreground.png   Android 8+ 自适应前景 108–432dp
  android/.../res/mipmap-anydpi-v26/ic_launcher.xml     自适应图标声明
  android/.../res/values/colors.xml                 自适应背景色
  src/index.html                                    内联 base64 favicon

重新生成（只在这一步需要 Pillow）：
    python scripts/make_icons.py
"""
import base64
import io
import os
import re
import sys

from PIL import Image, ImageDraw

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "logo.png")
ICO = os.path.join(ROOT, "assets", "icon.ico")
RES = os.path.join(ROOT, "android", "app", "src", "main", "res")
HTML = os.path.join(ROOT, "src", "index.html")

# Android 传统图标（48dp 基准）
LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
# 自适应图标前景画布（108dp 基准）
ADAPTIVE = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
BG_COLOR = "#4C9CF0"        # 取 logo 天空蓝，作为自适应图标背景/裁切填充
ROUND_RATIO = 0.22          # 传统图标圆角，避免 Android 7 上显示成生硬方块


def rounded(img: Image.Image, ratio: float = ROUND_RATIO) -> Image.Image:
    """圆角遮罩（4 倍超采样，边缘不留锯齿）。"""
    size = img.size[0]
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1],
                                           radius=int(big * ratio), fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask.resize((size, size), Image.LANCZOS))
    return out


def square(src: Image.Image, size: int) -> Image.Image:
    return src.resize((size, size), Image.LANCZOS)


def main() -> int:
    if not os.path.isfile(SRC):
        print(f"找不到源图: {SRC}", file=sys.stderr)
        return 2
    src = Image.open(SRC).convert("RGB")
    if src.size[0] != src.size[1]:
        side = min(src.size)
        left = (src.size[0] - side) // 2
        top = (src.size[1] - side) // 2
        src = src.crop((left, top, left + side, top + side))
        print(f"源图不是正方形，已居中裁成 {side}x{side}")

    os.makedirs(os.path.dirname(ICO), exist_ok=True)
    frames = [rounded(square(src, n)) for n in ICO_SIZES]
    frames[-1].save(ICO, format="ICO", sizes=[(n, n) for n in ICO_SIZES],
                    append_images=frames[:-1])
    print(f"  ico   {os.path.relpath(ICO, ROOT)}  ({os.path.getsize(ICO)} B, {len(ICO_SIZES)} 尺寸)")

    # Android 传统图标
    for density, size in LEGACY.items():
        out = os.path.join(RES, f"mipmap-{density}", "ic_launcher.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        rounded(square(src, size)).save(out, format="PNG", optimize=True)
        print(f"  png   mipmap-{density}/ic_launcher.png  {size}x{size}")

    # Android 自适应图标前景（满幅，系统负责遮罩，不能自带圆角）
    # 插画类图像量化到 256 色可把体积压到 1/4，图标显示尺寸小，视觉上无差别。
    for density, size in ADAPTIVE.items():
        out = os.path.join(RES, f"mipmap-{density}", "ic_launcher_foreground.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        square(src, size).quantize(colors=256, method=Image.MEDIANCUT).save(
            out, format="PNG", optimize=True)
        print(f"  png   mipmap-{density}/ic_launcher_foreground.png  {size}x{size}")

    anydpi = os.path.join(RES, "mipmap-anydpi-v26")
    os.makedirs(anydpi, exist_ok=True)
    with open(os.path.join(anydpi, "ic_launcher.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
                '    <background android:drawable="@color/ic_launcher_background" />\n'
                '    <foreground android:drawable="@mipmap/ic_launcher_foreground" />\n'
                '</adaptive-icon>\n')
    print("  xml   mipmap-anydpi-v26/ic_launcher.xml")

    colors = os.path.join(RES, "values", "colors.xml")
    os.makedirs(os.path.dirname(colors), exist_ok=True)
    with open(colors, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<resources>\n'
                f'    <color name="ic_launcher_background">{BG_COLOR}</color>\n'
                '</resources>\n')
    print(f"  xml   values/colors.xml  ({BG_COLOR})")

    # 页面 favicon：内联 base64，三端（源码 / exe / pyz / APK）都不需要额外资源文件
    buf = io.BytesIO()
    rounded(square(src, 48)).save(buf, format="PNG", optimize=True)
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    html = re.sub(r'\n?<link rel="icon"[^>]*>', "", html)
    html = html.replace("<title>", f'<link rel="icon" type="image/png" href="{uri}">\n<title>', 1)
    with open(HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  html  inline favicon  ({len(uri)} B base64)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
