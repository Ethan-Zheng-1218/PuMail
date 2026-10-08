# -*- coding: utf-8 -*-
"""Generate sidebar BMP (164x314) and header BMP (150x57) for NSIS installer."""
import os
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # electron/build -> project root
logo_path = os.path.join(ROOT, "logo.png")
sidebar_path = os.path.join(HERE, "sidebar.bmp")
header_path = os.path.join(HERE, "header.bmp")

BG = (248, 249, 250, 255)  # light-gray / bootstrap-light

logo = Image.open(logo_path).convert("RGBA")

# ── sidebar 164x314 ──
sb = Image.new("RGBA", (164, 314), BG)
lr = logo.copy()
max_h = int(314 * 0.55)
lr.thumbnail((120, max_h), Image.LANCZOS)
x = (164 - lr.width) // 2
y = (314 - lr.height) // 2
sb.paste(lr, (x, y), lr if lr.mode == "RGBA" else None)
sb.convert("RGB").save(sidebar_path, "BMP")
print(f"[OK] sidebar.bmp  -> {sidebar_path}")

# ── header 150x57 ──
hd = Image.new("RGBA", (150, 57), BG)
hr = logo.copy()
hr.thumbnail((130, 48), Image.LANCZOS)
x = (150 - hr.width) // 2
y = (57 - hr.height) // 2
hd.paste(hr, (x, y), hr if hr.mode == "RGBA" else None)
hd.convert("RGB").save(header_path, "BMP")
print(f"[OK] header.bmp    -> {header_path}")
