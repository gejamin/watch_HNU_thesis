# -*- coding: utf-8 -*-
"""
湖南大学研究生管理信息系统 —— 论文评阅书状态检查 + 微信推送

做什么
------
1. 登录 http://yjsxt.hnu.edu.cn/gmis/.../home/stulogin
   （若本地已保存有效会话则直接复用，否则用 学号+密码+RSA+验证码OCR 登录）；
2. 在学生端首页（/student/default/index）与学生菜单中定位「论文评阅书」入口；
3. 拉取该页 datagrid 的数据接口，判断是**有论文评阅书**还是**暂无数据**；
4. 通过 DSH 微信插件（iLink 机器人）把结论推送到微信。

用法
----
    python check_thesis.py                 # 检查 + 推送微信
    python check_thesis.py --no-push       # 只检查，打印结果
    python check_thesis.py --force-login   # 忽略缓存会话，强制重新登录
    python check_thesis.py --quiet         # 只在状态变化时推送（需配合 state 文件）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import captcha_ocr
import hnu
import wechat_push

# --------------------------------------------------------------- 登录凭据（环境变量）
# 凭据不写进代码：运行时从环境变量读取，避免学号/密码进入版本库。
#   $env:HNU_USER_ID  = "你的学号"
#   $env:HNU_PASSWORD = "你的密码"
USER_ID = (os.environ.get("HNU_USER_ID") or "").strip()
PASSWORD = (os.environ.get("HNU_PASSWORD") or "").strip()


def _require_credentials() -> None:
    """缺少凭据时给出明确提示后退出（若已有缓存会话则无需登录，故不强制）。"""
    missing = [n for n, v in (("HNU_USER_ID", USER_ID), ("HNU_PASSWORD", PASSWORD)) if not v]
    if missing:
        raise SystemExit(
            "缺少登录凭据：%s\n"
            "请先设置环境变量再运行，例如：\n"
            '    $env:HNU_USER_ID  = "你的学号"\n'
            '    $env:HNU_PASSWORD = "你的密码"\n'
            "（HNU_PASSWORD 请按你的密码原文填写，程序内部会自动做 RSA 加密）"
            % ", ".join(missing))

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".thesis_state.json")
LOG_FILE = os.path.join(HERE, "check_thesis.log")
GUARD_FILE = os.path.join(HERE, ".login_guard.json")

MAX_LOGIN_ATTEMPTS_PER_HOUR = 8      # 熔断阈值：1 小时内最多真正尝试登录 8 次

INDEX_PATH = "student/default/index"
MENU_PATH = "student/default/MenuleftInfo"


class LoginRateLimited(hnu.HnuError):
    """1 小时内登录尝试次数超限，主动熔断以保护账号。"""


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ------------------------------------------------------- 登录频率熔断（保护账号）
def _recent_login_attempts() -> list:
    if not os.path.exists(GUARD_FILE):
        return []
    try:
        data = json.load(open(GUARD_FILE, encoding="utf-8"))
    except Exception:
        return []
    cutoff = time.time() - 3600
    return [t for t in (data.get("attempts") or []) if t > cutoff]


def _record_login_attempt() -> None:
    attempts = _recent_login_attempts() + [time.time()]
    with open(GUARD_FILE, "w", encoding="utf-8") as fh:
        json.dump({"attempts": attempts[-200:]}, fh)


# --------------------------------------------------------------------- 登录
def ensure_login(force: bool = False, max_attempts: int = 8) -> hnu.HnuClient:
    """复用已保存会话；失效或强制时用验证码 OCR 重新登录。"""
    if not force:
        client = hnu.HnuClient.load()
        if client is not None and client.is_logged_in():
            log("复用本地保存的登录会话（%s）" % client.root)
            return client
        log("本地会话不存在或已失效，需要重新登录")

    _require_credentials()  # 仅在真正需要登录时才要求提供凭据
    client = hnu.HnuClient()
    client.bootstrap()
    log("已获取新会话：%s" % client.root)

    if len(_recent_login_attempts()) >= MAX_LOGIN_ATTEMPTS_PER_HOUR:
        raise LoginRateLimited(
            "1 小时内已尝试登录 %d 次（上限 %d），为避免账号被锁主动熔断，请稍后再试"
            % (len(_recent_login_attempts()), MAX_LOGIN_ATTEMPTS_PER_HOUR))

    last_msg = ""
    for attempt in range(1, max_attempts + 1):
        img = client.fetch_captcha()
        code, details = captcha_ocr.solve_captcha(img)
        _record_login_attempt()
        ok, msg = client.login(USER_ID, PASSWORD, code)
        conf = " ".join("%s(d=%d,m=%d)" % (d["digit"], d["dist"], d["margin"]) for d in details)
        if ok:
            log("第 %d 次尝试登录成功（验证码识别=%s）" % (attempt, code))
            client.save()
            return client
        last_msg = msg
        log("第 %d 次尝试登录失败：msg=%r  识别=%s  [%s]" % (attempt, msg, code, conf))
        if "验证码" not in msg:
            # 不是验证码问题（如密码错误、账号锁定），继续重试没有意义
            raise hnu.HnuError("登录被拒绝：%s" % msg)
        # 服务器可能刷新了会话，重新取一次会话 URL 与验证码
        client.bootstrap()
    raise hnu.HnuError("连续 %d 次登录失败，最后一次信息：%s" % (max_attempts, last_msg))


# ------------------------------------------------------------- 论文评阅书检查
def find_review_menu(client: hnu.HnuClient):
    """在左侧菜单里找「论文评阅书」入口，返回 (菜单名, 相对路径)。"""
    r = client.s.post("%s/%s" % (client.root, MENU_PATH), timeout=client.timeout,
                      headers={"Referer": "%s/%s" % (client.root, INDEX_PATH),
                               "X-Requested-With": "XMLHttpRequest"})
    try:
        menus = r.json().get("menus") or []
    except Exception:
        return None
    found = []
    for top in menus:
        for item in (top.get("menus") or []):
            name = item.get("menuname") or ""
            if "评阅" in name:
                url = item.get("url") or ""
                m = re.search(r"\)\)/(.+)$", url)
                found.append((name, m.group(1) if m else url))
    # 优先精确匹配「论文评阅书」
    for f in found:
        if "论文评阅书" in f[0]:
            return f
    return found[0] if found else None


def extract_grid_url(html: str) -> str:
    """从页面 JS 里取出 easyui datagrid 的 url（即数据接口）。"""
    m = re.search(r"url\s*:\s*['\"]([^'\"]+)['\"]", html)
    return m.group(1) if m else ""


def fetch_grid_rows(client: hnu.HnuClient, page_path: str, grid_url: str):
    """调用 datagrid 数据接口，返回 (总条数, 行列表, 原始文本)。"""
    base = page_path.rsplit("/", 1)[0]
    target = grid_url if grid_url.startswith("http") else "%s/%s/%s" % (client.root, base, grid_url)
    rows_all, total = [], 0
    page = 1
    while page <= 20:
        r = client.s.post(target, data={"page": page, "rows": 100}, timeout=client.timeout,
                          headers={"Referer": "%s/%s" % (client.root, page_path),
                                   "X-Requested-With": "XMLHttpRequest"})
        txt = r.text.strip()
        if not txt:
            break
        try:
            data = json.loads(txt)
        except Exception:
            return None, [], txt
        if isinstance(data, list):
            rows_all.extend(data)
            break
        rows = data.get("rows") or []
        total = int(data.get("total") or len(rows))
        rows_all.extend(rows)
        if len(rows_all) >= total or not rows:
            break
        page += 1
    return total or len(rows_all), rows_all, ""


def check_thesis(client: hnu.HnuClient) -> dict:
    """检查论文评阅书状态，返回结构化结果。"""
    result = {
        "index_ok": False,
        "menu_name": None,
        "menu_path": None,
        "page_ok": False,
        "has_review": False,
        "empty_text": False,
        "count": 0,
        "rows": [],
        "note": "",
    }

    # 1) 学生端首页
    idx = client.get(INDEX_PATH)
    result["index_ok"] = "mainFrame" in idx.text
    log("学生端首页 %s -> %s (%d 字节)" % (INDEX_PATH, idx.status_code, len(idx.text)))

    # 2) 菜单里定位论文评阅书入口
    menu = find_review_menu(client)
    if menu:
        result["menu_name"], result["menu_path"] = menu
        log("菜单中定位到「%s」-> %s" % (menu[0], menu[1]))
    else:
        result["menu_path"] = "student/bygl/lwpyssc"      # 兜底：已知路径
        result["menu_name"] = "论文评阅书上传"
        log("菜单未返回评阅入口，使用已知路径 %s" % result["menu_path"])

    # 3) 打开该页面，取数据接口
    page = client.get(result["menu_path"])
    html = page.text
    result["page_ok"] = page.status_code == 200
    result["empty_text"] = ("暂无数据" in html)
    grid_url = extract_grid_url(html)
    log("评阅书页面 %s -> %s，datagrid 接口=%r，页面内出现「暂无数据」=%s"
        % (result["menu_path"], page.status_code, grid_url, result["empty_text"]))

    # 4) 拉数据
    if grid_url:
        total, rows, raw = fetch_grid_rows(client, result["menu_path"], grid_url)
        if raw and "暂无数据" in raw:
            result["empty_text"] = True
        if rows:
            result["has_review"] = True
            result["count"] = len(rows)
            result["rows"] = rows
            log("数据接口返回 %d 条记录" % len(rows))
        else:
            log("数据接口返回空（即页面显示「暂无数据」）")
    else:
        # 没有 datagrid：退化为直接在 HTML 里找表格行
        body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
        cells = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
        data_rows = [c for c in cells if c.count("<td") >= 2]
        result["has_review"] = bool(data_rows)
        result["count"] = len(data_rows)
        log("未找到 datagrid 接口，页面表格行数=%d" % len(data_rows))
    return result


def summarize(result: dict) -> str:
    """把检查结果整理成适合微信阅读的文本。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    head = "【湖南大学研究生系统】论文评阅书检查"
    body = ["学号：%s" % USER_ID, "时间：%s" % ts,
            "入口：%s" % (result.get("menu_name") or "论文评阅书")]
    if not result["index_ok"]:
        body.append("结果：⚠️ 未能进入学生端首页，请检查账号或系统状态")
    elif result["has_review"]:
        body.append("结果：✅ 有论文评阅书（共 %d 条）" % result["count"])
        for i, row in enumerate(result["rows"][:10], 1):
            parts = []
            for key, label in (("lwsscs", "第几次送审"), ("ztmc", "状态"), ("xm", "姓名"),
                               ("xh", "学号")):
                if row.get(key) not in (None, ""):
                    parts.append("%s=%s" % (label, row[key]))
            extra = "、".join(parts) if parts else json.dumps(row, ensure_ascii=False)[:80]
            body.append("  %d) %s" % (i, extra))
        body.append("可登录系统「%s」查看/下载。" % (result.get("menu_name") or "论文评阅书上传"))
    else:
        body.append("结果：📭 暂无数据（系统里还没有可查看/下载的论文评阅书）")
    return head + "\n" + "\n".join(body)


