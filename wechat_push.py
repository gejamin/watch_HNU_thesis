# -*- coding: utf-8 -*-
"""
通过 DSH 微信插件 (dsh-wechat) 所用的 iLink 机器人接口推送消息。

凭据直接复用插件自己保存的登录态：
    ~/.dsh-wechat/auth/token.json   -> token / baseUrl / accountId
    ~/.dsh-wechat/state.json        -> 默认收件人 peerUserId
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

WECHAT_DIR = os.path.join(os.path.expanduser("~"), ".dsh-wechat")
TOKEN_FILE = os.path.join(WECHAT_DIR, "auth", "token.json")
STATE_FILE = os.path.join(WECHAT_DIR, "state.json")
CONFIG_FILE = os.path.join(WECHAT_DIR, "config.json")

CHANNEL_VERSION = "1.0.2"

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TYPE_TEXT = 1


class WeChatPushError(RuntimeError):
    pass


class WeChatRateLimited(WeChatPushError):
    """iLink 连续发送限流（HTTP 200 + ret=-2 / errmsg="prepare failed"）。"""


def _load_credentials():
    if not os.path.exists(TOKEN_FILE):
        raise WeChatPushError("未找到微信插件登录态：%s（请先在 DSH 设置→WeChat 扫码登录）" % TOKEN_FILE)
    with open(TOKEN_FILE, encoding="utf-8") as fh:
        tok = json.load(fh)
    token = (tok.get("token") or "").strip()
    if not token:
        raise WeChatPushError("微信插件登录态中没有 token，请重新扫码登录")
    base_url = (tok.get("baseUrl") or "https://ilinkai.weixin.qq.com").strip()

    peer = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                st = json.load(fh)
            peer = ((st.get("outbound") or {}).get("peerUserId")
                    or next(iter((st.get("users") or {}).keys()), None))
        except Exception:
            peer = None
    if not peer:
        peer = tok.get("userId")
    if not peer:
        raise WeChatPushError("无法确定微信收件人（state.json 中没有 peerUserId）")
    return token, base_url.rstrip("/"), peer


def _wechat_uin() -> str:
    """模拟插件里的随机 X-WECHAT-UIN。"""
    return base64.b64encode(str(secrets.randbits(32)).encode()).decode()


def _sanitize(text: str) -> str:
    """去掉落单的 UTF-16 代理项——iLink 会对这种文本返回 ret=-1。"""
    out = []
    i = 0
    while i < len(text):
        c = ord(text[i])
        if 0xD800 <= c <= 0xDBFF:
            nxt = ord(text[i + 1]) if i + 1 < len(text) else 0
            if 0xDC00 <= nxt <= 0xDFFF:
                out.append(text[i:i + 2])
                i += 2
                continue
            out.append("\uFFFD")
        elif 0xDC00 <= c <= 0xDFFF:
            out.append("\uFFFD")
        else:
            out.append(text[i])
        i += 1
    return "".join(out)


def split_text(text: str, max_len: int = 2000):
    """按行优先切分超长文本。"""
    if len(text) <= max_len:
        return [text]
    parts, remaining = [], text
    while remaining:
        if len(remaining) <= max_len:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return parts


def send_wechat(text: str, to_user: str = None, retries: int = 3, timeout: int = 20) -> dict:
    """推送纯文本到微信，返回服务端响应。超长文本自动分条。"""
    token, base_url, peer = _load_credentials()
    to_user = to_user or peer
    url = base_url + "/ilink/bot/sendmessage"
    results = []
    for chunk in split_text(_sanitize(text)):
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user,
                "client_id": "wechat-acp-" + secrets.token_hex(16),
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [{"type": ITEM_TYPE_TEXT, "text_item": {"text": chunk}}],
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "AuthorizationType": "ilink_bot_token",
                "Authorization": "Bearer " + token,
                "X-WECHAT-UIN": _wechat_uin(),
            })
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                data = json.loads(raw) if raw.strip() else {}
                ret, errcode = data.get("ret"), data.get("errcode")
                errmsg = str(data.get("errmsg") or "")
                if ret not in (None, 0) or errcode not in (None, 0):
                    # iLink 的"连续发送过快"限流：可退避重试
                    if ret == -2 or errcode == -2 or "prepare failed" in errmsg.lower():
                        raise WeChatRateLimited(
                            "iLink 限流(ret=%s errcode=%s errmsg=%r)" % (ret, errcode, errmsg))
                    # ret=-1 / invalid request 这类重试也不会成功，直接失败
                    raise WeChatPushError("iLink 拒绝发送: %s" % raw[:300])
                results.append(data)
                break
            except WeChatPushError as e:
                if not isinstance(e, WeChatRateLimited):
                    raise
                last = str(e)
            except urllib.error.HTTPError as e:
                last = "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])
            except Exception as e:                       # 网络抖动 -> 退避重试
                last = "%s: %s" % (type(e).__name__, e)
            if attempt < retries - 1:
                time.sleep(1.5 * (2 ** attempt))
        else:
            raise WeChatPushError("微信推送失败：%s" % last)
    return {"sent": len(results), "results": results, "to": to_user}


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "DSH 微信推送自检：通道正常。"
    print(json.dumps(send_wechat(msg), ensure_ascii=False, indent=2))
