import unittest
import json
from pathlib import Path

import requests
from requests.cookies import RequestsCookieJar

from academic_core.zdbk_client import ZdbkClient, ZdbkError


class FakeResponse:
    def __init__(
        self,
        status_code=200,
        text="",
        json_data=None,
        headers=None,
        url="https://example.invalid/",
        cookies=None,
    ):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.headers = headers or {}
        self.url = url
        self.cookies = RequestsCookieJar()
        for cookie in cookies or []:
            self.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

    def json(self):
        if isinstance(self._json_data, BaseException):
            raise self._json_data
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class ScriptedSession:
    def __init__(self):
        self.routes = []
        self.calls = []
        self.cookies = RequestsCookieJar()
        self.headers = {}

    def mount(self, *_args, **_kwargs):
        return None

    def add(self, method, url, response, **expected_kwargs):
        self.routes.append((method.upper(), url, response, expected_kwargs))

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def _request(self, method, url, **kwargs):
        if not self.routes:
            raise AssertionError(f"Unexpected {method} {url}")
        expected_method, expected_url, response, expected_kwargs = self.routes.pop(0)
        if expected_method != method:
            raise AssertionError(f"Expected {expected_method}, got {method}")
        if expected_url != url:
            raise AssertionError(f"Expected {expected_url}, got {url}")
        for key, value in expected_kwargs.items():
            if kwargs.get(key) != value:
                raise AssertionError(f"Expected {key}={value!r}, got {kwargs.get(key)!r}")
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        for cookie in response.cookies:
            self.cookies.set_cookie(cookie)
        return response


def cas_login_response():
    return FakeResponse(
        text='<input type="hidden" name="execution" value="e1s1" />',
        url=ZdbkClient.SSO_URL,
    )


def pubkey_response():
    return FakeResponse(
        json_data={
            "modulus": "f" * 128,
            "exponent": "10001",
        },
        url=ZdbkClient.PUBKEY_URL,
    )


def cas_submit_response():
    return FakeResponse(
        text="ok",
        url="https://zjuam.zju.edu.cn/cas/login",
        cookies=[
            {
                "name": "iPlanetDirectoryPro",
                "value": "zjuam-session",
                "domain": "zjuam.zju.edu.cn",
                "path": "/",
            }
        ],
    )


def zdbk_sso_redirect():
    return FakeResponse(
        status_code=302,
        headers={"Location": "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_ssologin.html?ticket=ST-1"},
        url=ZdbkClient.SSO_URL,
    )


def zdbk_session_response(include_route=True):
    cookies = [
        {
            "name": "JSESSIONID",
            "value": "zdbk-session",
            "domain": "zdbk.zju.edu.cn",
            "path": "/jwglxt",
        }
    ]
    if include_route:
        cookies.append(
            {
                "name": "route",
                "value": "zdbk-route",
                "domain": "zdbk.zju.edu.cn",
                "path": "/jwglxt",
            }
        )
    return FakeResponse(
        text="zdbk ok",
        url="https://zdbk.zju.edu.cn/jwglxt/xtgl/login_ssologin.html?ticket=ST-1",
        cookies=cookies,
    )


def add_successful_login(session, include_route=True):
    session.add("GET", ZdbkClient.SSO_URL, cas_login_response())
    session.add("GET", ZdbkClient.PUBKEY_URL, pubkey_response())
    session.add("POST", "https://zjuam.zju.edu.cn/cas/login", cas_submit_response())
    session.add("GET", ZdbkClient.SSO_URL, zdbk_sso_redirect(), allow_redirects=False)
    session.add(
        "GET",
        "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_ssologin.html?ticket=ST-1",
        zdbk_session_response(include_route=include_route),
        allow_redirects=False,
    )


def load_fixture(name):
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8"))


def json_response(payload):
    return FakeResponse(
        text=json.dumps(payload, ensure_ascii=False),
        json_data=payload,
        url="https://zdbk.zju.edu.cn/jwglxt/xtgl/index_initMenu.html",
    )