# ------------------------------------------------------------------------ 主流程
def perform_check(force_login: bool = False, push: bool = True,
                  only_change: bool = False, notify_error: bool = True,
                  only_when_found: bool = False) -> dict:
    """
    执行一次完整检查（可被 CLI 与后台守护进程复用）。

    推送策略（三选一，优先级 only_when_found > only_change > 总是推送）：
      * only_when_found=True —— **只在发现论文评阅书时才推送**（推荐；等到有记录才通知一次，
        记录条数/状态再变化时会再通知一次，期间一直是"暂无数据"则完全静默）；
      * only_change=True     —— 任何结论变化都推送；
      * 两者都为 False        —— 每轮都推送。

    notify_error=False 时不推送失败通知（守护进程用它避免长期故障刷屏）。
    返回 dict：{"status": "ok"/"error", "result": ..., "text": ..., "pushed": bool,
               "changed": bool, "error": str}
    """
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            state = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            state = {}

    try:
        client = ensure_login(force=force_login)
        result = check_thesis(client)
    except Exception as e:
        msg = "【湖南大学研究生系统】论文评阅书检查失败\n时间：%s\n原因：%s: %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), type(e).__name__, e)
        log("检查出错：%s: %s" % (type(e).__name__, e))
        pushed = False
        if push and notify_error:
            try:
                wechat_push.send_wechat(msg)
                pushed = True
                log("已推送失败通知到微信")
            except Exception as pe:
                log("微信推送失败：%s" % pe)
        print(msg, flush=True)
        return {"status": "error", "result": None, "text": msg, "pushed": pushed,
                "changed": False, "error": "%s: %s" % (type(e).__name__, e)}

    text = summarize(result)
    log("检查结论：has_review=%s count=%d" % (result["has_review"], result["count"]))

    signature = "%s|%d" % (result["has_review"], result["count"])
    changed = state.get("signature") != signature

    if only_when_found:
        should_push = bool(result["has_review"]) and changed
        if not should_push:
            log("仅在发现评阅书时推送：当前 has_review=%s changed=%s，跳过推送"
                % (result["has_review"], changed))
    elif only_change:
        should_push = changed
        if not should_push:
            log("结论未变化（%s），跳过推送" % signature)
    else:
        should_push = True

    pushed = False
    if push and should_push:
        try:
            wechat_push.send_wechat(text)
            pushed = True
            log("已推送微信通知")
        except Exception as e:
            log("微信推送失败：%s: %s" % (type(e).__name__, e))

    if push:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"signature": signature, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "has_review": result["has_review"], "count": result["count"]}, fh,
                      ensure_ascii=False, indent=2)
    return {"status": "ok", "result": result, "text": text, "pushed": pushed,
            "changed": changed, "error": ""}


def main() -> int:
    ap = argparse.ArgumentParser(description="检查湖南大学研究生系统论文评阅书并推送微信")
    ap.add_argument("--no-push", action="store_true", help="只检查，不推送微信")
    ap.add_argument("--force-login", action="store_true", help="忽略缓存会话，强制重新登录")
    ap.add_argument("--only-change", action="store_true",
                    help="仅在结论与上次不同（或首次）时推送微信")
    args = ap.parse_args()

    out = perform_check(force_login=args.force_login, push=not args.no_push,
                        only_change=args.only_change)
    print("\n" + out["text"] + "\n")
    if out["status"] == "error":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
