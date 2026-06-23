from __future__ import annotations

import hashlib
import json
import re
import ssl
from datetime import datetime, timedelta, timezone
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
    COURSES_INDEX_URL = "https://courses.zju.edu.cn/user/index"
    COURSES_TASKS_URL = "https://courses.zju.edu.cn/api/todos"
    ZDBK_HEADERS = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://zdbk.zju.edu.cn/jwglxt/xtgl/index_initMenu.html",
    }
    TERM_XQM = {
        0: "1|秋",
        1: "1|冬",
        2: "2|春",
        3: "2|夏",
        4: "2|春",
        5: "2|夏",
    }
    TERM_LABEL = {
        0: "秋",
        1: "冬",
        2: "春",
        3: "夏",
        4: "春",
        5: "夏",
    }
    TERM_CANONICAL = {
        0: 0,
        1: 1,
        2: 2,
        3: 3,
        4: 2,
        5: 3,
    }

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
        self.last_http_status: dict[str, int] = {}
        self.last_raw_counts: dict[str, int] = {}
        self.last_converted_counts: dict[str, int] = {}
        self.last_format_issues: list[dict[str, str]] = []

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

    def get_classes(self, academic_year: str, term: int) -> list[dict[str, Any]]:
        xqm = self._class_term_query(term)
        response = self.request_zdbk(
            "POST",
            self.TIMETABLE_URL,
            headers=self.ZDBK_HEADERS,
            data={"xnm": academic_year, "xqm": xqm, "captcha_value": ""},
        )
        self.last_http_status["classes"] = int(response.status_code)
        payload = self._json_payload(response, source_name="课表")
        if payload is None:
            self.last_raw_counts["classes"] = 0
            self.last_converted_counts["classes"] = 0
            return []
        if not isinstance(payload, dict) or "kbList" not in payload:
            raise ZdbkError("response_format", "教务系统课表返回格式异常。")

        raw_classes = payload.get("kbList")
        if raw_classes is None:
            self.last_raw_counts["classes"] = 0
            self.last_converted_counts["classes"] = 0
            return []
        if not isinstance(raw_classes, list):
            raise ZdbkError("response_format", "教务系统课表列表格式异常。")

        parsed: list[dict[str, Any]] = []
        for item in raw_classes:
            class_item = self._parse_class_item(item, target_term=term)
            if class_item is not None:
                parsed.append(class_item)

        self.last_raw_counts["classes"] = len(raw_classes)
        self.last_converted_counts["classes"] = len(parsed)
        return parsed

    def get_exams(self) -> list[dict[str, Any]]:
        self.last_format_issues = []
        response = self.request_zdbk(
            "POST",
            self.EXAMS_URL,
            headers=self.ZDBK_HEADERS,
        )
        self.last_http_status["exams"] = int(response.status_code)
        payload = self._json_payload(response, source_name="考试")
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ZdbkError("response_format", "教务系统考试返回格式异常。")

        raw_exams = payload["items"]
        parsed: list[dict[str, Any]] = []
        for item in raw_exams:
            if isinstance(item, dict) and item.get("xkkh"):
                parsed.extend(self._parse_exam_item(item))

        self.last_raw_counts["exams"] = len(raw_exams)
        self.last_converted_counts["exams"] = len(parsed)
        return parsed

    def get_learning_tasks(self) -> list[dict[str, Any]]:
        self._ensure_courses_session()
        response = self.session.get(
            self.COURSES_TASKS_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": self.COURSES_INDEX_URL,
            },
            timeout=self.timeout,
        )
        self._raise_for_http_status(response)
        payload = response.json()
        todo_list = payload.get("todo_list") if isinstance(payload, dict) else None
        if not isinstance(todo_list, list):
            raise ZdbkError("response_format", "学在浙大任务接口返回格式异常。")
        return [
            item
            for item in todo_list
            if isinstance(item, dict) and item.get("is_student") is True
        ]

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

    def _ensure_courses_session(self) -> None:
        if not self.has_cookie("iPlanetDirectoryPro"):
            raise ZdbkError("auth_session", "统一身份认证会话缺失，无法获取学在浙大任务。")
        if self.has_cookie("session", "courses.zju.edu.cn"):
            return

        next_url = self.COURSES_INDEX_URL
        seen: set[str] = set()
        try:
            for _ in range(16):
                response = self.session.get(next_url, timeout=self.timeout, allow_redirects=False)
                if 300 <= response.status_code < 400:
                    location = response.headers.get("Location", "").strip()
                    if not location:
                        break
                    next_url = urljoin(response.url, location)
                    if next_url in seen:
                        break
                    seen.add(next_url)
                    continue
                self._raise_for_http_status(response)
                if self.has_cookie("session", "courses.zju.edu.cn"):
                    return
                break
        except requests.Timeout as exc:
            raise ZdbkError("timeout", "学在浙大访问超时，请稍后重试。") from exc
        except requests.RequestException as exc:
            raise ZdbkError(
                "upstream_http",
                "学在浙大暂时不可用，请稍后重试。",
                str(exc),
            ) from exc

        if not self.has_cookie("session", "courses.zju.edu.cn"):
            raise ZdbkError("auth_session", "未能获取学在浙大学习平台 session。")

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

    def _json_payload(self, response: requests.Response, source_name: str) -> Any:
        text = str(getattr(response, "text", "") or "").strip()
        if "captcha_error" in text:
            raise ZdbkError("captcha_required", "教务系统要求验证码，当前版本暂不支持自动填写。")
        if text == "null":
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ZdbkError(
                "response_format",
                f"教务系统{source_name}返回不是有效 JSON。",
            ) from exc

    def _class_term_query(self, term: int) -> str:
        try:
            return self.TERM_XQM[int(term)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ZdbkError(
                "response_format",
                "课表学期编号不受支持，无法请求教务系统。",
                f"Unsupported term: {term!r}",
            ) from exc

    def _parse_class_item(self, item: Any, target_term: int) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        if item.get("kcb") is None or str(item.get("sfyjskc") or "") == "1":
            return None

        label = self.TERM_LABEL[self.TERM_CANONICAL[int(target_term)]]
        short_term = str(item.get("xxq") or "")
        if short_term and label not in short_term:
            return None

        try:
            day_number = int(str(item.get("xqj") or ""))
            initial_period = int(str(item.get("djj") or ""))
            duration = int(str(item.get("skcd") or ""))
        except ValueError:
            return None
        if day_number < 1 or duration < 1:
            return None

        name, teacher, location = self._parse_kcb(str(item.get("kcb") or ""))
        if not name:
            return None

        week_arrangement_raw = str(item.get("dsz") or "").strip()
        week_arrangement = "normal"
        if week_arrangement_raw == "0":
            week_arrangement = "odd"
        elif week_arrangement_raw == "1":
            week_arrangement = "even"

        term_arrangements = self._term_arrangements_from_short_term(short_term)
        if not term_arrangements:
            term_arrangements = [self.TERM_CANONICAL[int(target_term)]]

        return {
            "id": hashlib.sha1(
                json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "name": name,
            "teacher": teacher,
            "location": location,
            "course_code": str(item.get("kch") or item.get("kcdm") or "").strip(),
            "day_number": day_number,
            "start_period": initial_period,
            "end_period": initial_period + duration - 1,
            "week_arrangement": week_arrangement,
            "week_numbers": self._parse_week_numbers(item),
            "term_arrangements": term_arrangements,
        }

    def _parse_kcb(self, text: str) -> tuple[str, str, str]:
        normalized = (
            text.replace("<br/>", "<br>")
            .replace("<br />", "<br>")
            .replace("\r", "")
            .replace("\n", "")
        )
        parts = [
            re.sub(r"<[^>]+>", "", part).strip()
            for part in normalized.split("<br>")
        ]
        parts = [part for part in parts if part and part != "zwf"]
        name = parts[0] if parts else ""
        teacher = parts[2] if len(parts) >= 3 else ""
        location = parts[3] if len(parts) >= 4 else ""
        return (
            name.replace("(", "（").replace(")", "）"),
            teacher,
            location,
        )

    def _term_arrangements_from_short_term(self, text: str) -> list[int]:
        mapping = (("秋", 0), ("冬", 1), ("春", 2), ("夏", 3))
        return [term for label, term in mapping if label in text]

    def _parse_week_numbers(self, item: dict[str, Any]) -> list[int]:
        for start_key, end_key in (
            ("qsz", "jsz"),
            ("ksz", "jsz"),
            ("qszc", "jszc"),
            ("startWeek", "endWeek"),
            ("start_week", "end_week"),
        ):
            start = self._coerce_week_number(item.get(start_key))
            end = self._coerce_week_number(item.get(end_key))
            if start and end:
                lo, hi = sorted((start, end))
                return list(range(lo, hi + 1))

        for key in (
            "zcd",
            "zc",
            "zcs",
            "zcsm",
            "skzc",
            "skzcs",
            "week",
            "weeks",
            "weekNumbers",
            "week_numbers",
        ):
            weeks = self._parse_week_numbers_text(item.get(key))
            if weeks:
                return weeks
        return []

    def _parse_exam_item(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for agenda_key, location_key, seat_key, label in (
            ("qzkssj", "qzksdd", "qzzwxh", "期中考试"),
            ("kssj", "jsmc", "zwxh", "期末考试"),
        ):
            agenda = str(item.get(agenda_key) or "").strip()
            if not agenda:
                continue
            parsed = self._parse_exam_agenda(agenda)
            if not parsed:
                self.last_format_issues.append(
                    {
                        "code": "exam_time",
                        "message": f"{label}时间无法解析，已跳过该条。",
                    }
                )
                continue
            start_at, end_at = parsed
            seat = str(item.get(seat_key) or "").strip()
            description = f"座位号：{seat}" if seat else ""
            result.append(
                {
                    "id": hashlib.sha1(
                        f"{item.get('xkkh','')}|{agenda}|{label}".encode(
                            "utf-8",
                            errors="ignore",
                        )
                    ).hexdigest(),
                    "name": f"{str(item.get('kcmc') or '').strip()} {label}".strip(),
                    "location": str(item.get(location_key) or "").strip(),
                    "description": description,
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat(),
                }
            )
        return result

    def _parse_exam_agenda(self, text: str) -> tuple[datetime, datetime] | None:
        match = re.search(
            r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})[:：](\d{2})\D+(\d{1,2})[:：](\d{2})",
            text,
        )
        if not match:
            return None
        year, month, day, shour, sminute, ehour, eminute = [int(part) for part in match.groups()]
        tz = timezone(timedelta(hours=8))
        try:
            start_at = datetime(year, month, day, shour, sminute, tzinfo=tz)
            end_at = datetime(year, month, day, ehour, eminute, tzinfo=tz)
        except ValueError:
            return None
        if end_at <= start_at:
            return None
        return start_at, end_at

    def _coerce_week_number(self, value: Any) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        match = re.search(r"\d{1,2}", text)
        if not match:
            return None
        number = int(match.group(0))
        return number if 1 <= number <= 30 else None

    def _parse_week_numbers_text(self, value: Any) -> list[int]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            numbers = {
                number
                for item in value
                for number in self._parse_week_numbers_text(item)
            }
            return sorted(numbers)

        text = str(value).strip()
        if not text:
            return []
        text = text.replace("－", "-").replace("—", "-").replace("~", "-").replace("，", ",")

        numbers: set[int] = set()
        for start_raw, end_raw in re.findall(r"(\d{1,2})\s*-\s*(\d{1,2})", text):
            start, end = int(start_raw), int(end_raw)
            lo, hi = sorted((start, end))
            if 1 <= lo <= 30 and 1 <= hi <= 30:
                numbers.update(range(lo, hi + 1))

        text_without_ranges = re.sub(r"\d{1,2}\s*-\s*\d{1,2}", " ", text)
        for raw in re.findall(r"\d{1,2}", text_without_ranges):
            number = int(raw)
            if 1 <= number <= 30:
                numbers.add(number)
        return sorted(numbers)

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
