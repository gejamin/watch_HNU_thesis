# -*- coding: utf-8 -*-
"""
验证码 OCR（湖南大学研究生系统 stulogin 验证码）。

验证码特征（实测 40+ 样本）:
  * 102x46 GIF, 4 位数字, 深蓝色描边字;
  * 数字位置**固定**: 第 k 位横向占 x ∈ [7+25k, 19+25k]（槽距 25px），
    数字本体只在其槽位中心附近做随机抖动，实测纵向约 ±5px、横向约 ±3px;
  * 存在 1px 干扰线与浅色噪点，且颜色高度量化（GIF 调色板只有 40 来种颜色）。

因此策略是：固定槽位切片 -> 颜色掩码 -> 对 0-9 模板做**平移不变的最小汉明距离**匹配。
模板由人工标注样本经"对齐 + 多数投票"生成（见 build_templates.py），投票过程天然去掉了干扰线。
"""
from __future__ import annotations

import io
import os
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILE = os.path.join(HERE, "templates.npz")

SLOT_STEP = 25
SLOT_X0 = -3             # 切片左边界 = 25k - 3（刚好排除左侧相邻数字）
SLOT_W = 27              # 覆盖数字本体
Y0, Y1 = 0, 46           # 纵向取整幅高度，保证上下抖动时不被裁掉
SHIFT_RY = 9             # 纵向抖动搜索范围
SHIFT_RX = 6             # 横向抖动搜索范围

_TEMPLATES: Optional[dict] = None


# --------------------------------------------------------------------- 预处理
def ink_mask(img: Image.Image) -> np.ndarray:
    """深蓝描边 = 掩码。背景噪点亮度高或偏红，被同时排除。"""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    r, b = a[..., 0], a[..., 2]
    lum = a.sum(axis=2)
    return ((b - r) >= 50) & (lum < 560)


def extract_slots(mask: np.ndarray) -> List[np.ndarray]:
    """
    按固定槽位切出 4 个字符位图。

    第 k 位的窗口是绝对坐标 x ∈ [25k-3, 25k+24)。第 0 位会越左边界，所以先左右
    各补 3 列 0，再按 [25k, 25k+27) 切片 —— 这样 4 个槽位几何完全一致。
    """
    padded = np.pad(mask[Y0:Y1], ((0, 0), (abs(SLOT_X0), abs(SLOT_X0))), constant_values=False)
    return [padded[:, SLOT_STEP * k:SLOT_STEP * k + SLOT_W].copy() for k in range(4)]


def mask_from_bytes(data: bytes) -> np.ndarray:
    return ink_mask(Image.open(io.BytesIO(data)))


# ----------------------------------------------------------------- 平移与匹配
def shift_patch(p: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """把位图平移 (dy, dx)，越界补 0。"""
    q = np.zeros_like(p)
    h, w = p.shape
    ys0, ys1 = max(0, dy), min(h, h + dy)
    xs0, xs1 = max(0, dx), min(w, w + dx)
    if ys1 <= ys0 or xs1 <= xs0:
        return q
    q[ys0:ys1, xs0:xs1] = p[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx]
    return q


def best_shift(ref: np.ndarray, p: np.ndarray, ry: int = SHIFT_RY,
               rx: int = SHIFT_RX) -> Tuple[int, int, int]:
    """
    在 ±ry / ±rx 平移范围内求 p 与 ref 的最小汉明距离，返回 (距离, dy, dx)。

    注意：距离在**整幅画布**上统计（越界部分按"不一致"计），否则会出现
    "把图形整体移出画布 -> 重叠区很小 -> 距离很小" 的退化解。
    """
    best, bdy, bdx = 1 << 30, 0, 0
    for dy in range(-ry, ry + 1):
        for dx in range(-rx, rx + 1):
            d = int(np.count_nonzero(ref != shift_patch(p, dy, dx)))
            if d < best:
                best, bdy, bdx = d, dy, dx
    return best, bdy, bdx


# ---------------------------------------------------------------------- 模板
def load_templates() -> dict:
    global _TEMPLATES
    if _TEMPLATES is None:
        if not os.path.exists(TEMPLATE_FILE):
            raise RuntimeError("缺少模板文件 %s，请先运行 build_templates.py" % TEMPLATE_FILE)
        z = np.load(TEMPLATE_FILE)
        _TEMPLATES = {str(k): z[k].astype(bool) for k in z.files}
    return _TEMPLATES


def build_digit_template(patches: List[np.ndarray], rounds: int = 4) -> np.ndarray:
    """把同一数字的多个样本对齐后多数投票，得到去噪模板。"""
    ref = patches[0].copy()
    aligned: List[np.ndarray] = []
    for _ in range(rounds):
        aligned = []
        for p in patches:
            _, dy, dx = best_shift(ref, p)
            aligned.append(shift_patch(p, dy, dx))
        stack = np.stack(aligned).astype(np.int16)
        new_ref = (stack.mean(axis=0) >= 0.5)
        if np.array_equal(new_ref, ref.astype(bool)):
            ref = new_ref
            break
        ref = new_ref
    return aligned


def save_templates(templates: dict, path: str = TEMPLATE_FILE) -> None:
    np.savez_compressed(path, **{k: v.astype(np.uint8) for k, v in templates.items()})


# ------------------------------------------------------------------- 识别入口
def classify_patch(patch: np.ndarray, templates: dict = None) -> Tuple[str, int, int]:
    """返回 (数字, 最小距离, 次小距离-最小距离 的余量)。"""
    templates = templates or load_templates()
    names = sorted(templates)
    stack = np.stack([templates[n] for n in names]).astype(bool)   # (10, H, W)
    best = np.full(len(names), 1 << 30, dtype=np.int64)
    for dy in range(-SHIFT_RY, SHIFT_RY + 1):
        for dx in range(-SHIFT_RX, SHIFT_RX + 1):
            aligned = shift_patch(patch, dy, dx)
            d = np.count_nonzero(stack != aligned, axis=(1, 2))
            np.minimum(best, d, out=best)
    order = np.argsort(best, kind="stable")
    i0, i1 = int(order[0]), int(order[1])
    return names[i0], int(best[i0]), int(best[i1] - best[i0])


def solve_captcha(data: bytes, templates: dict = None) -> Tuple[str, List[dict]]:
    """识别验证码图片字节，返回 (4位字符串, 每位明细)。"""
    templates = templates or load_templates()
    slots = extract_slots(mask_from_bytes(data))
    chars, details = [], []
    for i, s in enumerate(slots):
        d, dist, margin = classify_patch(s, templates)
        chars.append(d)
        details.append({"pos": i, "digit": d, "dist": dist, "margin": margin,
                        "ink": int(s.sum())})
    return "".join(chars), details


def solve_captcha_file(path: str, templates: dict = None) -> Tuple[str, List[dict]]:
    with open(path, "rb") as fh:
        return solve_captcha(fh.read(), templates)


if __name__ == "__main__":
    import sys
    args = sys.argv[1:] or ["captcha_now.gif"]
    for a in args:
        code, det = solve_captcha_file(a)
        print("%-22s -> %s   %s" % (a, code,
                                    "  ".join("%s(d=%d,m=%d)" % (x["digit"], x["dist"], x["margin"])
                                              for x in det)))
