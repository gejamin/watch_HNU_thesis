# -*- coding: utf-8 -*-
"""把若干张验证码拼成一张大图(放大3倍)，便于一次性人工识别，标注 ground truth。"""
import glob
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from PIL import Image, ImageDraw

COLS, ROWS = 2, 6          # 每张拼图 12 张验证码
PER = COLS * ROWS
SCALE = 3
PAD = 12

files = sorted(glob.glob("caps/*.gif"))
print("samples:", len(files))

for page in range(len(files) // PER):
    chunk = files[page * PER:(page + 1) * PER]
    cw, ch = 102 * SCALE, 46 * SCALE
    W = COLS * cw + (COLS + 1) * PAD
    H = ROWS * ch + (ROWS + 1) * PAD
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    dr = ImageDraw.Draw(canvas)
    for i, f in enumerate(chunk):
        r, c = divmod(i, COLS)
        x = PAD + c * (cw + PAD)
        y = PAD + r * (ch + PAD)
        im = Image.open(f).convert("RGB").resize((cw, ch), Image.NEAREST)
        canvas.paste(im, (x, y))
        dr.rectangle([x - 2, y - 2, x + cw + 1, y + ch + 1], outline=(255, 0, 0), width=2)
        dr.text((x + 4, y + 4), "#%d" % i, fill=(0, 128, 0))
    out = "montage_%d.png" % page
    canvas.save(out)
    print("%s  ->  %s" % (out, ", ".join("%d:%s" % (i, f.split("\\")[-1].split("/")[-1])
                                         for i, f in enumerate(chunk))))
