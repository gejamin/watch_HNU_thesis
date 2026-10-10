# -*- coding: utf-8 -*-
"""
论文评阅书「高频轮询」守护进程。

为什么要单独一个进程？
----------------------
DSH 的定时任务每次触发都会唤醒一次 Agent 对话。如果直接让它每分钟触发一次，
一天就是 1440 轮对话，token 和会话记录都会被刷爆。所以正确做法是：

  * 本进程常驻后台，按 interval 秒轮询，**不消耗任何 token**；
  * 默认**每轮都推送微信**（含"暂无数据"，按用户要求）；
    加 --only-change 可改回"仅结论变化时推送"；
  * DSH 定时任务只当「看门狗」，定期检查本进程是否还活着（见 README）。

负载控制
--------
* 会话复用：登录成功后把 cookies + 会话 URL 存到 .hnu_session.json，
  之后每次轮询只是几次 GET/POST，**不会反复登录**；
* 频繁的请求本身会让 ASP.NET 的滑动会话不过期，因此登录次数通常≈1 次/天；
* 登录熔断：1 小时内真正尝试登录超过 8 次即停止重试（check_thesis 里的保护）；
* 连续失败自动指数退避，最长退避 30 分钟。

用法
----
    python watch_thesis.py                      # 默认 60 秒一轮
    python watch_thesis.py --interval 300       # 5 分钟一轮
    python watch_thesis.py --max-rounds 3       # 只跑 3 轮（自测用）
    python watch_thesis.py --once               # 只跑一轮

停止：创建 .watch_stop 文件即可优雅退出。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import check_thesis as ct

HERE = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_FILE = os.path.join(HERE, ".watch_heartbeat.json")
STOP_FILE = os.path.join(HERE, ".watch_stop")

INTERVAL = 60               # 轮询间隔（秒），main() 会按参数覆盖
MAX_BACKOFF = 1800          # 连续失败时最长退避 30 分钟


def write_heartbeat(round_no: int, status: str, note: str = "") -> None:
    payload = {
        "pid": os.getpid(),
        "round": round_no,
        "status": status,
        "note": note,
        "interval": INTERVAL,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_ts": time.time(),
    }
    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def main() -> int:
    global INTERVAL
    ap = argparse.ArgumentParser(description="论文评阅书轮询守护进程")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔（秒），默认 60")
    ap.add_argument("--max-rounds", type=int, default=0, help="最多轮询多少轮，0=不限")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出")
    ap.add_argument("--only-change", action="store_true",
                    help="任何结论变化都推送（默认只在发现评阅书时推送）")
    ap.add_argument("--always-push", action="store_true",
                    help="每轮都推送（含'暂无数据'，会非常吵）")
    args = ap.parse_args()
    INTERVAL = max(30, args.interval)
    if args.once:
        args.max_rounds = 1
    only_when_found = not (args.only_change or args.always_push)

    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    ct.log("=== 轮询守护进程启动 pid=%d interval=%ds max_rounds=%s ==="
           % (os.getpid(), INTERVAL, args.max_rounds or "∞"))
    rounds = 0
    failures = 0
    sleep_s = INTERVAL

    while True:
        if os.path.exists(STOP_FILE):
            ct.log("检测到 .watch_stop，守护进程退出")
            break
        rounds += 1
        started = time.time()
        try:
            # 默认只在发现评阅书时推送；失败通知只在连续失败的第一轮推送，避免刷屏
            out = ct.perform_check(force_login=False, push=True,
                                   only_change=args.only_change,
                                   only_when_found=only_when_found,
                                   notify_error=(failures == 0))
            if out["status"] == "ok":
                failures = 0
                sleep_s = INTERVAL
                write_heartbeat(rounds, "ok",
                                "has_review=%s count=%s changed=%s pushed=%s"
                                % (out["result"]["has_review"], out["result"]["count"],
                                   out["changed"], out["pushed"]))
            else:
                failures += 1
                sleep_s = min(INTERVAL * (2 ** failures), MAX_BACKOFF)
                write_heartbeat(rounds, "error", out["error"])
                ct.log("第 %d 轮失败（连续 %d 次），%d 秒后重试" % (rounds, failures, sleep_s))
        except ct.LoginRateLimited as e:
            failures += 1
            sleep_s = MAX_BACKOFF
            write_heartbeat(rounds, "rate_limited", str(e))
            ct.log("登录熔断：%s —— 退避 %d 秒" % (e, sleep_s))
        except Exception as e:
            failures += 1
            sleep_s = min(INTERVAL * (2 ** failures), MAX_BACKOFF)
            write_heartbeat(rounds, "exception", "%s: %s" % (type(e).__name__, e))
            ct.log("第 %d 轮异常（连续 %d 次）：%s: %s，%d 秒后重试"
                   % (rounds, failures, type(e).__name__, e, sleep_s))

        if args.max_rounds and rounds >= args.max_rounds:
            ct.log("已达到 max_rounds=%d，退出" % args.max_rounds)
            break

        # 扣掉本轮耗时，保持稳定节奏
        elapsed = time.time() - started
        time.sleep(max(1.0, sleep_s - elapsed))

    write_heartbeat(rounds, "stopped", "进程已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