class ZdbkSessionTest(unittest.TestCase):
    def test_login_extracts_execution_encrypts_password_and_gets_zdbk_cookies(self):
        session = ScriptedSession()
        add_successful_login(session)

        client = ZdbkClient("alice", "plain-password", session=session)
        client.login()

        urls = [call["url"] for call in session.calls]
        self.assertEqual(urls[0], ZdbkClient.SSO_URL)
        self.assertEqual(urls[1], ZdbkClient.PUBKEY_URL)
        self.assertEqual(urls[3], ZdbkClient.SSO_URL)
        self.assertIn(
            "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_ssologin.html?ticket=ST-1",
            urls,
        )

        submit = session.calls[2]["kwargs"]["data"]
        self.assertEqual(submit["username"], "alice")
        self.assertEqual(submit["execution"], "e1s1")
        self.assertNotEqual(submit["password"], "plain-password")
        self.assertTrue(client.has_cookie("iPlanetDirectoryPro"))
        self.assertTrue(client.has_cookie("JSESSIONID", "zdbk.zju.edu.cn", "/jwglxt"))
        self.assertTrue(client.has_cookie("route", "zdbk.zju.edu.cn", "/jwglxt"))

    def test_missing_zdbk_cookie_raises_auth_session(self):
        session = ScriptedSession()
        add_successful_login(session, include_route=False)
        client = ZdbkClient("alice", "plain-password", session=session)

        with self.assertRaises(ZdbkError) as ctx:
            client.login()
        self.assertEqual(ctx.exception.code, "auth_session")

    def test_business_responses_can_be_identified_as_session_invalid(self):
        client = ZdbkClient("alice", "plain-password", session=ScriptedSession())

        self.assertTrue(
            client.is_session_invalid_response(
                FakeResponse(status_code=302, headers={"Location": ZdbkClient.SSO_URL})
            )
        )
        self.assertTrue(client.is_session_invalid_response(FakeResponse(text="login_ssologin")))
        self.assertTrue(client.is_session_invalid_response(FakeResponse(text="cas/login")))
        self.assertTrue(client.is_session_invalid_response(FakeResponse(text="统一身份认证")))
        self.assertFalse(client.is_session_invalid_response(FakeResponse(json_data={"ok": True})))

    def test_session_invalid_reauthenticates_once_then_stops(self):
        session = ScriptedSession()
        session.cookies.set("iPlanetDirectoryPro", "old", domain="zjuam.zju.edu.cn", path="/")
        session.cookies.set("JSESSIONID", "old", domain="zdbk.zju.edu.cn", path="/jwglxt")
        session.cookies.set("route", "old", domain="zdbk.zju.edu.cn", path="/jwglxt")
        session.add("POST", ZdbkClient.TIMETABLE_URL, FakeResponse(text="统一身份认证"))
        add_successful_login(session)
        session.add("POST", ZdbkClient.TIMETABLE_URL, FakeResponse(text="统一身份认证"))

        client = ZdbkClient("alice", "plain-password", session=session)
        with self.assertRaises(ZdbkError) as ctx:
            client.request_zdbk("POST", ZdbkClient.TIMETABLE_URL, data={"xnm": "2025-2026"})

        self.assertEqual(ctx.exception.code, "auth_session")
        business_calls = [
            call for call in session.calls
            if call["method"] == "POST" and call["url"] == ZdbkClient.TIMETABLE_URL
        ]
        self.assertEqual(len(business_calls), 2)


