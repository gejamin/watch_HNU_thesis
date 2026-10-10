# -*- coding: utf-8 -*-
"""
离线构建验证码识别模板，并报告准确率。

数据来源:
  * caps/c000.gif ... : 40 张样本，其中前 24 张已人工标注(LABELS)；
  * captcha_now.gif   : 已用其成功登录，答案 3768（服务器确认的 ground truth）。
"""
import glob
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np

import captcha_ocr as ocr

# 人工标注(见 montage_0.png / montage_1.png)
LABELS = {
    "c000": "7488", "c001": "4472", "c002": "2793", "c003": "7038",
    "c004": "3305", "c005": "1626", "c006": "7321", "c007": "2642",
    "c008": "0933", "c009": "8214", "c010": "1545", "c011": "8840",
    "c012": "3821", "c013": "1112", "c014": "8106", "c015": "6487",
    "c016": "0452", "c017": "8773", "c018": "4050", "c019": "0048",
    "c020": "9385", "c021": "6390", "c022": "1611", "c023": "9932",
}
EXTRA = {"captcha_now.gif": "3768"}      # 服务器已验证正确


def patches_of(path):
    return ocr.extract_slots(ocr.mask_from_bytes(open(path, "rb").read()))


def main():
    files = sorted(glob.glob("caps/*.gif"))
    print("样本总数:", len(files))

    # ---------- 1. 收集已标注样本的字符位图 ----------
    buckets = {str(d): [] for d in range(10)}
    used = 0
    for f in files:
        key = os.path.splitext(os.path.basename(f))[0]
        ans = LABELS.get(key)
        if not ans:
            continue
        for pat, ch in zip(patches_of(f), ans):
            buckets[ch].append(pat)
        used += 1
    for name, ans in EXTRA.items():
        if os.path.exists(name):
            for pat, ch in zip(patches_of(name), ans):
                buckets[ch].append(pat)
            used += 1
    print("用于建模板的标注样本:", used, " 每个数字样本数:",
          {k: len(v) for k, v in buckets.items()})

    missing = [k for k, v in buckets.items() if not v]
    if missing:
        print("!! 缺少数字样本:", missing)
        return 1

    # ---------- 2. 生成模板(对齐 + 多数投票) ----------
    templates = {}
    for d, pats in buckets.items():
        aligned = ocr.build_digit_template(pats)
        stack = np.stack(aligned).astype(np.int16)
        templates[d] = (stack.mean(axis=0) >= 0.5)
        consistency = float((stack == templates[d]).mean())
        print("数字 %s: n=%2d  模板一致性=%.4f" % (d, len(pats), consistency))

    # ---------- 3. 模板两两可分性 ----------
    print("\n模板间最小汉明距离矩阵(越小越危险):")
    ds = sorted(templates)
    worst = []
    for i, a in enumerate(ds):
        row = []
        for j, b in enumerate(ds):
            if i == j:
                row.append("  --")
                continue
            dist, _, _ = ocr.best_shift(templates[a], templates[b])
            row.append("%4d" % dist)
            if i < j:
                worst.append((dist, a, b))
        print("   %s %s" % (a, "".join(row)))
    worst.sort()
    print("   最易混淆对:", worst[:4])

    # ---------- 4. 在已标注样本上评估 ----------
    print("\n在 %d 张已标注样本上的识别结果:" % used)
    ok = 0
    total = 0
    for f in files:
        key = os.path.splitext(os.path.basename(f))[0]
        ans = LABELS.get(key)
        if not ans:
            continue
        code, det = ocr.solve_captcha(open(f, "rb").read(), templates)
        total += 1
        ok += (code == ans)
        mark = "OK " if code == ans else "BAD"
        print("   %s %s 真实=%s 识别=%s   %s" % (mark, key, ans, code,
              " ".join("%s/%d/%d" % (x["digit"], x["dist"], x["margin"]) for x in det)))
    print("   ==> 整图准确率 %d/%d = %.1f%%" % (ok, total, 100.0 * ok / max(1, total)))

    # ---------- 5. 未标注样本的自洽性检查 ----------
    print("\n未标注样本(无 ground truth，看距离是否都很低):")
    for f in files:
        key = os.path.splitext(os.path.basename(f))[0]
        if key in LABELS:
            continue
        code, det = ocr.solve_captcha(open(f, "rb").read(), templates)
        print("   %s -> %s   %s" % (key, code,
              " ".join("%s(d=%d,m=%d)" % (x["digit"], x["dist"], x["margin"]) for x in det)))

    ocr.save_templates(templates)
    print("\n模板已保存 ->", ocr.TEMPLATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
