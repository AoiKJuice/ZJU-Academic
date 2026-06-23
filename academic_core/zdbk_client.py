from __future__ import annotations

import re
import ssl
from typing import Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter


class LegacySSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        try:
            context.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


class ZdbkError(RuntimeError):
    def __init__(self, code: str, user_message: str, technical_message: str = ""):
        super().__init__(technical_message or user_message)
        self.code = code
        self.user_message = user_message
        self.technical_message = technical_message or user_message


class ZdbkClient:
    PUBKEY_URL = "https://zjuam.zju.edu.cn/cas/v2/getPubKey"
    SSO_URL = (
        "https://zjuam.zju.edu.cn/cas/login?"
        "service=https%3A%2F%2Fzdbk.zju.edu.cn%2Fjwglxt%2Fxtgl%2Flogin_ssologin.html"
    )
    TIMETABLE_URL = "https://zdbk.zju.edu.cn/jwglxt/kbcx/xskbcx_cxXsKb.html"
    EXAMS_URL = (
        "https://zdbk.zju.edu.cn/jwglxt/xskscx/kscx_cxXsgrksIndex.html?"
        "doType=query&queryModel.showCount=5000"
    )

    def __init__(
        self,
        username: str,
        password: str,
        timeout: int = 20,
        session: requests.Session | None = None,
    ):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.mount("https://zdbk.zju.edu.cn/", LegacySSLAdapter())
        self.session.mount("https://courses.zju.edu.cn/", LegacySSLAdapter())
        self.session.mount("https://identity.zju.edu.cn/", LegacySSLAdapter())
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                )
            }
        )

    def login(self) -> None:
        self._login_cas(self.SSO_URL)
        if not self.has_cookie("iPlanetDirectoryPro"):
            raise ZdbkError(
                "auth_session",
                "统一身份认证登录后没有返回有效会话，请稍后重试。",
                "ZJUAM cookie iPlanetDirectoryPro missing after CAS login",
            )
        self._ensure_zdbk_session()

    def request_zdbk(self, method: str, url: str, **kwargs) -> requests.Response:
        response = self._send(method, url, **kwargs)
        if not self.is_session_invalid_response(response):
            self._raise_for_http_status(response)
            return response

        self.login()
        response = self._send(method, url, **kwargs)
        if self.is_session_invalid_response(response):
            raise ZdbkError(
                "auth_session",
                "教务系统会话已失效，重新登录后仍无法访问，请稍后重试。",
                "ZDBK response still indicates invalid session after one re-login",
            )
        self._raise_for_http_status(response)
        return response

    def is_session_invalid_response(self, response: requests.Response) -> bool:
        if 300 <= int(getattr(response, "status_code", 0) or 0) < 400:
            return True
        location = str(getattr(response, "headers", {}).get("Location", ""))
        if "cas/login" in location or "login_ssologin" in location:
            return True
        text = str(getattr(response, "text", "") or "")
        return any(marker in text for marker in ("login_ssologin", "cas/login", "统一身份认证"))

    def has_cookie(
        self,
        name: str,
        domain_contains: str = "",
        path_contains: str = "",
    ) -> bool:
        for cookie in self.session.cookies:
            if cookie.name != name or not cookie.value:
                continue
            if domain_contains and domain_contains not in (cookie.domain or ""):
                continue
            if path_contains and path_contains not in (cookie.path or ""):
                continue
            return True
        return False

    def _login_cas(self, login_url: str) -> None:
        try:
            page = self.session.get(login_url, timeout=self.timeout)
            self._raise_for_http_status(page)
            match = re.search(r'name=["\']execution["\']\s+value=["\']([^"\']+)["\']', page.text)
            if not match:
                raise ZdbkError(
                    "response_format",
                    "统一身份认证页面格式异常，无法读取登录参数。",
                    "CAS execution field missing",
                )
            execution = match.group(1)

            pubkey_resp = self.session.get(self.PUBKEY_URL, timeout=self.timeout)
            self._raise_for_http_status(pubkey_resp)
            pubkey = pubkey_resp.json()
            encrypted = self._encrypt_password(
                password=self.password,
                modulus_hex=str(pubkey["modulus"]),
                exponent_hex=str(pubkey["exponent"]),
            )

            response = self.session.post(
                "https://zjuam.zju.edu.cn/cas/login",
                data={
                    "username": self.username,
                    "password": encrypted,
                    "authcode": "",
                    "execution": execution,
                    "_eventId": "submit",
                },
                timeout=self.timeout,
            )
            self._raise_for_http_status(response)
        except requests.Timeout as exc:
            raise ZdbkError("timeout", "学校统一身份认证访问超时，请稍后重试。") from exc
        except requests.RequestException as exc:
            raise ZdbkError(
                "upstream_http",
                "学校统一身份认证暂时不可用，请稍后重试。",
                str(exc),
            ) from exc

        text = str(getattr(response, "text", "") or "")
        if "用户名或密码错误" in text or "异常登录" in text:
            raise ZdbkError("auth_credentials", "统一身份认证登录失败：用户名或密码错误。")
        if "账号被锁定" in text:
            raise ZdbkError("auth_locked", "统一身份认证登录失败：账号已被锁定。")
        if "验证码" in text or "captcha" in text.lower():
            raise ZdbkError("captcha_required", "统一身份认证要求验证码，当前版本暂不支持自动填写。")

    def _ensure_zdbk_session(self) -> None:
        try:
            response = self.session.get(
                self.SSO_URL,
                timeout=self.timeout,
                allow_redirects=False,
            )
            self._raise_for_http_status(response)
            if 300 <= response.status_code < 400:
                location = response.headers.get("Location", "").strip()
                if not location:
                    raise ZdbkError(
                        "auth_session",
                        "教务系统没有返回有效跳转地址，请稍后重试。",
                        "ZDBK SSO redirect Location missing",
                    )
                target = urljoin(response.url, location)
                response = self.session.get(
                    target,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                self._raise_for_http_status(response)
        except requests.Timeout as exc:
            raise ZdbkError("timeout", "教务系统登录访问超时，请稍后重试。") from exc
        except requests.RequestException as exc:
            raise ZdbkError(
                "upstream_http",
                "教务系统暂时不可用，请稍后重试。",
                str(exc),
            ) from exc

        if not self.has_cookie("JSESSIONID", "zdbk.zju.edu.cn", "/jwglxt"):
            raise ZdbkError(
                "auth_session",
                "教务系统登录后没有返回有效会话，请稍后重试。",
                "ZDBK cookie JSESSIONID missing for /jwglxt",
            )
        if not self.has_cookie("route", "zdbk.zju.edu.cn", "/jwglxt"):
            raise ZdbkError(
                "auth_session",
                "教务系统登录后没有返回路由会话，请稍后重试。",
                "ZDBK cookie route missing for /jwglxt",
            )

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        try:
            if method.upper() == "GET":
                return self.session.get(url, **kwargs)
            if method.upper() == "POST":
                return self.session.post(url, **kwargs)
        except requests.Timeout as exc:
            raise ZdbkError("timeout", "学校接口访问超时，请稍后重试。") from exc
        except requests.RequestException as exc:
            raise ZdbkError(
                "upstream_http",
                "学校接口暂时不可用，请稍后重试。",
                str(exc),
            ) from exc
        raise ValueError(f"Unsupported HTTP method: {method}")

    def _raise_for_http_status(self, response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = getattr(response, "status_code", "")
            raise ZdbkError(
                "upstream_http",
                f"学校接口返回 HTTP {status_code}，请稍后重试。",
                str(exc),
            ) from exc

    @staticmethod
    def _encrypt_password(password: str, modulus_hex: str, exponent_hex: str) -> str:
        payload_hex = password.encode("utf-8").hex()
        message = int(payload_hex, 16)
        modulus = int(modulus_hex, 16)
        exponent = int(exponent_hex, 16)
        cipher = pow(message, exponent, modulus)
        return format(cipher, "x").rjust(128, "0")