class ZdbkPayloadTest(unittest.TestCase):
    def test_get_classes_posts_current_zdbk_payload_and_converts_timetable(self):
        payload = load_fixture("zdbk_timetable.json")
        session = ScriptedSession()
        session.add("POST", ZdbkClient.TIMETABLE_URL, json_response(payload))
        client = ZdbkClient("alice", "plain-password", session=session)

        classes = client.get_classes("2025-2026", 3)

        call = session.calls[0]
        self.assertEqual(
            call["kwargs"]["data"],
            {"xnm": "2025-2026", "xqm": "2|夏", "captcha_value": ""},
        )
        self.assertEqual(call["kwargs"]["headers"], ZdbkClient.ZDBK_HEADERS)
        self.assertEqual(len(classes), 2)

        first = classes[0]
        self.assertEqual(first["name"], "样例课程甲")
        self.assertEqual(first["teacher"], "教师甲")
        self.assertEqual(first["location"], "样例教室A")
        self.assertEqual(first["course_code"], "FAKE101")
        self.assertEqual(first["day_number"], 2)
        self.assertEqual(first["start_period"], 3)
        self.assertEqual(first["end_period"], 4)
        self.assertEqual(first["week_arrangement"], "odd")
        self.assertEqual(first["week_numbers"], [1, 2, 3, 4])
        self.assertEqual(first["term_arrangements"], [3])

        second = classes[1]
        self.assertEqual(second["week_arrangement"], "even")
        self.assertEqual(second["week_numbers"], [2, 4, 6])
        self.assertNotIn("预置课程", {item["name"] for item in classes})
        self.assertNotIn("非本短学期课程", {item["name"] for item in classes})
        self.assertEqual(client.last_raw_counts["classes"], 4)
        self.assertEqual(client.last_converted_counts["classes"], 2)

    def test_get_classes_accepts_legacy_spring_and_summer_term_ids(self):
        for legacy_term, expected_xqm in ((4, "2|春"), (5, "2|夏")):
            with self.subTest(legacy_term=legacy_term):
                session = ScriptedSession()
                session.add("POST", ZdbkClient.TIMETABLE_URL, json_response({"kbList": []}))
                client = ZdbkClient("alice", "plain-password", session=session)

                self.assertEqual(client.get_classes("2025-2026", legacy_term), [])
                self.assertEqual(session.calls[0]["kwargs"]["data"]["xqm"], expected_xqm)

    def test_get_classes_handles_captcha_null_and_malformed_payloads(self):
        session = ScriptedSession()
        session.add("POST", ZdbkClient.TIMETABLE_URL, FakeResponse(text="captcha_error"))
        client = ZdbkClient("alice", "plain-password", session=session)
        with self.assertRaises(ZdbkError) as ctx:
            client.get_classes("2025-2026", 3)
        self.assertEqual(ctx.exception.code, "captcha_required")

        session = ScriptedSession()
        session.add("POST", ZdbkClient.TIMETABLE_URL, FakeResponse(text="null", json_data=None))
        client = ZdbkClient("alice", "plain-password", session=session)
        self.assertEqual(client.get_classes("2025-2026", 3), [])

        session = ScriptedSession()
        session.add("POST", ZdbkClient.TIMETABLE_URL, json_response({"unexpected": []}))
        client = ZdbkClient("alice", "plain-password", session=session)
        with self.assertRaises(ZdbkError) as ctx:
            client.get_classes("2025-2026", 3)
        self.assertEqual(ctx.exception.code, "response_format")

    def test_get_exams_reads_items_and_skips_unparseable_exam_dates(self):
        payload = load_fixture("zdbk_exams.json")
        session = ScriptedSession()
        session.add("POST", ZdbkClient.EXAMS_URL, json_response(payload))
        client = ZdbkClient("alice", "plain-password", session=session)

        exams = client.get_exams()

        self.assertEqual(session.calls[0]["kwargs"]["headers"], ZdbkClient.ZDBK_HEADERS)
        self.assertEqual(len(exams), 2)
        self.assertEqual(exams[0]["name"], "样例课程甲 期中考试")
        self.assertEqual(exams[0]["location"], "样例考场A")
        self.assertEqual(exams[0]["description"], "座位号：A01")
        self.assertEqual(exams[0]["start_at"], "2026-05-15T10:00:00+08:00")
        self.assertEqual(exams[0]["end_at"], "2026-05-15T11:30:00+08:00")
        self.assertEqual(exams[1]["name"], "样例课程甲 期末考试")
        self.assertEqual(exams[1]["location"], "样例考场B")
        self.assertEqual(exams[1]["description"], "座位号：B02")
        self.assertEqual(client.last_raw_counts["exams"], 2)
        self.assertEqual(client.last_converted_counts["exams"], 2)
        self.assertIn("exam_time", {issue["code"] for issue in client.last_format_issues})


if __name__ == "__main__":
    unittest.main()
