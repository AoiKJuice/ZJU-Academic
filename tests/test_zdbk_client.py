import unittest

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


if __name__ == "__main__":
    unittest.main()
