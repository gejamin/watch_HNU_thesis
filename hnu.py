# -*- coding: utf-8 -*-
"""
湖南大学研究生管理信息系统 (yjsxt.hnu.edu.cn/gmis) 访问库。

要点
----
* 该系统采用 ASP.NET *cookieless* 会话：会话 ID 写在 URL 的 ``(S(...))`` 段里，
  所以会话必须连同 URL 一起保存 / 复用，不能只存 cookie。
* 登录接口 ``/home/stulogin_do`` 要求：
    - ``Password`` 用页面内嵌公钥做 RSA/PKCS#1 v1.5 加密后再 base64；
    - ``VeriCode`` 为图形验证码，服务端**强制校验**（错误返回 {"jg":"0","msg":"验证码错误"}）。
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Optional, Tuple

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

HOST = "http://yjsxt.hnu.edu.cn"
GMIS = HOST + "/gmis"
LOGIN_ENTRY = GMIS + "/(S(bwu02qn1cu4kcg44hmyivuqu))/home/stulogin"

SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hnu_session.json")

RE_SESSION = re.compile(r"/gmis/\(S\(([^)]+)\)\)")
RE_PUBKEY = re.compile(r'id="pubkey"[^>]*value=\'(.*?)\'', re.S)


class HnuError(RuntimeError):
    pass


class HnuClient:
    """一个可持久化的 gmis 客户端会话。"""

    def __init__(self, timeout: int = 25):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.root: Optional[str] = None      # .../gmis/(S(xxx))
        self.home: Optional[str] = None      # .../gmis/(S(xxx))/home
        self.pubkey_pem: Optional[str] = None

    # ------------------------------------------------------------------ 会话
    def bootstrap(self) -> str:
        """访问登录页，取得服务端分配的会话 URL。"""
        r = self.s.get(LOGIN_ENTRY, timeout=self.timeout)
        r.raise_for_status()
        self._absorb(r)
        if not self.root:
            raise HnuError("未能从 %s 解析出会话 ID（可能站点结构已变）" % r.url)
        return self.root

    def _absorb(self, r: requests.Response) -> None:
        """从任意响应里同步当前的会话 URL 与公钥。"""
        m = RE_SESSION.search(r.url)
        if m:
            self.root = "%s/gmis/(S(%s))" % (HOST, m.group(1))
            self.home = self.root + "/home"
        m = RE_PUBKEY.search(r.text or "")
        if m:
            self.pubkey_pem = m.group(1).strip()

    # ---------------------------------------------------------------- 持久化
    def save(self) -> None:
        data = {
            "root": self.root,
            "cookies": self.s.cookies.get_dict(),
            "pubkey": self.pubkey_pem,
        }
        with open(SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, timeout: int = 25) -> Optional["HnuClient"]:
        if not os.path.exists(SESSION_FILE):
            return None
        try:
            with open(SESSION_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return None
        if not data.get("root"):
            return None
        c = cls(timeout=timeout)
        c.root = data["root"]
        c.home = c.root + "/home"
        c.pubkey_pem = data.get("pubkey")
        for k, v in (data.get("cookies") or {}).items():
            c.s.cookies.set(k, v)
        return c

    # --------------------------------------------------------------- 验证码
    def fetch_captcha(self) -> bytes:
        """取一张新的验证码图片（GIF）。"""
        if not self.home:
            self.bootstrap()
        r = self.s.get(self.home + "/verificationcode?codetype=stucode",
                       timeout=self.timeout, headers={"Referer": self.home + "/stulogin"})
        r.raise_for_status()
        return r.content

    # ------------------------------------------------------------------ 登录
    def _encrypt(self, password: str) -> str:
        if not self.pubkey_pem:
            self.bootstrap()
        pub = serialization.load_pem_public_key(self.pubkey_pem.encode())
        ct = pub.encrypt(password.encode("utf-8"), padding.PKCS1v15())
        return base64.b64encode(ct).decode()

    def login(self, user_id: str, password: str, veri_code: str) -> Tuple[bool, str]:
        """提交登录。返回 (是否成功, 消息)。"""
        if not self.root:
            self.bootstrap()
        info = {
            "UserId": user_id,
            "Password": self._encrypt(password),
            "VeriCode": veri_code,
            "url": "",
            "city": "",
        }
        r = self.s.post(self.home + "/stulogin_do",
                        data={"json": json.dumps(info, ensure_ascii=False)},
                        timeout=self.timeout,
                        headers={"Referer": self.home + "/stulogin",
                                 "X-Requested-With": "XMLHttpRequest"})
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            raise HnuError("登录响应不是 JSON：%s" % r.text[:300])
        ok = str(data.get("jg")) == "1"
        return ok, (data.get("msg") or "")

    # ------------------------------------------------------------ 页面与状态
    def get(self, path: str) -> requests.Response:
        """按会话根路径取页面，path 形如 'student/default/index'。"""
        if not self.root:
            raise HnuError("会话未初始化")
        url = path if path.startswith("http") else "%s/%s" % (self.root, path.lstrip("/"))
        return self.s.get(url, timeout=self.timeout, headers={"Referer": self.root + "/"})

    def is_logged_in(self) -> bool:
        """访问学生首页，判断会话是否仍然有效（未登录会被弹回登录页）。"""
        try:
            r = self.get("student/default/index")
        except Exception:
            return False
        self._absorb(r)
        if "/home/stulogin" in r.url or "stulogin_do" in r.text:
            return False
        # 学生端框架页有这些特征；登录页则包含 btnlogin
        return "mainFrame" in r.text and 'id="btnlogin"' not in r.text
