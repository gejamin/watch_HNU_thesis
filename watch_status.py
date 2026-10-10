# -*- coding: utf-8 -*-
"""
守护进程健康检查（供 DSH 看门狗定时任务调用）。

退出码:
    0 = 健康（心跳新鲜且 status="ok"）
    1 = 不健康（心跳文件缺失 / 过期 / status 非 ok）
    2 = 心跳文件存在但内容损坏

用法:
    python watch_status.py                 # 默认新鲜阈值 300 秒
    python watch_status.py --max-age 120
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_FILE = os.path.join(HERE, ".watch_heartbeat.json")


def main() -> int:
    ap = argparse.ArgumentParser(description="论文评阅书轮询守护进程健康检查")
    ap.add_argument("--max-age", type=int, default=300, help="心跳允许的最大年龄（秒）")
    args = ap.parse_args()

    if not os.path.exists(HEARTBEAT_FILE):
        print("UNHEALTHY 心跳文件不存在：%s（守护进程从未启动或已被清理）" % HEARTBEAT_FILE)
        return 1
    try:
        with open(HEARTBEAT_FILE, encoding="utf-8") as fh:
            hb = json.load(fh)
        updated_ts = float(hb["updated_ts"])
    except Exception as e:
        print("UNHEALTHY 心跳文件内容损坏：%s: %s" % (type(e).__name__, e))
        return 2

    age = time.time() - updated_ts
    status = hb.get("status", "?")
    healthy = (status == "ok") and (age <= args.max_age)

    print("%s pid=%s round=%s status=%s 心跳距今=%.0fs 阈值=%ds"
          % ("HEALTHY  " if healthy else "UNHEALTHY",
             hb.get("pid"), hb.get("round"), status, age, args.max_age))
    print("  心跳时间: %s   上次结论: %s" % (hb.get("updated_at"), hb.get("note")))
    if not healthy:
        if status != "ok":
            print("  原因: status=%s（守护进程报错/已停止）" % status)
        else:
            print("  原因: 心跳已过期 %.0fs > %ds，守护进程疑似已退出" % (age, args.max_age))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
