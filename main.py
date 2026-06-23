from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import ssl
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

try:
    from .academic_core.health_notifier import HealthNotifier
    from .academic_core.messages import (
        DATA_FETCH_FAILED_MESSAGE,
        DATA_REFRESH_FAILED_MESSAGE,
        MESSAGE_PREFIX,
        NEXT_TERM_CALENDAR_PENDING_MESSAGE,
        PTA_CAPTCHA_INCOMPLETE_MESSAGE,
        PTA_CREDENTIALS_REQUIRED_MESSAGE,
        PTA_DISABLED_MESSAGE,
        PTA_LOGIN_FAILED_MESSAGE,
        PTA_LOGIN_SAVED_MESSAGE,
        PTA_PASSWORD_MISSING_MESSAGE,
        PTA_SESSION_CLEARED_MESSAGE,
        PTA_USERNAME_MISSING_MESSAGE,
        QUERY_DENIED_MESSAGE,
    )
    from .academic_core.models import SourceHealth, SourceResult, SourceStatus, migrate_cache
    from .academic_core.plugin_integration import source_status_payload
    from .academic_core.refresh_coordinator import RefreshCoordinator
    from .academic_core.zdbk_client import ZdbkClient
except ImportError:
    from academic_core.health_notifier import HealthNotifier
    from academic_core.messages import (
        DATA_FETCH_FAILED_MESSAGE,
        DATA_REFRESH_FAILED_MESSAGE,
        MESSAGE_PREFIX,
        NEXT_TERM_CALENDAR_PENDING_MESSAGE,
        PTA_CAPTCHA_INCOMPLETE_MESSAGE,
        PTA_CREDENTIALS_REQUIRED_MESSAGE,
        PTA_DISABLED_MESSAGE,
        PTA_LOGIN_FAILED_MESSAGE,
        PTA_LOGIN_SAVED_MESSAGE,
        PTA_PASSWORD_MISSING_MESSAGE,
        PTA_SESSION_CLEARED_MESSAGE,
        PTA_USERNAME_MISSING_MESSAGE,
        QUERY_DENIED_MESSAGE,
    )
    from academic_core.models import SourceHealth, SourceResult, SourceStatus, migrate_cache
    from academic_core.plugin_integration import source_status_payload
    from academic_core.refresh_coordinator import RefreshCoordinator
    from academic_core.zdbk_client import ZdbkClient


TERM_AUTUMN = 0
TERM_WINTER = 1
TERM_SPRING = 4
TERM_SUMMER = 5

EXAM_AUTUMN_WINTER = 0
EXAM_SPRING_SUMMER = 1

WEEK_NORMAL = "normal"
WEEK_ODD = "odd"
WEEK_EVEN = "even"

TWEAK_CLEAR = "clear"
TWEAK_COPY = "copy"
TWEAK_EXCHANGE = "exchange"

TERM_NAME_TO_ID = {
    "秋": TERM_AUTUMN,
    "冬": TERM_WINTER,
    "春": TERM_SPRING,
    "夏": TERM_SUMMER,
}

ZJU_ICAL_PY_CONFIG_BASE_URLS = (
    "https://cdn.jsdelivr.net/gh/Xecades/zju-ical-py@main/configs",
    "https://raw.githubusercontent.com/Xecades/zju-ical-py/main/configs",
)
ZJU_ICAL_PY_CONFIG_TIMEOUT_SECONDS = 3
ZJU_ICAL_PY_CONFIG_CACHE_SECONDS = 12 * 60 * 60
ZJU_ACADEMIC_DATA_CACHE_SECONDS = 12 * 60 * 60

DEFAULT_PERIODS = {
    "1": {"start": "08:00", "end": "08:45"},
    "2": {"start": "08:50", "end": "09:35"},
    "3": {"start": "10:00", "end": "10:45"},
    "4": {"start": "10:50", "end": "11:35"},
    "5": {"start": "11:40", "end": "12:25"},
    "6": {"start": "13:25", "end": "14:10"},
    "7": {"start": "14:15", "end": "15:00"},
    "8": {"start": "15:05", "end": "15:50"},
    "9": {"start": "16:15", "end": "17:00"},
    "10": {"start": "17:05", "end": "17:50"},
    "11": {"start": "18:50", "end": "19:35"},
    "12": {"start": "19:40", "end": "20:25"},
    "13": {"start": "20:30", "end": "21:15"},
    "14": {"start": "21:20", "end": "22:05"},
    "15": {"start": "22:10", "end": "22:55"},
}

TERM_LABELS = {
    TERM_AUTUMN: "秋学期",
    TERM_WINTER: "冬学期",
    TERM_SPRING: "春学期",
    TERM_SUMMER: "夏学期",
}

EXAM_TERM_LABELS = {
    EXAM_AUTUMN_WINTER: "秋冬学期",
    EXAM_SPRING_SUMMER: "春夏学期",
}
EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u27BF"
    "]+",
)
PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGE_FONT_PATH = "assets/fonts/NotoSansCJK-Regular.ttc"
SYSTEM_IMAGE_FONT_FALLBACKS = (
    "/AstrBot/data/fonts/msyhbd.ttc",
    "/AstrBot/data/fonts/simhei.ttf",
    "/AstrBot/data/fonts/NotoSansSC-VF.ttf",
    "/AstrBot/data/fonts/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _image_font_candidates(configured_path: str) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw in (configured_path, DEFAULT_IMAGE_FONT_PATH, *SYSTEM_IMAGE_FONT_FALLBACKS):
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text)
        if not path.is_absolute():
            path = PLUGIN_DIR / path
        key = str(path)
        if key not in seen:
            candidates.append(path)
            seen.add(key)
    return candidates


class LegacySSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        try:
            context.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


@register(
    "astrbot_plugin_zju_academic",
    "OpenAI",
    "【ZJU-Academic】查询 zdbk数据、学在浙大任务和 PTA 待办，并发送提醒。",
    "0.3.4",
    "local",
)
class ZjuAcademicPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.data_dir = Path(StarTools.get_data_dir())
        self.state_path = self.data_dir / "state.json"
        self.cache_path = self.data_dir / "cache.json"
        self.calendar_cache_path = self.data_dir / "academic_calendar_cache.json"
        self.image_dir = self.data_dir / "query_images"
        self._state_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        self._state: dict[str, Any] = {}
        self._cache: dict[str, Any] = {}
        self._last_health_transitions: list[Any] = []
        self._loop_task: asyncio.Task | None = None
        self._public_route_task: asyncio.Task | None = None

    async def initialize(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._state = await asyncio.to_thread(self._load_json_file, self.state_path, self._default_state())
        raw_cache = await asyncio.to_thread(self._load_json_file, self.cache_path, self._default_cache())
        self._cache = migrate_cache(raw_cache)
        self._ensure_pta_login_token()
        await self._save_state()
        await asyncio.to_thread(self._remove_legacy_pta_login_page_config)
        self._register_web_apis()
        self._public_route_task = asyncio.create_task(self._register_public_pta_login_route_later())
        self._loop_task = asyncio.create_task(self._background_loop())

    async def terminate(self):
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._public_route_task:
            self._public_route_task.cancel()
            try:
                await self._public_route_task
            except asyncio.CancelledError:
                pass

    def _register_web_apis(self):
        register_web_api = getattr(self.context, "register_web_api", None)
        if not callable(register_web_api):
            return
        register_web_api(
            "/zju-academic/pta-login",
            self._pta_login_web_api,
            ["GET", "POST"],
            "【ZJU-Academic】PTA 登录页面和登录接口。",
        )
        register_web_api(
            "/zju-academic/pta-session",
            self._pta_session_web_api,
            ["GET", "POST"],
            "【ZJU-Academic】PTA 会话状态。",
        )

    async def _register_public_pta_login_route_later(self):
        for _ in range(20):
            if self._register_public_pta_login_route():
                return
            await asyncio.sleep(0.5)
        logger.warning("failed to register public PTA login page: WebUI app is unavailable")

    def _find_dashboard_app(self):
        try:
            import gc
            from quart import Quart
        except Exception:
            return None

        for obj in gc.get_objects():
            try:
                if isinstance(obj, Quart) and getattr(obj, "name", "") == "dashboard":
                    return obj
            except Exception:
                continue
        return None

    def _register_public_pta_login_route(self) -> bool:
        try:
            from astrbot.dashboard import server as dashboard_server
            from quart import Response as QuartResponse
            from quart import jsonify, request
        except Exception as exc:
            logger.warning(f"failed to register public PTA login page: {exc}")
            return False

        app = getattr(dashboard_server, "APP", None)
        if app is None:
            app = self._find_dashboard_app()
        if app is None:
            return False

        endpoint = "zju_academic_pta_login_public"
        if endpoint in app.view_functions:
            return True

        async def public_pta_login(token: str):
            if token != self._ensure_pta_login_token():
                response = jsonify({"ok": False, "error": PTA_LOGIN_FAILED_MESSAGE})
                response.status_code = 403
                return response
            if request.method == "GET":
                return QuartResponse(self._pta_login_page_html(self._public_pta_login_path()), mimetype="text/html")
            payload = await request.get_json(silent=True)
            if not isinstance(payload, dict):
                payload = {}
            result = await self._handle_pta_login_payload(payload)
            response = jsonify(result)
            response.status_code = 200 if result.get("ok") else 400
            return response

        try:
            app.add_url_rule(
                "/zju-academic/pta-login/<token>",
                endpoint=endpoint,
                view_func=public_pta_login,
                methods=["GET", "POST"],
            )
        except AssertionError:
            pass
        return True

    async def _pta_login_web_api(self, *args, **kwargs):
        from quart import Response as QuartResponse
        from quart import jsonify, request

        if request.method == "GET":
            return QuartResponse(self._pta_login_page_html(), mimetype="text/html")

        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        result = await self._handle_pta_login_payload(payload)
        response = jsonify(result)
        response.status_code = 200 if result.get("ok") else 400
        return response

    async def _pta_session_web_api(self, *args, **kwargs):
        from quart import jsonify, request

        if request.method == "POST":
            payload = await request.get_json(silent=True)
            action = str((payload or {}).get("action", "")).strip().lower() if isinstance(payload, dict) else ""
            if action == "clear":
                await self._clear_pta_session()
                return jsonify({"ok": True, "message": PTA_SESSION_CLEARED_MESSAGE})

        return jsonify(self._pta_session_status_payload())

    async def _handle_pta_login_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        username = self._clean_text(str(payload.get("username") or ""))
        password = str(payload.get("password") or "")
        ticket = self._clean_text(str(payload.get("ticket") or ""))
        rand_str = self._clean_text(str(payload.get("randStr") or payload.get("randstr") or payload.get("rand_str") or ""))

        if not self._pta_enabled():
            return {"ok": False, "error": PTA_DISABLED_MESSAGE}
        if not username:
            return {"ok": False, "error": PTA_USERNAME_MISSING_MESSAGE}
        if not password:
            return {"ok": False, "error": PTA_PASSWORD_MISSING_MESSAGE}
        if not ticket or not rand_str:
            return {"ok": False, "error": PTA_CAPTCHA_INCOMPLETE_MESSAGE}

        try:
            session_cookie = await asyncio.to_thread(
                self._login_pta_with_captcha_sync,
                username,
                password,
                ticket,
                rand_str,
            )
        except Exception as exc:
            logger.exception("PTA login failed")
            return {"ok": False, "error": self._sanitize_pta_error(exc)}

        updated_at = await self._save_pta_login_state(session_cookie)
        return {
            "ok": True,
            "message": PTA_LOGIN_SAVED_MESSAGE,
            "session_saved": True,
            "updated_at": updated_at,
        }

    def _login_pta_with_captcha_sync(self, username: str, password: str, ticket: str, rand_str: str) -> str:
        client = PintiaClient(
            username=username,
            password=password,
            timeout=20,
        )
        return client.login(ticket=ticket, rand_str=rand_str)

    async def _save_pta_login_state(self, session_cookie: str) -> str:
        normalized = self._normalize_pta_cookie(session_cookie)
        if not normalized:
            raise RuntimeError(PTA_LOGIN_FAILED_MESSAGE)
        updated_at = self._now().isoformat()
        self._state["pta_session"] = {
            "cookie": normalized,
            "updated_at": updated_at,
        }
        await self._save_state()
        return updated_at

    @staticmethod
    def _normalize_pta_cookie(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        if "PTASession=" in text:
            return text
        return f"PTASession={text}"
    async def _clear_pta_session(self):
        self._state.pop("pta_session", None)
        await self._save_state()

    def _ensure_pta_login_token(self) -> str:
        token = ""
        if isinstance(self._state, dict):
            token = self._cfg_like_str(self._state.get("pta_login_token", ""))
        if not token:
            token = secrets.token_urlsafe(24)
            self._state["pta_login_token"] = token
        return token

    def _public_pta_login_path(self) -> str:
        return f"/zju-academic/pta-login/{self._ensure_pta_login_token()}"

    def _remove_legacy_pta_login_page_config(self):
        config_path = Path("/AstrBot/data/config/astrbot_plugin_zju_academic_config.json")
        if not config_path.exists():
            return
        try:
            data = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                return
            basic = data.get("basic", {})
            if not isinstance(basic, dict):
                return
            if "pta_login_page" not in basic:
                return
            basic.pop("pta_login_page", None)
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            tmp_path = config_path.with_suffix(".tmp")
            tmp_path.write_text(payload, encoding="utf-8-sig")
            tmp_path.replace(config_path)
            self._chmod_private(config_path)
            if isinstance(self.config.get("basic"), dict):
                self.config["basic"].pop("pta_login_page", None)
        except Exception:
            logger.exception("failed to remove legacy PTA login page config")

    def _pta_session_status_payload(self, include_login_page: bool = False) -> dict[str, Any]:
        saved = self._state.get("pta_session") if isinstance(self._state, dict) else {}
        saved = saved if isinstance(saved, dict) else {}
        payload = {
            "ok": True,
            "pta_enabled": self._pta_enabled(),
            "saved_session": bool(self._pta_saved_session_cookie()),
            "saved_session_updated_at": self._clean_text(str(saved.get("updated_at", ""))),
            "tasks_enabled": self._pta_available(),
        }
        if include_login_page:
            payload["login_page"] = self._public_pta_login_path()
        return payload

    def _sanitize_pta_error(self, exc: Exception) -> str:
        return PTA_LOGIN_FAILED_MESSAGE

    def _pta_login_page_html(self, public_login_path: str = "") -> str:
        public_path_json = json.dumps(public_login_path, ensure_ascii=False)
        html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>【ZJU-Academic】PTA 登录</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f5f7fb; color: #172033; }
    main { width: min(460px, calc(100vw - 32px)); padding: 28px; border: 1px solid #d8deea; border-radius: 8px; background: #fff; box-shadow: 0 16px 48px rgba(31, 44, 71, .12); }
    h1 { margin: 0 0 18px; font-size: 22px; line-height: 1.25; }
    label { display: block; margin: 14px 0 6px; font-size: 14px; font-weight: 600; }
    input { box-sizing: border-box; width: 100%; height: 42px; padding: 8px 10px; border: 1px solid #c8d0df; border-radius: 6px; font: inherit; background: #fff; color: #172033; }
    button { margin-top: 18px; width: 100%; height: 42px; border: 0; border-radius: 6px; background: #1769e0; color: #fff; font: inherit; font-weight: 700; cursor: pointer; }
    button:disabled { cursor: progress; opacity: .65; }
    .status { margin-top: 14px; min-height: 22px; font-size: 14px; line-height: 1.55; color: #44516a; white-space: pre-wrap; }
    .ok { color: #096b39; }
    .err { color: #b42318; }
    .meta { margin-top: 12px; font-size: 12px; line-height: 1.45; color: #667085; }
    @media (prefers-color-scheme: dark) {
      body { background: #111827; color: #eef2ff; }
      main { background: #182233; border-color: #334155; box-shadow: none; }
      input { background: #111827; border-color: #475569; color: #eef2ff; }
      .status { color: #cbd5e1; }
      .meta { color: #94a3b8; }
      .ok { color: #86efac; }
      .err { color: #fca5a5; }
    }
  </style>
  <script src="https://turing.captcha.qcloud.com/TCaptcha.js"></script>
</head>
<body>
  <main>
    <h1>【ZJU-Academic】PTA 登录</h1>
    <form id="loginForm">
      <label for="username">【ZJU-Academic】PTA 账号</label>
      <input id="username" name="username" autocomplete="username" placeholder="邮箱或手机号">
      <label for="password">【ZJU-Academic】PTA 密码</label>
      <input id="password" name="password" type="password" autocomplete="current-password">
      <button id="submitBtn" type="submit">【ZJU-Academic】登录并保存会话</button>
    </form>
    <div id="status" class="status">【ZJU-Academic】正在读取状态</div>
    <div class="meta">【ZJU-Academic】密码只用于本次提交给 Pintia 登录接口，插件只保存 PTASession。</div>
  </main>
  <script>
    const publicLoginPath = __PUBLIC_LOGIN_PATH__;
    const statusEl = document.getElementById('status');
    const button = document.getElementById('submitBtn');
    const form = document.getElementById('loginForm');

    function setStatus(text, kind) {
      statusEl.textContent = text;
      statusEl.className = 'status ' + (kind || '');
    }

    function findTokenInValue(value, depth = 0) {
      if (!value || depth > 4) return '';
      if (typeof value === 'string') {
        const bearer = value.match(/Bearer\\s+([A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+)/);
        if (bearer) return bearer[1];
        const jwt = value.match(/^[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$/);
        if (jwt) return value;
        try { return findTokenInValue(JSON.parse(value), depth + 1); } catch (_) { return ''; }
      }
      if (Array.isArray(value)) {
        for (const item of value) {
          const token = findTokenInValue(item, depth + 1);
          if (token) return token;
        }
      }
      if (typeof value === 'object') {
        const preferred = ['token', 'access_token', 'accessToken', 'jwt', 'id_token', 'authorization', 'Authorization'];
        for (const key of preferred) {
          const token = findTokenInValue(value[key], depth + 1);
          if (token) return token;
        }
        for (const key of Object.keys(value)) {
          const token = findTokenInValue(value[key], depth + 1);
          if (token) return token;
        }
      }
      return '';
    }

    function getAuthHeaders() {
      const headers = { 'Content-Type': 'application/json' };
      for (const store of [localStorage, sessionStorage]) {
        for (let i = 0; i < store.length; i++) {
          const token = findTokenInValue(store.getItem(store.key(i)));
          if (token) {
            headers.Authorization = 'Bearer ' + token;
            return headers;
          }
        }
      }
      return headers;
    }

    async function api(path, options = {}) {
      const headers = publicLoginPath
        ? { 'Content-Type': 'application/json', ...(options.headers || {}) }
        : { ...getAuthHeaders(), ...(options.headers || {}) };
      const resp = await fetch(path, { ...options, headers });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.ok === false) throw new Error(data.error || data.message || ('HTTP ' + resp.status));
      return data;
    }

    async function refreshStatus() {
      if (publicLoginPath) {
        setStatus('【ZJU-Academic】请填写账号和密码', '');
        return;
      }
      try {
        const data = await api('/api/plug/zju-academic/pta-session');
        const parts = [];
        parts.push('【ZJU-Academic】PTA 待办：' + (data.tasks_enabled ? '已启用' : '未启用'));
        parts.push('【ZJU-Academic】已保存会话：' + (data.saved_session ? '是' : '否'));
        if (data.saved_session_updated_at) parts.push('【ZJU-Academic】保存时间：' + data.saved_session_updated_at);
        setStatus(parts.join('\\n'), data.saved_session ? 'ok' : '');
      } catch (err) {
        setStatus('【ZJU-Academic】状态读取失败，请刷新页面', 'err');
      }
    }

    function startCaptcha(username, password) {
      if (!window.TencentCaptcha) {
        setStatus('【ZJU-Academic】PTA 登录失败，请重新打开页面再试', 'err');
        button.disabled = false;
        return;
      }
      const captcha = new TencentCaptcha('194593025', async function(res) {
        if (!res || res.ret !== 0) {
          setStatus('【ZJU-Academic】验证码未完成', 'err');
          button.disabled = false;
          return;
        }
        try {
          setStatus('【ZJU-Academic】正在登录 PTA', '');
          const data = await api(publicLoginPath || '/api/plug/zju-academic/pta-login', {
            method: 'POST',
            body: JSON.stringify({
              username,
              password,
              ticket: res.ticket,
              randStr: res.randstr
            })
          });
          setStatus(data.message || '【ZJU-Academic】已保存 PTA 登录状态', 'ok');
        } catch (err) {
          setStatus('【ZJU-Academic】PTA 登录失败，请重新打开页面再试', 'err');
        } finally {
          button.disabled = false;
        }
      });
      captcha.show();
    }

    form.addEventListener('submit', function(event) {
      event.preventDefault();
      const username = document.getElementById('username').value.trim();
      const password = document.getElementById('password').value;
      if (!username || !password) {
        setStatus('【ZJU-Academic】请填写账号和密码', 'err');
        return;
      }
      button.disabled = true;
      setStatus('【ZJU-Academic】请完成验证码', '');
      startCaptcha(username, password);
    });

    refreshStatus();
  </script>
</body>
</html>"""
        return html.replace("__PUBLIC_LOGIN_PATH__", public_path_json)

    def _is_query_allowed(self, event: AstrMessageEvent) -> bool:
        return event.is_admin()

    def _query_denied_text(self) -> str:
        return QUERY_DENIED_MESSAGE

    def _query_denied_payload(self) -> str:
        return json.dumps(
            {
                "ok": False,
                "error": self._query_denied_text(),
                "reply_instruction": "只回复 error。",
            },
            ensure_ascii=False,
            indent=2,
        )

    @filter.llm_tool(name="zju_academic_status")
    async def llm_status(self, event: AstrMessageEvent) -> str:
        """【ZJU-Academic】获取插件状态、缓存数量、提醒配置、PTA 登录入口和当前会话 ID。用户询问 PTA 登录、配置、绑定、提醒是否生效、zdbk数据是否已刷新时使用。"""
        if not self._is_query_allowed(event):
            return self._query_denied_payload()
        bindings = self._target_bindings()
        payload = {
            "current_session_id": event.unified_msg_origin,
            "username_configured": bool(self._username()),
            "courses_todos_direct_enabled": bool(self._username() and self._password()),
            "pta_tasks_enabled": self._pta_available(),
            "pta_login_page": self._public_pta_login_path(),
            "pta_session_saved": bool(self._pta_saved_session_cookie()),
            "bound_sessions_count": len(bindings),
            "last_refresh": self._cache.get("last_refresh", ""),
            "academic_refresh": self._cache.get("academic_refresh", ""),
            "task_refresh": self._cache.get("task_refresh", ""),
            "class_event_count": len(self._cache.get("class_events", [])),
            "exam_event_count": len(self._cache.get("exam_events", [])),
            "task_event_count": len(self._cache.get("task_events", [])),
            "calendar_source": (self._cache.get("raw_counts", {}) or {}).get("calendar_source", ""),
            "calendar_updated_at": (self._cache.get("raw_counts", {}) or {}).get("calendar_updated_at", ""),
            "auto_calendar_enabled": self._cfg_bool("auto_calendar_enabled", True),
            "auto_refresh_enabled": self._cfg_bool("auto_refresh_enabled", True),
            "llm_reminder_enabled": self._cfg_bool("llm_reminder_enabled", True),
            "render_query_as_image": self._cfg_bool("render_query_as_image", True),
            "class_reminder_offsets_minutes": self._class_offsets(),
            "exam_reminder_offsets_minutes": self._exam_offsets(),
            "task_reminder_offsets_minutes": self._task_offsets(),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="zju_academic_pta_login")
    async def llm_pta_login(self, event: AstrMessageEvent) -> str:
        """获取 PTA 登录入口。用户询问 PTA、拼题A、Pintia 怎么登录或怎么更新 PTA 会话时使用。"""
        if not self._is_query_allowed(event):
            return self._query_denied_payload()
        payload = {
            "ok": True,
            "pta_enabled": self._pta_enabled(),
            "pta_login_page": self._public_pta_login_path(),
            "pta_session_saved": bool(self._pta_saved_session_cookie()),
            "pta_tasks_enabled": self._pta_available(),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="zju_academic_refresh")
    async def llm_refresh(self, event: AstrMessageEvent, force: bool = True) -> str:
        """刷新 zdbk数据。用户要求更新、同步、重新获取、数据可能过期，或回答前需要最新数据时使用。

        Args:
            force(boolean): 是否强制刷新。需要最新数据时传 true；只想利用缓存时传 false。
        """
        if not self._is_query_allowed(event):
            return self._query_denied_payload()
        try:
            cache = await self._refresh_cache(force=force)
        except Exception as exc:
            logger.exception("zju llm refresh failed")
            return json.dumps(
                {"ok": False, "error": DATA_REFRESH_FAILED_MESSAGE},
                ensure_ascii=False,
                indent=2,
            )
        return json.dumps(self._cache_summary_payload(cache), ensure_ascii=False, indent=2)

    @filter.llm_tool(name="zju_academic_binding")
    async def llm_binding(self, event: AstrMessageEvent, action: str = "status") -> str:
        """【ZJU-Academic】管理当前会话的提醒绑定。用户自然语言要求绑定、解绑、开启提醒、关闭提醒或查看当前会话绑定状态时使用。

        Args:
            action(string): bind、unbind 或 status。绑定/开启提醒传 bind；解绑/关闭提醒传 unbind；查看是否绑定传 status。
        """
        if not self._is_query_allowed(event):
            return self._query_denied_payload()
        normalized = self._clean_text(str(action or "status")).lower()
        umo = event.unified_msg_origin
        bindings = self._bindings()

        if normalized in {"bind", "绑定", "开启", "enable", "on"}:
            bindings[umo] = {
                "label": self._session_label(event),
                "bound_at": self._now().isoformat(),
                "class_reminders": True,
                "exam_reminders": True,
                "task_reminders": True,
            }
            await self._save_state()
            payload = {
                "ok": True,
                "action": "bind",
                "bound": True,
                "session_id": umo,
                "reply_instruction": "只回复结果。",
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)

        if normalized in {"unbind", "解绑", "关闭", "disable", "off"}:
            was_bound = umo in bindings
            bindings.pop(umo, None)
            await self._save_state()
            payload = {
                "ok": True,
                "action": "unbind",
                "was_bound": was_bound,
                "bound": False,
                "session_id": umo,
                "reply_instruction": "只回复结果。",
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)

        payload = {
            "ok": True,
            "action": "status",
            "bound": umo in self._target_bindings(),
            "runtime_bound": umo in bindings,
            "session_id": umo,
            "bound_sessions_count": len(self._target_bindings()),
            "reply_instruction": "只回复结果。",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="zju_academic_query")
    async def llm_query(self, event: AstrMessageEvent, data_type: str, range: str = "", force_refresh: bool = False) -> str:
        """【ZJU-Academic】查询 zdbk数据、DDL、学在浙大任务或 PTA 待办。用户问 DDL、ddl、截止、任务时默认查询学在浙大和 PTA 的整合结果；用户明确说 PTA、Pintia、拼题A 时只查 PTA。

        Args:
            data_type(string): 查询类型。可填 schedule、classes、课表、exam、exams、考试、task、tasks、任务、pta、pintia、拼题A。
            range(string): 查询范围。留空时课表查今天，18:00 后查今天剩余课程和明天课程；任务查未来 7 天。需要所有数据时传 all 或 全部。
            force_refresh(boolean): 是否先强制刷新数据。用户要求最新、刷新、同步时传 true；普通查询可传 false。
        """
        if not self._is_query_allowed(event):
            return self._query_denied_payload()
        try:
            cache = await self._refresh_cache(force=force_refresh)
        except Exception as exc:
            logger.exception("zju llm query refresh failed")
            return json.dumps(
                {"ok": False, "error": DATA_FETCH_FAILED_MESSAGE},
                ensure_ascii=False,
                indent=2,
            )

        normalized_type = self._normalize_query_type(data_type)
        if normalized_type == "tasks" and self._looks_like_pta_query(data_type, getattr(event, "message_str", "")):
            normalized_type = "pta_tasks"
        normalized_range = self._normalize_query_range(range)
        now = self._now()

        if normalized_type == "schedule":
            class_items = sorted(cache.get("class_events", []), key=lambda x: x["start_at"])
            raw_items, label = self._select_schedule_items(
                class_items,
                normalized_range,
                now,
            )
            calendar_items = self._select_schedule_calendar_items(class_items, raw_items, normalized_range, label, now)
            image_sent = await self._send_schedule_image_result(
                event,
                self._schedule_calendar_title(label),
                calendar_items,
            )
            payload = {
                "ok": True,
                "data_type": "schedule",
                "range": label,
                "last_refresh": cache.get("academic_refresh", "") or cache.get("last_refresh", ""),
                "image_sent": image_sent,
                "item_count": len(raw_items),
            }
            if image_sent:
                payload["reply_instruction"] = "不再输出文字。"
            else:
                payload["reply_instruction"] = "按 plain_lines 回复。"
                payload["plain_lines"] = self._class_plain_lines(raw_items)
                payload["items"] = [self._class_payload(item) for item in raw_items]
            payload = self._annotate_source_status(cache, "schedule", payload)
            return json.dumps(payload, ensure_ascii=False, indent=2)

        if normalized_type == "exams":
            raw_items = sorted(cache.get("exam_events", []), key=lambda x: x["start_at"])
            if normalized_range != "全部":
                raw_items = [item for item in raw_items if self._parse_dt(item["start_at"]) >= now - timedelta(hours=2)]
                raw_items = raw_items[:12]
            image_sent = await self._send_query_image_result(event, "考试安排", self._exam_cards(raw_items))
            payload = {
                "ok": True,
                "data_type": "exams",
                "range": "全部" if normalized_range == "全部" else "近期",
                "last_refresh": cache.get("academic_refresh", "") or cache.get("last_refresh", ""),
                "image_sent": image_sent,
                "item_count": len(raw_items),
            }
            if image_sent:
                payload["reply_instruction"] = "不再输出文字。"
            else:
                payload["reply_instruction"] = "按 plain_lines 回复。"
                payload["plain_lines"] = self._exam_plain_lines(raw_items)
                payload["items"] = [self._exam_payload(item) for item in raw_items]
            payload = self._annotate_source_status(cache, "exams", payload)
            return json.dumps(payload, ensure_ascii=False, indent=2)

        if normalized_type in {"tasks", "pta_tasks"}:
            raw_items = sorted(cache.get("task_events", []), key=lambda x: x["due_at"])
            if normalized_type == "pta_tasks":
                raw_items = [item for item in raw_items if self._is_pta_task(item)]
            raw_items, label = self._select_task_items(raw_items, normalized_range, now)
            title = "PTA DDL" if normalized_type == "pta_tasks" else "DDL"
            title = self._title_with_range(title, label)
            image_sent = await self._send_query_image_result(event, title, self._task_cards(raw_items))
            source_counts = self._task_source_counts(raw_items)
            payload = {
                "ok": True,
                "data_type": normalized_type,
                "range": label,
                "last_refresh": cache.get("task_refresh", "") or cache.get("last_refresh", ""),
                "image_sent": image_sent,
                "source_counts": source_counts,
                "item_count": len(raw_items),
            }
            if image_sent:
                payload["reply_instruction"] = "不再输出文字。"
            else:
                payload["reply_instruction"] = "按 plain_lines 回复。"
                payload["plain_lines"] = self._task_plain_lines(raw_items)
                payload["items"] = [self._task_payload(item) for item in raw_items]
            status_source = "pta_tasks" if normalized_type == "pta_tasks" else "tasks"
            payload = self._annotate_source_status(cache, status_source, payload)
            return json.dumps(payload, ensure_ascii=False, indent=2)

        return json.dumps(
            {
                "ok": False,
                "error": DATA_FETCH_FAILED_MESSAGE,
                "supported_data_type": ["schedule", "exams", "tasks", "pta_tasks"],
            },
            ensure_ascii=False,
            indent=2,
        )

    def _annotate_source_status(self, cache: dict[str, Any], source: str, payload: dict[str, Any]) -> dict[str, Any]:
        status = source_status_payload(cache, source)
        if status:
            payload = dict(payload)
            payload["source_status"] = status
        return payload

    async def _background_loop(self):
        while True:
            try:
                if self._cfg_bool("auto_refresh_enabled", True):
                    await self._refresh_cache(force=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("zju academic refresh loop failed")

            try:
                await self._dispatch_health_notifications()
                await self._dispatch_reminders()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("zju academic notification loop failed")

            await asyncio.sleep(max(15, self._cfg_int("loop_interval_seconds", 45)))

    async def _dispatch_reminders(self):
        now = self._now()
        grace = timedelta(seconds=max(30, self._cfg_int("loop_interval_seconds", 45) + 15))
        recipients = self._reminder_targets()
        if not recipients:
            return
        target_bindings = self._target_bindings()

        for umo in recipients:
            binding = target_bindings.get(umo, {})
            if binding.get("class_reminders", True):
                await self._dispatch_event_reminders(
                    umo=umo,
                    items=self._cache.get("class_events", []),
                    event_type="class",
                    offsets=self._class_offsets(),
                    key_field="start_at",
                    formatter=self._format_class_reminder,
                    now=now,
                    grace=grace,
                )
            if binding.get("exam_reminders", True):
                await self._dispatch_event_reminders(
                    umo=umo,
                    items=self._cache.get("exam_events", []),
                    event_type="exam",
                    offsets=self._exam_offsets(),
                    key_field="start_at",
                    formatter=self._format_exam_reminder,
                    now=now,
                    grace=grace,
                )
            if binding.get("task_reminders", True):
                await self._dispatch_event_reminders(
                    umo=umo,
                    items=self._cache.get("task_events", []),
                    event_type="task",
                    offsets=self._task_offsets(),
                    key_field="due_at",
                    formatter=self._format_task_reminder,
                    now=now,
                    grace=grace,
                )

    async def _dispatch_health_notifications(self):
        transitions = list(self._last_health_transitions or [])
        if not transitions:
            return
        recipients = self._reminder_targets()
        if not recipients:
            return

        labels = {
            "calendar": "校历",
            "schedule": "课表",
            "exams": "考试",
            "tasks": "学在浙大任务",
            "pta_tasks": "PTA 任务",
        }
        sent_any = False
        for transition in transitions:
            source = transition.source
            notifier = HealthNotifier(source=source, source_label=labels.get(source, source))
            health = SourceHealth.from_dict(self._cache.get("source_health", {}).get(source))
            for note in notifier.pending(transition.before, health, recipients, self._now()):
                try:
                    sent = await self.context.send_message(
                        note.recipient,
                        MessageChain(chain=[Plain(note.text)]),
                    )
                except Exception:
                    logger.exception("zju academic health notification failed")
                    continue
                if sent:
                    health = notifier.mark_sent(health, note.recipient, self._now())
                    self._cache.setdefault("source_health", {})[source] = health.to_dict()
                    sent_any = True
        self._last_health_transitions = []
        if sent_any:
            await self._save_cache()

    async def _dispatch_event_reminders(
        self,
        umo: str,
        items: list[dict[str, Any]],
        event_type: str,
        offsets: list[int],
        key_field: str,
        formatter,
        now: datetime,
        grace: timedelta,
    ):
        for item in items:
            if event_type == "task" and self._is_task_completed(item):
                continue
            event_dt = self._parse_dt(item[key_field])
            for offset in offsets:
                remind_at = event_dt - timedelta(minutes=offset)
                if now < remind_at or now > remind_at + grace:
                    continue
                reminder_key = self._reminder_key(event_type, umo, item["id"], offset)
                if self._has_reminded(reminder_key):
                    continue
                text = await self._format_reminder_text(event_type, item, offset, umo, formatter)
                sent = await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
                if sent:
                    self._mark_reminded(reminder_key)
                    await self._save_state()

    async def _format_reminder_text(self, event_type: str, item: dict[str, Any], offset: int, umo: str, fallback_formatter) -> str:
        fallback = fallback_formatter(item, offset)
        if not self._cfg_bool("llm_reminder_enabled", True):
            return fallback

        provider_id = await self._resolve_provider_id(umo=umo, config_key="reminder_provider_id")
        if not provider_id:
            return fallback

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=self._compose_persona_system_prompt(
                    "你是一个可靠的课业提醒助手。只根据用户提供的事件信息生成提醒，"
                    "不要编造课程、地点或截止时间。输出只包含最终要发送给学生的中文提醒文本。"
                    "输出必须以【ZJU-Academic】开头。"
                    "允许沿用人设语气，但必须克制、清楚、可执行。不要使用 Markdown 标题、加粗、表格或代码块。"
                ),
                prompt=self._build_reminder_prompt(event_type, item, offset),
            )
            text = (resp.completion_text or "").strip()
            text = text.strip("\"'` \n")
            if text:
                if not text.startswith(MESSAGE_PREFIX):
                    text = f"{MESSAGE_PREFIX}{text}"
                return text[: self._cfg_int("llm_reminder_max_chars", 260)]
        except Exception:
            logger.exception("zju llm reminder generation failed")
        return fallback

    async def _refresh_cache(self, force: bool) -> dict[str, Any]:
        if not force and self._is_cache_fresh() and not self._has_due_unhealthy_source():
            return self._cache
        cache = await asyncio.to_thread(self._fetch_remote_data_sync, force)
        async with self._cache_lock:
            self._cache = cache
            await self._save_cache()
        return self._cache

    def _fetch_remote_data_sync(self, force: bool = False) -> dict[str, Any]:
        refresh_academic = bool(
            force
            or not self._is_academic_cache_fresh()
            or any(self._source_due_for_refresh(source) for source in ("calendar", "schedule", "exams"))
        )
        refresh_tasks = bool(
            force
            or not self._is_task_cache_fresh()
            or any(self._source_due_for_refresh(source) for source in ("tasks", "pta_tasks"))
        )
        if not refresh_academic and not refresh_tasks:
            return migrate_cache(self._cache)

        cache = migrate_cache(self._cache or self._default_cache())
        raw_counts = dict(cache.get("raw_counts", {}) or {})
        calendar_holder: dict[str, Any] = {}
        refresh_meta: dict[str, Any] = {}
        sources: dict[str, Any] = {}
        client_holder: dict[str, Any] = {}

        username = self._username()
        password = self._password()
        has_zju_credentials = bool(username and password)

        def zju_client() -> ZdbkClient:
            if "client" in client_holder:
                return client_holder["client"]
            if "error" in client_holder:
                raise client_holder["error"]
            try:
                client = self._build_zju_client()
                client.login()
            except Exception as exc:
                client_holder["error"] = exc
                raise
            client_holder["client"] = client
            return client

        def calendar_config() -> dict[str, Any]:
            if "config" not in calendar_holder:
                calendar_holder["config"] = self._academic_calendar_config(force=force)
            return calendar_holder["config"]

        if refresh_academic:
            sources["calendar"] = lambda: SourceResult(data=calendar_config())

            if has_zju_credentials:
                def fetch_schedule() -> SourceResult:
                    now = self._now()
                    config = calendar_config()
                    term_configs = config.get("term_configs", [])
                    if self._calendar_refresh_state(term_configs) == "calendar_pending":
                        return SourceResult(
                            data={
                                "templates": cache.get("source_data", {}).get("schedule_templates", []),
                                "events": cache.get("class_events", []),
                            },
                            metadata={
                                "status": "calendar_pending",
                                "message": NEXT_TERM_CALENDAR_PENDING_MESSAGE,
                            },
                        )

                    client = zju_client()
                    classes: list[dict[str, Any]] = []
                    for academic_year, term in self._unique_class_terms(term_configs):
                        classes.extend(client.get_classes(academic_year, term))
                    schedule_start = now.date() - timedelta(days=now.date().weekday())
                    class_events = self._expand_class_events(
                        classes,
                        term_configs,
                        config.get("holiday_tweaks", []),
                        schedule_start,
                    )
                    refresh_meta["class_events_from"] = schedule_start.isoformat()
                    raw_counts["class_templates"] = len(classes)
                    raw_counts["calendar_source"] = config.get("source", "")
                    raw_counts["calendar_updated_at"] = config.get("updated_at", "")
                    return SourceResult(data={"templates": classes, "events": class_events})

                def fetch_exams() -> SourceResult:
                    now = self._now()
                    client = zju_client()
                    exams = client.get_exams()
                    exam_events = [
                        item
                        for item in exams
                        if self._parse_dt(item["start_at"]) >= now - timedelta(days=7)
                    ]
                    exam_events.sort(key=lambda x: x["start_at"])
                    raw_counts["exams"] = len(exam_events)
                    return SourceResult(data=exam_events)

                sources["schedule"] = fetch_schedule
                sources["exams"] = fetch_exams

        if refresh_tasks:
            if has_zju_credentials:
                def fetch_tasks() -> SourceResult:
                    client = zju_client()
                    return SourceResult(data=self._filter_task_horizon(self._fetch_courses_task_events_sync(client)))

                sources["tasks"] = fetch_tasks

            def fetch_pta_tasks() -> SourceResult:
                return SourceResult(data=self._filter_task_horizon(self._fetch_pta_task_events_sync()))

            sources["pta_tasks"] = fetch_pta_tasks

        result = RefreshCoordinator(cache, self._now).refresh(sources, force=force)
        cache = migrate_cache(result.cache)
        self._last_health_transitions = result.transitions
        if refresh_meta.get("class_events_from"):
            cache["class_events_from"] = refresh_meta["class_events_from"]

        self._merge_task_source_data(cache)
        raw_counts["tasks"] = len(cache.get("source_data", {}).get("task_events", []) or [])
        raw_counts["pta_tasks"] = len(cache.get("source_data", {}).get("pta_task_events", []) or [])
        cache["raw_counts"] = raw_counts
        self._update_legacy_refresh_times(cache)
        return cache

    def _build_zju_client(self) -> ZdbkClient:
        return ZdbkClient(
            username=self._username(),
            password=self._password(),
            timeout=20,
        )

    def _has_due_unhealthy_source(self) -> bool:
        return any(self._source_due_for_refresh(source) for source in ("calendar", "schedule", "exams", "tasks", "pta_tasks"))

    def _source_due_for_refresh(self, source: str) -> bool:
        health = SourceHealth.from_dict(self._cache.get("source_health", {}).get(source))
        if health.status not in (SourceStatus.FAILED, SourceStatus.WAITING_CALENDAR):
            return False
        if not health.next_retry_at:
            return True
        try:
            return self._now() >= self._parse_dt(health.next_retry_at)
        except Exception:
            return True

    def _calendar_refresh_state(self, term_configs: list[dict[str, Any]]) -> str:
        today = self._now().date()
        parsed_terms: list[tuple[date, date]] = []
        for item in term_configs:
            try:
                begin = self._parse_date(item["begin"])
                end = self._parse_date(item["end"])
            except Exception:
                continue
            parsed_terms.append((begin, end))
        if not parsed_terms:
            return "calendar_pending"
        parsed_terms.sort(key=lambda item: item[0])
        if any(begin <= today <= end for begin, end in parsed_terms):
            return "active"
        if any(begin > today for begin, _ in parsed_terms):
            return "vacation"
        return "calendar_pending"

    def _filter_task_horizon(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = self._now()
        task_horizon = self._cfg_int("task_horizon_days", 45)
        result = [
            item for item in tasks
            if self._parse_dt(item["due_at"]) >= now - timedelta(days=1)
            and self._parse_dt(item["due_at"]) <= now + timedelta(days=task_horizon)
        ]
        result.sort(key=lambda x: x["due_at"])
        return result

    def _merge_task_source_data(self, cache: dict[str, Any]):
        source_data = cache.get("source_data", {})
        tasks = list(source_data.get("task_events", []) or [])
        pta_tasks = list(source_data.get("pta_task_events", []) or [])
        merged = tasks + pta_tasks
        merged.sort(key=lambda x: x.get("due_at", ""))
        cache["task_events"] = merged

    def _update_legacy_refresh_times(self, cache: dict[str, Any]):
        health = cache.get("source_health", {})
        academic_times = [
            SourceHealth.from_dict(health.get(source)).last_success_at
            for source in ("calendar", "schedule", "exams")
        ]
        task_times = [
            SourceHealth.from_dict(health.get(source)).last_success_at
            for source in ("tasks", "pta_tasks")
        ]
        latest_academic = max([item for item in academic_times if item], default="")
        latest_tasks = max([item for item in task_times if item], default="")
        if latest_academic:
            cache["academic_refresh"] = latest_academic
        if latest_tasks:
            cache["task_refresh"] = latest_tasks
        latest = max([item for item in (latest_academic, latest_tasks) if item], default="")
        if latest:
            cache["last_refresh"] = latest

    def _fetch_courses_task_events_sync(self, client: ZdbkClient) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for todo in client.get_learning_tasks():
            title = self._clean_text(str(todo.get("title", "")).strip())
            course = self._clean_text(str(todo.get("course_name", "")).strip())
            due_dt = self._coerce_task_datetime(todo.get("end_time"))
            if not title or not due_dt:
                continue
            task_id = self._clean_text(str(todo.get("id", "")).strip()) or self._stable_id(
                "courses_todo", title, course, due_dt.isoformat()
            )
            description = course
            items.append(
                {
                    "id": task_id,
                    "name": title,
                    "description": description,
                    "location": "",
                    "course": course,
                    "start_at": due_dt.isoformat(),
                    "due_at": due_dt.isoformat(),
                    "completed": self._task_completed_from_payload(todo),
                    "is_task_like": True,
                }
            )
        items.sort(key=lambda x: x["due_at"])
        return items

    def _fetch_pta_task_events_sync(self) -> list[dict[str, Any]]:
        if not self._pta_enabled():
            return []
        session_cookie = self._pta_effective_session_cookie()
        if not session_cookie:
            return []

        client = PintiaClient(
            session_cookie=session_cookie,
            username="",
            password="",
            timeout=20,
        )
        items: list[dict[str, Any]] = []
        for problem_set in client.get_active_problem_sets(self._now()):
            if not isinstance(problem_set, dict):
                continue
            title = self._clean_text(str(problem_set.get("name", "")).strip())
            problem_set_id = self._clean_text(str(problem_set.get("id", "")).strip())
            due_dt = self._coerce_task_datetime(problem_set.get("endAt"))
            if not title or not due_dt:
                continue
            start_dt = self._coerce_task_datetime(problem_set.get("startAt")) or due_dt
            organization = self._clean_text(str(problem_set.get("organizationName", "")).strip())
            owner = self._clean_text(str(problem_set.get("ownerNickname", "")).strip())
            source_label = organization or owner or "PTA"
            items.append(
                {
                    "id": f"pta:{problem_set_id}" if problem_set_id else self._stable_id("pta", title, due_dt.isoformat()),
                    "name": title,
                    "description": source_label,
                    "location": "PTA",
                    "course": source_label,
                    "source": "PTA",
                    "start_at": start_dt.isoformat(),
                    "due_at": due_dt.isoformat(),
                    "completed": self._task_completed_from_payload(problem_set),
                    "is_task_like": True,
                    "url": f"https://pintia.cn/problem-sets/{problem_set_id}" if problem_set_id else "",
                }
            )
        items.sort(key=lambda x: x["due_at"])
        return items

    def _expand_class_events(
        self,
        classes: list[dict[str, Any]],
        term_configs: list[dict[str, Any]],
        tweaks: list[dict[str, Any]],
        today: date,
    ) -> list[dict[str, Any]]:
        horizon_days = self._cfg_int("class_horizon_days", 21)
        end_day = today + timedelta(days=max(1, horizon_days))
        periods = self._periods()
        events: list[dict[str, Any]] = []

        for term_config in term_configs:
            begin = self._parse_date(term_config["begin"])
            end = self._parse_date(term_config["end"])
            if end < today or begin > end_day:
                continue
            shadow_dates = self._build_shadow_dates(begin, end, tweaks)
            monday_of_first_week = self._monday_of_first_week(begin, int(term_config.get("first_week_no", 1) or 1))
            for actual_day, class_day in shadow_dates.items():
                if actual_day < today or actual_day > end_day:
                    continue
                weekday = class_day.isoweekday()
                week_number = self._week_number(monday_of_first_week, class_day)
                is_even_week = self._is_even_week(monday_of_first_week, class_day)
                for item in classes:
                    if weekday != item["day_number"]:
                        continue
                    if int(term_config["term"]) not in item["term_arrangements"]:
                        continue
                    week_numbers = item.get("week_numbers") or []
                    if week_numbers and week_number not in week_numbers:
                        continue
                    arrangement = item["week_arrangement"]
                    if arrangement == WEEK_ODD and is_even_week:
                        continue
                    if arrangement == WEEK_EVEN and not is_even_week:
                        continue

                    start_period = periods.get(str(item["start_period"])) or periods["1"]
                    end_period = periods.get(str(item["end_period"])) or start_period
                    start_time = start_period["start"]
                    end_time = end_period["end"]
                    start_dt = self._combine_day_time(actual_day, start_time)
                    end_dt = self._combine_day_time(actual_day, end_time)
                    events.append(
                        {
                            "id": self._stable_id(
                                "class",
                                item["name"],
                                actual_day.isoformat(),
                                str(item["start_period"]),
                                str(item["end_period"]),
                                item.get("location", ""),
                            ),
                            "name": item["name"],
                            "location": item.get("location", ""),
                            "teacher": item.get("teacher", ""),
                            "course_code": item.get("course_code", ""),
                            "term": int(term_config["term"]),
                            "week_number": week_number,
                            "start_period": item["start_period"],
                            "end_period": item["end_period"],
                            "start_at": start_dt.isoformat(),
                            "end_at": end_dt.isoformat(),
                        }
                    )
        events.sort(key=lambda x: x["start_at"])
        return events

    def _build_shadow_dates(
        self,
        begin: date,
        end: date,
        tweaks: list[dict[str, Any]],
    ) -> dict[date, date]:
        shadow_dates: dict[date, date] = {}
        current = begin
        while current <= end:
            shadow_dates[current] = current
            current += timedelta(days=1)

        for tweak in tweaks:
            from_day = self._parse_date(tweak["from"])
            to_day = self._parse_date(tweak["to"])
            tweak_type = tweak["type"]
            if tweak_type == TWEAK_CLEAR:
                cursor = from_day
                while cursor <= to_day:
                    shadow_dates.pop(cursor, None)
                    cursor += timedelta(days=1)
            elif tweak_type == TWEAK_COPY:
                if begin <= to_day <= end:
                    shadow_dates[to_day] = from_day
            elif tweak_type == TWEAK_EXCHANGE:
                if begin <= to_day <= end:
                    shadow_dates[to_day] = from_day
                if begin <= from_day <= end:
                    shadow_dates[from_day] = to_day
        return shadow_dates

    def _resolve_schedule_range(self, raw: str) -> tuple[date, date, str]:
        today = self._now().date()
        if raw == "明天":
            target = today + timedelta(days=1)
            return target, target, "明天"
        if raw == "全部":
            return today, today + timedelta(days=max(1, self._cfg_int("class_horizon_days", 21))), "全部"
        if raw == "本周":
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
            return start, end, "本周"
        if raw == "下周":
            start = today - timedelta(days=today.weekday()) + timedelta(days=7)
            end = start + timedelta(days=6)
            return start, end, "下周"
        if raw not in {"今天", ""}:
            try:
                target = datetime.strptime(raw, "%Y-%m-%d").date()
                return target, target, raw
            except ValueError:
                pass
        return today, today, "今天"

    def _select_schedule_items(
        self,
        items: list[dict[str, Any]],
        raw_range: str,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], str]:
        if raw_range in {"", "默认"}:
            today = now.date()
            if now.time() >= time(18, 0):
                tomorrow = today + timedelta(days=1)
                return [
                    item for item in items
                    if (
                        self._parse_dt(item["start_at"]).date() == today
                        and self._parse_dt(item["end_at"]) >= now
                    )
                    or self._parse_dt(item["start_at"]).date() == tomorrow
                ], "今晚和明天"
            return [
                item for item in items
                if self._parse_dt(item["start_at"]).date() == today
            ], "今天"

        start_day, end_day, label = self._resolve_schedule_range(raw_range)
        return [
            item for item in items
            if start_day <= self._parse_dt(item["start_at"]).date() <= end_day
        ], label

    def _schedule_week_starts(self, items: list[dict[str, Any]]) -> list[date]:
        result = {
            self._parse_dt(item["start_at"]).date() - timedelta(days=self._parse_dt(item["start_at"]).date().weekday())
            for item in items
        }
        return sorted(result) or [self._now().date() - timedelta(days=self._now().date().weekday())]

    def _select_schedule_calendar_items(
        self,
        all_items: list[dict[str, Any]],
        selected_items: list[dict[str, Any]],
        raw_range: str,
        label: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        if raw_range in {"全部", "本周", "下周"} or label in {"全部", "本周", "下周"}:
            return selected_items

        if label == "明天":
            target = now.date() + timedelta(days=1)
        elif label == "今晚和明天":
            target = now.date()
        elif selected_items:
            target = self._parse_dt(selected_items[0]["start_at"]).date()
        else:
            target = now.date()

        week_start = target - timedelta(days=target.weekday())
        week_end = week_start + timedelta(days=6)
        return [
            item for item in all_items
            if week_start <= self._parse_dt(item["start_at"]).date() <= week_end
        ]

    @staticmethod
    def _schedule_calendar_title(label: str) -> str:
        if label in {"今天", "明天", "今晚和明天", "本周"}:
            return "本周课表"
        if label == "下周":
            return "下周课表"
        return f"{label}课表"

    def _select_task_items(
        self,
        items: list[dict[str, Any]],
        raw_range: str,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], str]:
        if raw_range == "全部":
            return items, "全部"

        start = now - timedelta(hours=2)
        end_day = now.date() + timedelta(days=7)
        end = datetime.combine(end_day, time.max, tzinfo=now.tzinfo)
        label = "未来7天"

        if raw_range == "今天":
            start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
            end = datetime.combine(now.date(), time.max, tzinfo=now.tzinfo)
            label = "今天"
        elif raw_range == "明天":
            day = now.date() + timedelta(days=1)
            start = datetime.combine(day, time.min, tzinfo=now.tzinfo)
            end = datetime.combine(day, time.max, tzinfo=now.tzinfo)
            label = "明天"
        elif raw_range == "本周":
            sunday = now.date() + timedelta(days=6 - now.date().weekday())
            end = datetime.combine(sunday, time.max, tzinfo=now.tzinfo)
            label = "本周"
        elif raw_range == "下周":
            monday = now.date() - timedelta(days=now.date().weekday()) + timedelta(days=7)
            sunday = monday + timedelta(days=6)
            start = datetime.combine(monday, time.min, tzinfo=now.tzinfo)
            end = datetime.combine(sunday, time.max, tzinfo=now.tzinfo)
            label = "下周"
        elif raw_range not in {"", "默认", "近7天", "近期"}:
            try:
                day = datetime.strptime(raw_range, "%Y-%m-%d").date()
                start = datetime.combine(day, time.min, tzinfo=now.tzinfo)
                end = datetime.combine(day, time.max, tzinfo=now.tzinfo)
                label = raw_range
            except ValueError:
                pass

        return [
            item for item in items
            if start <= self._parse_dt(item["due_at"]) <= end
        ], label

    def _is_pta_task(self, item: dict[str, Any]) -> bool:
        source = self._clean_text(str(item.get("source") or item.get("location") or item.get("course") or "")).lower()
        task_id = self._clean_text(str(item.get("id") or "")).lower()
        return source == "pta" or task_id.startswith("pta:")

    def _task_completed_from_payload(self, payload: dict[str, Any]) -> bool | None:
        if not isinstance(payload, dict):
            return None

        explicit_keys = (
            "completed",
            "complete",
            "is_completed",
            "isComplete",
            "is_finished",
            "isFinished",
            "finished",
            "done",
            "is_done",
            "isDone",
            "submitted",
            "is_submitted",
            "isSubmitted",
            "has_submitted",
            "hasSubmitted",
            "user_finished",
            "userFinished",
            "user_completed",
            "userCompleted",
        )
        for key in explicit_keys:
            if key in payload:
                value = self._coerce_task_completion_value(payload.get(key), numeric_allowed=True)
                if value is not None:
                    return value

        status_keys = (
            "status",
            "state",
            "todo_status",
            "todoStatus",
            "submit_status",
            "submitStatus",
            "submission_status",
            "submissionStatus",
        )
        for key in status_keys:
            if key in payload:
                value = self._coerce_task_completion_value(payload.get(key), numeric_allowed=False)
                if value is not None:
                    return value
        return None

    def _coerce_task_completion_value(self, value: Any, numeric_allowed: bool) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, dict):
            for key in ("completed", "finished", "done", "submitted", "status", "state", "value"):
                if key in value:
                    result = self._coerce_task_completion_value(value.get(key), numeric_allowed=numeric_allowed)
                    if result is not None:
                        return result
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if numeric_allowed and value in {0, 1}:
                return bool(value)
            return None

        text = self._clean_text(str(value or "")).strip().lower()
        if not text:
            return None
        if text in {"0", "1"} and not numeric_allowed:
            return None

        true_values = {
            "1",
            "true",
            "yes",
            "y",
            "done",
            "complete",
            "completed",
            "finish",
            "finished",
            "submitted",
            "submit",
            "passed",
            "closed",
            "已完成",
            "完成",
            "已提交",
            "提交",
            "已交",
            "已截止",
        }
        false_values = {
            "0",
            "false",
            "no",
            "n",
            "todo",
            "open",
            "active",
            "pending",
            "unfinished",
            "incomplete",
            "not_finished",
            "not_completed",
            "unsubmitted",
            "未完成",
            "未提交",
            "未交",
            "进行中",
            "待完成",
            "待提交",
        }
        if text in true_values:
            return True
        if text in false_values:
            return False
        return None

    def _is_task_completed(self, item: dict[str, Any]) -> bool:
        value = self._coerce_task_completion_value(item.get("completed"), numeric_allowed=True)
        if value is not None:
            return value
        return self._task_completed_from_payload(item) is True

    def _task_platform_label(self, item: dict[str, Any]) -> str:
        return "PTA" if self._is_pta_task(item) else "学在浙大"

    def _task_source_counts(self, items: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            label = self._task_platform_label(item)
            counts[label] = counts.get(label, 0) + 1
        return counts

    def _display_range_label(self, label: str) -> str:
        label = self._clean_text(str(label or ""))
        if label in {"未来7天", "近7天", "近期"}:
            return "一周内"
        return label or "默认"

    def _title_with_range(self, title: str, label: str) -> str:
        display_label = self._display_range_label(label)
        if display_label == "全部":
            return title
        return f"{title}（{display_label}）"

    def _class_cards(self, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        cards: list[dict[str, str]] = []
        for item in items:
            start_dt = self._parse_dt(item["start_at"])
            end_dt = self._parse_dt(item["end_at"])
            day_label = start_dt.strftime("%m-%d %a")
            cards.append(
                {
                    "title": item.get("name", ""),
                    "time": f"{day_label} {start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}",
                    "meta": item.get("location") or "地点待定",
                }
            )
        return cards

    def _exam_cards(self, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        cards: list[dict[str, str]] = []
        for item in items:
            start_dt = self._parse_dt(item["start_at"])
            cards.append(
                {
                    "title": item.get("name", ""),
                    "time": start_dt.strftime("%m-%d %H:%M"),
                    "meta": item.get("location") or "地点待定",
                }
            )
        return cards

    def _task_cards(self, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        cards: list[dict[str, str]] = []
        for item in items:
            due_dt = self._task_deadline_dt(item)
            platform = self._task_platform_label(item)
            detail = item.get("course") or item.get("location") or item.get("source") or ""
            meta = f"{platform} · {detail}" if detail and detail != platform else platform
            cards.append(
                {
                    "title": item.get("name", ""),
                    "time": due_dt.strftime("%Y-%m-%d %H:%M"),
                    "deadline_at": due_dt.isoformat(),
                    "meta": meta,
                }
            )
        return cards

    def _class_plain_lines(self, items: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for item in items:
            start_dt = self._parse_dt(item["start_at"])
            end_dt = self._parse_dt(item["end_at"])
            lines.append(
                f"{MESSAGE_PREFIX}{start_dt.strftime('%m-%d %H:%M')}-{end_dt.strftime('%H:%M')} "
                f"{item.get('name', '')}（{item.get('location') or '地点待定'}）"
            )
        return lines

    def _exam_plain_lines(self, items: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for item in items:
            start_dt = self._parse_dt(item["start_at"])
            lines.append(
                f"{MESSAGE_PREFIX}{start_dt.strftime('%m-%d %H:%M')} "
                f"{item.get('name', '')}（{item.get('location') or '地点待定'}）"
            )
        return lines

    def _task_plain_lines(self, items: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for item in items:
            due_dt = self._task_deadline_dt(item)
            platform = self._task_platform_label(item)
            detail = item.get("course") or item.get("location") or item.get("source") or ""
            suffix = f"{platform}，{detail}" if detail and detail != platform else platform
            lines.append(f"{MESSAGE_PREFIX}{due_dt.strftime('%Y-%m-%d %H:%M')} {item.get('name', '')}（{suffix}）")
        return lines

    async def _send_query_image_result(
        self,
        event: AstrMessageEvent,
        title: str,
        cards: list[dict[str, str]],
        columns: int = 1,
        min_width: int | None = None,
    ) -> bool:
        if not self._cfg_bool("render_query_as_image", True):
            return False
        if not cards:
            return False
        title = self._prefixed_text(title)
        try:
            image_paths = await asyncio.to_thread(self._render_query_cards, title, cards, columns, min_width)
        except Exception:
            logger.exception("zju query image render failed")
            return False
        if not image_paths:
            return False
        try:
            return bool(await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Image.fromFileSystem(path) for path in image_paths]),
            ))
        except Exception:
            logger.exception("zju query image send failed")
            return False

    async def _build_query_image_result(
        self,
        event: AstrMessageEvent,
        title: str,
        cards: list[dict[str, str]],
        columns: int = 1,
        min_width: int | None = None,
    ) -> Any | None:
        if not self._cfg_bool("render_query_as_image", True):
            return None
        if not cards:
            return None
        title = self._prefixed_text(title)
        try:
            image_paths = await asyncio.to_thread(self._render_query_cards, title, cards, columns, min_width)
        except Exception:
            logger.exception("zju query image render failed")
            return None
        if not image_paths:
            return None
        return event.chain_result([Comp.Image.fromFileSystem(path) for path in image_paths])

    async def _send_schedule_image_result(self, event: AstrMessageEvent, title: str, items: list[dict[str, Any]]) -> bool:
        if not self._cfg_bool("render_query_as_image", True):
            return False
        if not items:
            return False
        title = self._prefixed_text(title)
        try:
            image_paths = await asyncio.to_thread(self._render_schedule_calendar, title, items)
        except Exception:
            logger.exception("zju schedule calendar render failed")
            return False
        if not image_paths:
            return False
        try:
            return bool(await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Image.fromFileSystem(path) for path in image_paths]),
            ))
        except Exception:
            logger.exception("zju schedule calendar send failed")
            return False

    async def _build_schedule_image_result(self, event: AstrMessageEvent, title: str, items: list[dict[str, Any]]) -> Any | None:
        if not self._cfg_bool("render_query_as_image", True):
            return None
        if not items:
            return None
        try:
            image_paths = await asyncio.to_thread(self._render_schedule_calendar, title, items)
        except Exception:
            logger.exception("zju schedule calendar render failed")
            return None
        if not image_paths:
            return None
        return event.chain_result([Comp.Image.fromFileSystem(path) for path in image_paths])

    def _render_schedule_calendar(self, title: str, items: list[dict[str, Any]]) -> list[str]:
        from PIL import Image, ImageDraw, ImageFont

        periods = self._periods()
        period_numbers = [int(key) for key in periods.keys() if str(key).isdigit()]
        period_numbers = sorted(period_numbers) or list(range(1, 16))
        period_numbers = [period_no for period_no in period_numbers if period_no <= 13]

        width = max(1560, self._cfg_int("query_image_width", 900))
        font_size = max(17, self._cfg_int("query_image_font_size", 24))
        padding = 24
        title_gap = 36
        week_gap = 34
        day_header_h = 54
        base_row_h = 58
        period_no_col_w = 54
        period_time_col_w = 96
        period_cols_w = period_no_col_w + period_time_col_w
        grid_w = width - padding * 2 - period_cols_w
        day_w = grid_w / 7
        palette = ["#dbeafe", "#dcfce7", "#fef3c7", "#fce7f3", "#ede9fe", "#ccfbf1", "#ffedd5"]
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]

        font_path = self._cfg_str("query_image_font_path", DEFAULT_IMAGE_FONT_PATH)

        def load_font(size: int):
            for candidate in _image_font_candidates(font_path):
                try:
                    if candidate.exists():
                        return ImageFont.truetype(str(candidate), size=size)
                except Exception:
                    continue
            return ImageFont.load_default()

        title_font = load_font(font_size + 8)
        header_font = load_font(max(18, font_size - 2))
        item_font = load_font(max(16, font_size - 5))
        small_font = load_font(max(13, font_size - 9))

        dummy = Image.new("RGB", (width, 10), "#f8f7f2")
        draw = ImageDraw.Draw(dummy)

        def text_size(text: str, font) -> tuple[int, int]:
            box = draw.textbbox((0, 0), text or " ", font=font)
            return box[2] - box[0], box[3] - box[1]

        def wrap(text: str, font, max_width: int, max_lines: int = 3) -> list[str]:
            text = self._sanitize_image_text(str(text or ""))
            if not text:
                return []
            lines: list[str] = []
            current = ""
            for char in text:
                candidate = current + char
                if current and draw.textlength(candidate, font=font) > max_width:
                    lines.append(current)
                    current = char
                    if len(lines) >= max_lines:
                        return lines
                else:
                    current = candidate
            if current and len(lines) < max_lines:
                lines.append(current)
            return lines

        week_starts = self._schedule_week_starts(items)
        title_h = text_size(title, title_font)[1] + title_gap
        week_layouts: list[tuple[date, list[dict[str, Any]], dict[int, int], int]] = []
        total_weeks_h = 0
        for week_start in week_starts:
            week_items = [
                item for item in items
                if self._parse_dt(item["start_at"]).date() - timedelta(days=self._parse_dt(item["start_at"]).date().weekday()) == week_start
            ]
            week_items = self._merge_schedule_calendar_items(week_items, periods, period_numbers)
            row_heights = {period_no: base_row_h for period_no in period_numbers}
            for item in week_items:
                start_period, end_period = self._class_period_span(item, periods, period_numbers)
                if start_period not in row_heights:
                    continue
                text_lines = wrap(item.get("name", ""), item_font, int(day_w - 20), max_lines=4)
                needed_h = max(base_row_h, 12 + len(text_lines) * (text_size("课", item_font)[1] + 4))
                row_span = max(1, end_period - start_period + 1)
                per_row_h = (needed_h + row_span - 1) // row_span
                for period_no in range(start_period, end_period + 1):
                    if period_no in row_heights:
                        row_heights[period_no] = max(row_heights[period_no], per_row_h)
            week_h = day_header_h + sum(row_heights[period_no] for period_no in period_numbers)
            week_layouts.append((week_start, week_items, row_heights, week_h))
            total_weeks_h += week_h
        height = padding + title_h + total_weeks_h + max(0, len(week_layouts) - 1) * week_gap + padding
        image = Image.new("RGB", (width, height), "#f8f7f2")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((10, 10, width - 10, height - 10), radius=20, fill="#fffefa", outline="#ebe5d8", width=2)
        draw.rounded_rectangle((10, 10, 18, height - 10), radius=4, fill="#255f56")

        y = padding
        draw.text((padding + 8, y), title, fill="#183b36", font=title_font)
        draw.text((padding + 8, y + text_size(title, title_font)[1] + 6), f"{self._now().strftime('%m-%d %H:%M')} 生成", fill="#8a8275", font=small_font)
        y += title_h

        for week_start, week_items, row_heights, week_h in week_layouts:
            grid_x = padding
            grid_y = y
            draw.rectangle((grid_x, grid_y, width - padding, grid_y + week_h), fill="#ffffff", outline="#e6dfd2", width=1)

            for day_idx in range(7):
                day = week_start + timedelta(days=day_idx)
                x0 = grid_x + period_cols_w + day_idx * day_w
                x1 = grid_x + period_cols_w + (day_idx + 1) * day_w
                fill = "#f3f8f6" if day_idx < 5 else "#faf4ea"
                draw.rectangle((x0, grid_y, x1, grid_y + day_header_h), fill=fill)
                header = f"周{weekdays[day_idx]} {day.strftime('%m-%d')}"
                tw, th = text_size(header, header_font)
                draw.text((x0 + (day_w - tw) / 2, grid_y + (day_header_h - th) / 2), header, fill="#183b36", font=header_font)

            draw.rectangle((grid_x, grid_y, grid_x + period_cols_w, grid_y + day_header_h), fill="#f3f8f6")
            draw.text((grid_x + 12, grid_y + 16), "节", fill="#183b36", font=header_font)
            draw.text((grid_x + period_no_col_w + 18, grid_y + 16), "时间", fill="#183b36", font=header_font)
            row_tops: dict[int, int] = {}
            current_y = grid_y + day_header_h
            for row_idx, period_no in enumerate(period_numbers):
                y0 = current_y
                row_tops[period_no] = y0
                y1 = y0 + row_heights[period_no]
                current_y = y1
                period = periods.get(str(period_no), {})
                period_text = str(period_no)
                tw, th = text_size(period_text, header_font)
                draw.text((grid_x + (period_no_col_w - tw) / 2, y0 + (row_heights[period_no] - th) / 2), period_text, fill="#776f64", font=header_font)
                start_text = period.get("start", "")
                end_text = period.get("end", "")
                start_w, start_h = text_size(start_text, small_font)
                end_w, end_h = text_size(end_text, small_font)
                time_x = grid_x + period_no_col_w
                draw.text((time_x + (period_time_col_w - start_w) / 2, y0 + 8), start_text, fill="#776f64", font=small_font)
                draw.text((time_x + (period_time_col_w - end_w) / 2, y1 - end_h - 8), end_text, fill="#776f64", font=small_font)
                draw.line((grid_x, y1, width - padding, y1), fill="#ebe5d8", width=1)

            for day_idx in range(8):
                x = grid_x + period_cols_w + day_idx * day_w
                draw.line((x, grid_y, x, grid_y + week_h), fill="#e1dacd", width=1)
            draw.line((grid_x + period_no_col_w, grid_y, grid_x + period_no_col_w, grid_y + week_h), fill="#e1dacd", width=1)
            draw.line((grid_x + period_cols_w, grid_y, grid_x + period_cols_w, grid_y + week_h), fill="#d6cebf", width=2)
            draw.line((grid_x, grid_y + day_header_h, width - padding, grid_y + day_header_h), fill="#d6cebf", width=2)

            for idx, item in enumerate(week_items):
                start_dt = self._parse_dt(item["start_at"])
                day_idx = start_dt.date().weekday()
                start_period, end_period = self._class_period_span(item, periods, period_numbers)
                if start_period not in period_numbers:
                    continue
                x0 = grid_x + period_cols_w + day_idx * day_w + 1
                x1 = grid_x + period_cols_w + (day_idx + 1) * day_w - 1
                y0 = row_tops[start_period] + 1
                y1 = row_tops.get(end_period, row_tops[start_period]) + row_heights.get(end_period, base_row_h) - 1
                color = self._schedule_color(item, palette)
                draw.rectangle((x0, y0, x1, y1), fill=color, outline="#b9c9c3", width=1)
                text_x = x0 + 9
                text_y = y0 + 7
                text_max = int(x1 - x0 - 18)
                bottom_limit = y1 - 7
                name_line_h = text_size("课", item_font)[1] + 4
                available_h = max(0, int(bottom_limit - text_y))
                max_name_lines = max(1, min(4, available_h // max(1, name_line_h)))
                block_lines = wrap(item.get("name", ""), item_font, text_max, max_lines=max_name_lines)
                for line in block_lines:
                    line_h = text_size(line, item_font)[1]
                    if text_y + line_h > bottom_limit:
                        break
                    draw.text((text_x, text_y), line, fill="#202623", font=item_font)
                    text_y += line_h + 4

                details = []
                location = self._clean_text(str(item.get("location", "")).strip())
                teacher = self._schedule_teacher_display(item.get("teacher"))
                if location:
                    details.append(location)
                if teacher:
                    details.append(teacher)
                for detail in details:
                    detail = self._sanitize_image_text(detail)
                    detail_h = text_size(detail, small_font)[1]
                    if draw.textlength(detail, font=small_font) > text_max:
                        continue
                    if text_y + detail_h > bottom_limit:
                        break
                    draw.text((text_x, text_y), detail, fill="#4e5b55", font=small_font)
                    text_y += detail_h + 3

            y += week_h + week_gap

        out_path = self.image_dir / f"zju_schedule_{self._now().strftime('%Y%m%d_%H%M%S')}_{hashlib.sha1((title + str(len(items))).encode()).hexdigest()[:8]}.png"
        image.save(out_path)
        return [str(out_path)]

    def _merge_schedule_calendar_items(
        self,
        items: list[dict[str, Any]],
        periods: dict[str, dict[str, str]],
        period_numbers: list[int],
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for item in items:
            current = dict(item)
            start_period, end_period = self._class_period_span(current, periods, period_numbers)
            if start_period not in period_numbers:
                continue
            end_period = min(end_period, max(period_numbers))
            current["start_period"] = start_period
            current["end_period"] = end_period
            if start_period <= 5 < end_period and 6 in period_numbers:
                day = self._parse_dt(current["start_at"]).date()
                first = dict(current)
                second = dict(current)
                first["end_period"] = 5
                first["end_at"] = self._combine_day_time(day, periods.get("5", {}).get("end", "12:25")).isoformat()
                second["start_period"] = 6
                second["start_at"] = self._combine_day_time(day, periods.get("6", {}).get("start", "13:25")).isoformat()
                prepared.extend([first, second])
            else:
                prepared.append(current)

        groups: dict[tuple[date, str], list[dict[str, Any]]] = {}
        for item in prepared:
            item_day = self._parse_dt(item["start_at"]).date()
            item_name = self._clean_text(str(item.get("name", "")))
            groups.setdefault((item_day, item_name), []).append(item)

        merged: list[dict[str, Any]] = []
        for _, group_items in groups.items():
            group_items = sorted(
                group_items,
                key=lambda item: (
                    int(item.get("start_period") or 0),
                    int(item.get("end_period") or 0),
                    self._parse_dt(item["start_at"]),
                ),
            )
            group_merged: list[dict[str, Any]] = []
            for item in group_items:
                if not group_merged:
                    group_merged.append(dict(item))
                    continue

                prev = group_merged[-1]
                start_period = int(item.get("start_period") or 0)
                end_period = int(item.get("end_period") or start_period)
                prev_end = int(prev.get("end_period") or 0)
                touching_or_overlapping = start_period <= prev_end + 1
                crosses_midday_break = prev_end == 5 and start_period == 6
                if touching_or_overlapping and not crosses_midday_break:
                    if end_period > prev_end:
                        prev["end_period"] = end_period
                        prev["end_at"] = item.get("end_at", prev.get("end_at"))
                    prev["location"] = self._merge_schedule_detail(prev.get("location"), item.get("location"))
                    prev["teacher"] = self._merge_schedule_detail(prev.get("teacher"), item.get("teacher"))
                    continue

                group_merged.append(dict(item))
            merged.extend(group_merged)

        return sorted(
            merged,
            key=lambda item: (
                self._parse_dt(item["start_at"]).date(),
                int(item.get("start_period") or 0),
                self._clean_text(str(item.get("name", ""))),
            ),
        )

    def _merge_schedule_detail(self, left: Any, right: Any) -> str:
        left_text = self._clean_text(str(left or "").strip())
        right_text = self._clean_text(str(right or "").strip())
        if not left_text:
            return right_text
        if not right_text or right_text == left_text:
            return left_text
        return left_text

    def _schedule_teacher_display(self, raw: Any) -> str:
        text = self._clean_text(str(raw or "").strip())
        if text in {"", "未知", "待定", "无"}:
            return ""
        text = re.sub(r"[（(]\s*(任课)?教师\s*[）)]", "", text)
        text = re.sub(r"^(任课)?教师\s*[:：]?\s*", "", text)
        text = re.sub(r"^老师\s*[:：]\s*", "", text)
        bracket_match = re.fullmatch(r"[（(]\s*(.+?)\s*[）)]", text)
        if bracket_match:
            text = bracket_match.group(1)
        text = re.sub(r"\s*(任课)?教师\s*$", "", text)
        return self._clean_text(text)

    def _schedule_color(self, item: dict[str, Any], palette: list[str]) -> str:
        key = self._clean_text(str(item.get("name") or item.get("course_code") or ""))
        digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()
        return palette[int(digest[:8], 16) % len(palette)]

    def _class_period_span(
        self,
        item: dict[str, Any],
        periods: dict[str, dict[str, str]],
        period_numbers: list[int],
    ) -> tuple[int, int]:
        try:
            start_period = int(item.get("start_period") or 0)
            end_period = int(item.get("end_period") or start_period)
            if start_period in period_numbers:
                if end_period not in period_numbers:
                    if end_period > max(period_numbers):
                        end_period = max(period_numbers)
                    else:
                        end_period = start_period
                if end_period < start_period:
                    end_period = start_period
                return start_period, end_period
        except Exception:
            pass

        start_dt = self._parse_dt(item["start_at"])
        end_dt = self._parse_dt(item.get("end_at") or item["start_at"])
        start_hm = start_dt.strftime("%H:%M")
        end_hm = end_dt.strftime("%H:%M")
        start_period = None
        end_period = None
        for period_no in period_numbers:
            period = periods.get(str(period_no), {})
            if period.get("start") == start_hm:
                start_period = period_no
            if period.get("end") == end_hm:
                end_period = period_no
        if start_period is None:
            start_period = min(period_numbers, key=lambda p: abs(self._minutes(periods.get(str(p), {}).get("start", "00:00")) - self._minutes(start_hm)))
        if end_period is None:
            end_period = start_period
            for period_no in period_numbers:
                if self._minutes(periods.get(str(period_no), {}).get("end", "00:00")) <= self._minutes(end_hm):
                    end_period = period_no
        if end_period < start_period:
            end_period = start_period
        return start_period, end_period

    @staticmethod
    def _minutes(raw: str) -> int:
        try:
            hour, minute = [int(part) for part in str(raw or "00:00").split(":", 1)]
            return hour * 60 + minute
        except Exception:
            return 0

    def _render_query_cards(
        self,
        title: str,
        cards: list[dict[str, str]],
        columns: int = 1,
        min_width: int | None = None,
    ) -> list[str]:
        from PIL import Image, ImageDraw, ImageFont

        width = max(min_width or 760, self._cfg_int("query_image_width", 900))
        font_size = max(17, self._cfg_int("query_image_font_size", 24))
        padding = 22
        card_gap = 10
        column_gap = 12
        card_padding = 15
        split_after = max(1, self._cfg_int("query_image_split_after_items", 10))

        if len(cards) <= split_after:
            chunks = [cards]
        else:
            pivot = (len(cards) + 1) // 2
            chunks = [cards[:pivot], cards[pivot:]]

        font_path = self._cfg_str("query_image_font_path", DEFAULT_IMAGE_FONT_PATH)

        def load_font(size: int):
            for candidate in _image_font_candidates(font_path):
                try:
                    if candidate.exists():
                        return ImageFont.truetype(str(candidate), size=size)
                except Exception:
                    continue
            return ImageFont.load_default()

        title_font = load_font(font_size + 7)
        item_font = load_font(font_size)
        meta_font = load_font(max(16, font_size - 4))
        small_font = load_font(max(14, font_size - 7))

        def text_size(draw, text: str, font) -> tuple[int, int]:
            box = draw.textbbox((0, 0), text or " ", font=font)
            return box[2] - box[0], box[3] - box[1]

        def wrap(draw, text: str, font, max_width: int, preserve_newlines: bool = False) -> list[str]:
            if preserve_newlines:
                text = self._sanitize_image_text_preserve_newlines(str(text or "").strip())
            else:
                text = self._sanitize_image_text(str(text or "").strip())
            if not text:
                return [""]
            lines: list[str] = []
            for paragraph in text.splitlines():
                current = ""
                for char in paragraph:
                    candidate = current + char
                    if current and draw.textlength(candidate, font=font) > max_width:
                        lines.append(current)
                        current = char
                    else:
                        current = candidate
                if current:
                    lines.append(current)
            return lines or [text]

        result_paths: list[str] = []
        for page_no, chunk in enumerate(chunks[:2], start=1):
            page_columns = max(1, min(int(columns or 1), len(chunk)))
            inner_width = width - padding * 2
            card_width = (inner_width - column_gap * (page_columns - 1)) // page_columns
            time_col_width = min(180, max(132, int(card_width * 0.34)))
            text_width = max(120, card_width - card_padding * 2 - time_col_width - 12)
            dummy = Image.new("RGB", (width, 10), "#f8f7f2")
            draw = ImageDraw.Draw(dummy)
            title_lines = wrap(draw, title, title_font, width - padding * 2 - 18)
            title_h = sum(text_size(draw, line, title_font)[1] + 6 for line in title_lines)
            subtitle = f"{self._now().strftime('%m-%d %H:%M')} 生成"
            if len(chunks) > 1:
                subtitle += f" · 第 {page_no}/{len(chunks)} 页"

            card_blocks: list[tuple[dict[str, str], list[str], list[str], list[str], int]] = []
            content_h = padding + title_h + 26
            for card in chunk:
                time_text = self._card_time_for_image(card)
                time_parts = wrap(draw, time_text, meta_font, time_col_width - 10, preserve_newlines=True)
                title_parts = wrap(draw, card.get("title", ""), item_font, text_width)
                meta_parts = wrap(draw, card.get("meta", ""), meta_font, text_width)
                title_line_h = text_size(draw, "课", item_font)[1] + 7
                meta_line_h = text_size(draw, "课", meta_font)[1] + 5
                time_line_h = text_size(draw, "00", meta_font)[1] + 5
                text_h = len(title_parts) * title_line_h + 2 + len(meta_parts) * meta_line_h
                time_h = len(time_parts) * time_line_h
                row_h = max(card_padding * 2 + max(text_h, time_h), 104)
                card_blocks.append((card, time_parts, title_parts, meta_parts, row_h))
            for row_start in range(0, len(card_blocks), page_columns):
                row = card_blocks[row_start: row_start + page_columns]
                content_h += max(block[-1] for block in row) + card_gap

            height = max(220, content_h + padding)
            image = Image.new("RGB", (width, height), "#f8f7f2")
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((10, 10, width - 10, height - 10), radius=20, fill="#fffefa", outline="#ebe5d8", width=2)
            draw.rounded_rectangle((10, 10, 18, height - 10), radius=4, fill="#255f56")

            y = padding
            for line in title_lines:
                draw.text((padding + 6, y), line, fill="#183b36", font=title_font)
                y += text_size(draw, line, title_font)[1] + 6
            draw.text((padding + 6, y + 1), subtitle, fill="#8a8275", font=small_font)
            y += 28

            for row_start in range(0, len(card_blocks), page_columns):
                row = card_blocks[row_start: row_start + page_columns]
                row_h = max(block[-1] for block in row)
                for col_no, (_, time_parts, title_parts, meta_parts, _) in enumerate(row):
                    x0 = padding + col_no * (card_width + column_gap)
                    y0, x1, y1 = y, x0 + card_width, y + row_h
                    draw.rounded_rectangle((x0, y0, x1, y1), radius=14, fill="#ffffff", outline="#e6dfd2", width=1)
                    time_y = y0 + card_padding
                    for line in time_parts:
                        draw.text((x0 + card_padding, time_y), line, fill="#255f56", font=meta_font)
                        time_y += text_size(draw, line, meta_font)[1] + 5
                    divider_x = x0 + card_padding + time_col_width - 8
                    draw.line((divider_x, y0 + 14, divider_x, y1 - 14), fill="#ddd6c8", width=2)
                    tx = x0 + card_padding + time_col_width + 8
                    ty = y0 + card_padding
                    for line in title_parts:
                        draw.text((tx, ty), line, fill="#202623", font=item_font)
                        ty += text_size(draw, line, item_font)[1] + 7
                    ty += 2
                    for line in meta_parts:
                        draw.text((tx, ty), line, fill="#776f64", font=meta_font)
                        ty += text_size(draw, line, meta_font)[1] + 5
                y += row_h + card_gap

            out_path = self.image_dir / f"zju_query_{self._now().strftime('%Y%m%d_%H%M%S')}_{page_no}_{hashlib.sha1((title + str(page_no) + str(len(cards))).encode()).hexdigest()[:8]}.png"
            image.save(out_path)
            result_paths.append(str(out_path))
        return result_paths

    def _sanitize_image_text(self, text: str) -> str:
        text = EMOJI_RE.sub("", text or "")
        text = text.replace("\ufe0f", "").replace("\u200d", "")
        return re.sub(r"\s+", " ", text).strip()

    def _sanitize_image_text_preserve_newlines(self, text: str) -> str:
        text = EMOJI_RE.sub("", text or "")
        text = text.replace("\ufe0f", "").replace("\u200d", "")
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    def _compact_time(self, text: str) -> str:
        text = self._sanitize_image_text(text)
        match = re.search(r"(\d{2}-\d{2})\s+\S+\s+(\d{2}:\d{2}-\d{2}:\d{2})", text)
        if match:
            return f"{match.group(1)}\n{match.group(2)}"
        match = re.search(r"(?:截止\s*)?(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})\s+(\d{2}:\d{2})", text)
        if match:
            return f"{match.group(1)}\n{match.group(2)}"
        return text

    def _card_time_for_image(self, card: dict[str, str]) -> str:
        deadline_at = card.get("deadline_at")
        if deadline_at:
            deadline_dt = self._parse_dt(deadline_at)
            return f"{deadline_dt.strftime('%Y-%m-%d')}\n{deadline_dt.strftime('%H:%M')}"
        return self._compact_time(card.get("time", ""))

    def _task_deadline_dt(self, item: dict[str, Any]) -> datetime:
        due_at = item.get("due_at")
        if not due_at:
            raise ValueError(f"task missing due_at: {item.get('name', '')}")
        return self._parse_dt(str(due_at))

    def _build_refresh_summary(self, cache: dict[str, Any]) -> str:
        counts = cache.get("raw_counts", {})
        return "\n".join(
            [
                f"{MESSAGE_PREFIX}zdbk数据已刷新",
                f"{MESSAGE_PREFIX}课表/考试刷新时间：{cache.get('academic_refresh', '') or cache.get('last_refresh', '')}",
                f"{MESSAGE_PREFIX}DDL 刷新时间：{cache.get('task_refresh', '') or cache.get('last_refresh', '')}",
                f"{MESSAGE_PREFIX}课表事件：{len(cache.get('class_events', []))}（模板 {counts.get('class_templates', 0)}）",
                f"{MESSAGE_PREFIX}考试事件：{len(cache.get('exam_events', []))}",
                f"{MESSAGE_PREFIX}任务事件：{len(cache.get('task_events', []))}",
                f"{MESSAGE_PREFIX}校历来源：{counts.get('calendar_source', '')}",
            ]
        )

    def _cache_summary_payload(self, cache: dict[str, Any]) -> dict[str, Any]:
        counts = cache.get("raw_counts", {})
        return {
            "ok": True,
            "last_refresh": cache.get("last_refresh", ""),
            "academic_refresh": cache.get("academic_refresh", ""),
            "task_refresh": cache.get("task_refresh", ""),
            "class_event_count": len(cache.get("class_events", [])),
            "class_template_count": counts.get("class_templates", 0),
            "exam_event_count": len(cache.get("exam_events", [])),
            "task_event_count": len(cache.get("task_events", [])),
            "calendar_source": counts.get("calendar_source", ""),
            "calendar_updated_at": counts.get("calendar_updated_at", ""),
        }

    def _normalize_query_type(self, raw: str) -> str:
        text = str(raw or "").strip().lower()
        if self._looks_like_pta_query(text):
            return "pta_tasks"
        if text in {"schedule", "class", "classes", "course", "courses", "课表", "课程", "上课"}:
            return "schedule"
        if text in {"exam", "exams", "test", "tests", "考试", "考表"}:
            return "exams"
        if text in {
            "task",
            "tasks",
            "todo",
            "todos",
            "deadline",
            "deadlines",
            "ddl",
            "ddls",
            "任务",
            "待办",
            "截止",
            "作业",
        }:
            return "tasks"
        return text

    def _looks_like_pta_query(self, *values: str) -> bool:
        text = " ".join(str(value or "") for value in values).lower()
        return any(marker in text for marker in ("pta", "pintia", "拼题", "拼题a", "拼题 a"))

    def _normalize_query_range(self, raw: str) -> str:
        text = str(raw or "").strip()
        lower = text.lower()
        mapping = {
            "": "",
            "default": "",
            "today": "今天",
            "tomorrow": "明天",
            "this_week": "本周",
            "this week": "本周",
            "week": "本周",
            "next_week": "下周",
            "next week": "下周",
            "recent": "近7天",
            "upcoming": "近7天",
            "next_7_days": "近7天",
            "next 7 days": "近7天",
            "7d": "近7天",
            "all": "全部",
        }
        return mapping.get(lower, text)

    def _class_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        start_dt = self._parse_dt(item["start_at"])
        end_dt = self._parse_dt(item["end_at"])
        return {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "date": start_dt.strftime("%Y-%m-%d"),
            "start_time": start_dt.strftime("%H:%M"),
            "end_time": end_dt.strftime("%H:%M"),
            "location": item.get("location") or "地点待定",
        }

    def _exam_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        start_dt = self._parse_dt(item["start_at"])
        payload = {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "date": start_dt.strftime("%Y-%m-%d"),
            "start_time": start_dt.strftime("%H:%M"),
            "location": item.get("location") or "地点待定",
        }
        end_at = item.get("end_at")
        if end_at:
            payload["end_time"] = self._parse_dt(end_at).strftime("%H:%M")
        return payload

    def _task_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        due_dt = self._task_deadline_dt(item)
        platform = self._task_platform_label(item)
        return {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "deadline_at": due_dt.isoformat(),
            "deadline_text": due_dt.strftime("%Y-%m-%d %H:%M"),
            "due_date": due_dt.strftime("%Y-%m-%d"),
            "due_time": due_dt.strftime("%H:%M"),
            "course": item.get("course") or "",
            "source": platform,
            "url": item.get("url") or "",
        }

    def _format_class_reminder(self, item: dict[str, Any], offset: int) -> str:
        start_dt = self._parse_dt(item["start_at"])
        return (
            f"{MESSAGE_PREFIX}上课提醒：{offset} 分钟后有课\n"
            f"{item['name']}\n"
            f"时间：{start_dt.strftime('%m-%d %H:%M')}\n"
            f"地点：{item.get('location') or '地点待定'}"
        )

    def _format_exam_reminder(self, item: dict[str, Any], offset: int) -> str:
        start_dt = self._parse_dt(item["start_at"])
        return (
            f"{MESSAGE_PREFIX}考试提醒：{offset} 分钟后开始\n"
            f"{item['name']}\n"
            f"时间：{start_dt.strftime('%m-%d %H:%M')}\n"
            f"地点：{item.get('location') or '地点待定'}"
        )

    def _format_task_reminder(self, item: dict[str, Any], offset: int) -> str:
        due_dt = self._parse_dt(item["due_at"])
        extra = item.get("source") or item.get("course") or item.get("location") or "学在浙大"
        return (
            f"{MESSAGE_PREFIX}任务提醒：{offset} 分钟后截止\n"
            f"{item['name']}\n"
            f"截止：{due_dt.strftime('%m-%d %H:%M')}\n"
            f"来源：{extra}"
        )

    def _build_reminder_prompt(self, event_type: str, item: dict[str, Any], offset: int) -> str:
        if event_type == "task":
            event_dt = self._parse_dt(item["due_at"])
            type_label = "PTA 任务截止" if item.get("source") == "PTA" else "学在浙大任务截止"
            time_label = "截止时间"
            location = item.get("source") or item.get("course") or item.get("location") or "学在浙大"
        else:
            event_dt = self._parse_dt(item["start_at"])
            type_label = "课程上课" if event_type == "class" else "考试开始"
            time_label = "开始时间"
            location = item.get("location") or "地点待定"

        payload = {
            "提醒类型": type_label,
            "剩余时间分钟": offset,
            "名称": item.get("name", ""),
            time_label: event_dt.strftime("%Y-%m-%d %H:%M"),
            "地点或来源": location,
        }
        return (
            "请根据下面 JSON 生成一条自然、简洁、可直接发给学生的提醒。\n"
            "要求：\n"
            "0. 输出必须以【ZJU-Academic】开头。\n"
            "1. 语气要服从系统人设，但不要生硬套模板，也不要过度卖萌。\n"
            "2. 必须包含事件名称、时间、地点或来源；任务提醒要强调截止。\n"
            "3. 不要输出“让我看看”“我来查一下”“根据数据”等过程性话术。\n"
            "4. 控制在 1 到 3 行。\n\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    def _compose_persona_system_prompt(self, task_prompt: str) -> str:
        persona = self._load_persona_prompt()
        if not persona:
            return task_prompt
        return (
            persona.strip()
            + "\n\n[当前插件任务]\n"
            + task_prompt.strip()
            + "\n\n[强制约束]\n"
            + "你正在发送自动课业提醒。不得输出工具调用过程、不得说“让我看看/让我找找”。"
            + "不要编造事件信息；如果人设与事实准确性冲突，以事实准确性为先。"
        )

    def _load_persona_prompt(self) -> str:
        override = self._cfg_str("persona_prompt_override", "")
        if override:
            return override
        if not self._cfg_bool("use_astrbot_persona_prompt", True):
            return ""

        config_path = Path("/AstrBot/data/cmd_config.json")
        db_path = Path("/AstrBot/data/data_v4.db")
        try:
            default_persona = "Main"
            if config_path.exists():
                cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
                default_persona = str(
                    cfg.get("provider_settings", {}).get("default_personality")
                    or cfg.get("default_personality")
                    or "Main"
                )
            if db_path.exists():
                uri = f"file:{db_path}?mode=ro"
                with sqlite3.connect(uri, uri=True) as conn:
                    row = conn.execute(
                        "select system_prompt from personas where persona_id = ?",
                        (default_persona,),
                    ).fetchone()
                if row and str(row[0]).strip():
                    return str(row[0]).strip()
        except Exception:
            logger.warning("failed to load AstrBot persona prompt for zju reminders", exc_info=True)
        return ""

    async def _resolve_provider_id(self, *, umo: str | None = None, config_key: str = "chat_provider_id") -> str:
        configured = self._cfg_str(config_key, "")
        if configured:
            return configured
        if config_key != "chat_provider_id":
            fallback = self._cfg_str("chat_provider_id", "")
            if fallback:
                return fallback
        if not umo:
            return ""
        try:
            return await self.context.get_current_chat_provider_id(umo)
        except Exception:
            return ""

    def _reminder_targets(self) -> list[str]:
        if not self._cfg_bool("bound_sessions_only", True):
            return list(self._target_bindings().keys())
        return list(self._target_bindings().keys())

    def _default_state(self) -> dict[str, Any]:
        return {"bindings": {}, "reminder_log": {}, "pta_session": {}, "pta_login_token": ""}

    def _default_cache(self) -> dict[str, Any]:
        return migrate_cache({
            "last_refresh": "",
            "academic_refresh": "",
            "task_refresh": "",
            "class_events": [],
            "exam_events": [],
            "task_events": [],
            "raw_counts": {},
        })

    def _bindings(self) -> dict[str, Any]:
        return self._state.setdefault("bindings", {})

    def _configured_bindings(self) -> dict[str, Any]:
        raw = self._cfg_str("bound_sessions", "")
        result: dict[str, Any] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            umo, sep, label = line.partition("|")
            umo = umo.strip()
            if not umo:
                continue
            result[umo] = {
                "label": label.strip() if sep and label.strip() else umo,
                "bound_at": "config",
                "class_reminders": True,
                "exam_reminders": True,
                "task_reminders": True,
                "source": "config",
            }
        return result

    def _target_bindings(self) -> dict[str, Any]:
        merged = dict(self._bindings())
        merged.update(self._configured_bindings())
        return merged

    def _has_reminded(self, key: str) -> bool:
        return key in self._state.setdefault("reminder_log", {})

    def _mark_reminded(self, key: str):
        reminder_log = self._state.setdefault("reminder_log", {})
        reminder_log[key] = self._now().isoformat()
        if len(reminder_log) > 2000:
            keys = sorted(reminder_log.items(), key=lambda x: x[1])
            for item_key, _ in keys[:300]:
                reminder_log.pop(item_key, None)

    def _reminder_key(self, event_type: str, umo: str, event_id: str, offset: int) -> str:
        return f"{event_type}|{umo}|{event_id}|{offset}"

    def _is_cache_fresh(self) -> bool:
        return self._is_academic_cache_fresh() and self._is_task_cache_fresh()

    def _is_academic_cache_fresh(self) -> bool:
        refreshed_at = self._cache_time("academic_refresh")
        if not refreshed_at:
            return False
        week_start = self._now().date() - timedelta(days=self._now().date().weekday())
        if self._cache.get("class_events_from") != week_start.isoformat():
            return False
        ttl = timedelta(seconds=ZJU_ACADEMIC_DATA_CACHE_SECONDS)
        return self._now() - refreshed_at < ttl

    def _is_task_cache_fresh(self) -> bool:
        refreshed_at = self._cache_time("task_refresh")
        if not refreshed_at:
            return False
        ttl = timedelta(minutes=max(1, self._cfg_int("cache_ttl_minutes", 30)))
        return self._now() - refreshed_at < ttl

    def _cache_time(self, key: str) -> datetime | None:
        raw = self._cfg_like_str(self._cache.get(key, ""))
        if not raw:
            raw = self._cfg_like_str(self._cache.get("last_refresh", ""))
        if not raw:
            return None
        try:
            return self._parse_dt(raw)
        except Exception:
            return None

    def _load_json_file(self, path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return fallback
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            logger.exception(f"failed to load json file: {path}")
            return fallback

    async def _save_state(self):
        async with self._state_lock:
            payload = json.dumps(self._state, ensure_ascii=False, indent=2)
            tmp_path = self.state_path.with_suffix(".tmp")
            await asyncio.to_thread(tmp_path.write_text, payload, encoding="utf-8")
            await asyncio.to_thread(tmp_path.replace, self.state_path)
            await asyncio.to_thread(self._chmod_private, self.state_path)

    async def _save_cache(self):
        payload = json.dumps(self._cache, ensure_ascii=False, indent=2)
        tmp_path = self.cache_path.with_suffix(".tmp")
        await asyncio.to_thread(tmp_path.write_text, payload, encoding="utf-8")
        await asyncio.to_thread(tmp_path.replace, self.cache_path)

    def _cfg_value(self, key: str, default: Any) -> Any:
        if key in self.config:
            return self.config.get(key, default)
        for group_key in ("basic", "advanced"):
            group = self.config.get(group_key)
            if isinstance(group, dict) and key in group:
                return group.get(key, default)
        return default

    def _cfg_str(self, key: str, default: str) -> str:
        value = self._cfg_value(key, default)
        return str(value).strip() if value is not None else default

    def _cfg_int(self, key: str, default: int) -> int:
        value = self._cfg_value(key, default)
        try:
            return int(value)
        except Exception:
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg_value(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _username(self) -> str:
        return self._cfg_str("username", "")

    def _password(self) -> str:
        return self._cfg_str("password", "")

    def _pta_enabled(self) -> bool:
        return self._cfg_bool("pta_enabled", True)

    def _pta_saved_session_cookie(self) -> str:
        saved = self._state.get("pta_session") if isinstance(self._state, dict) else None
        if not isinstance(saved, dict):
            return ""
        return self._cfg_like_str(saved.get("cookie", ""))

    def _pta_effective_session_cookie(self) -> str:
        return self._pta_saved_session_cookie()

    def _pta_available(self) -> bool:
        return bool(self._pta_enabled() and self._pta_effective_session_cookie())

    def _pta_status_label(self) -> str:
        if not self._pta_enabled():
            return f"{MESSAGE_PREFIX}未启用"
        if self._pta_effective_session_cookie():
            return f"{MESSAGE_PREFIX}已登录"
        return f"{MESSAGE_PREFIX}未登录"

    def _cfg_like_str(self, value: Any) -> str:
        return str(value).strip() if value is not None else ""

    @staticmethod
    def _chmod_private(path: Path):
        try:
            path.chmod(0o600)
        except Exception:
            pass

    def _tz(self) -> Any:
        try:
            return ZoneInfo(self._cfg_str("timezone", "Asia/Shanghai"))
        except Exception:
            try:
                return ZoneInfo("Asia/Shanghai")
            except Exception:
                return timezone(timedelta(hours=8), "Asia/Shanghai")

    def _now(self) -> datetime:
        return datetime.now(self._tz())

    def _parse_dt(self, raw: str) -> datetime:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self._tz())
        return dt.astimezone(self._tz())

    def _parse_date(self, raw: str) -> date:
        return datetime.strptime(raw, "%Y-%m-%d").date()

    def _combine_day_time(self, day: date, raw: str) -> datetime:
        hour, minute = [int(part) for part in raw.split(":", 1)]
        return datetime.combine(day, time(hour=hour, minute=minute), tzinfo=self._tz())

    def _periods(self) -> dict[str, dict[str, str]]:
        return DEFAULT_PERIODS

    def _academic_calendar_config(self, force: bool = False) -> dict[str, Any]:
        manual = self._manual_calendar_config()
        if self._cfg_bool("auto_calendar_enabled", True):
            cached = self._load_calendar_cache_sync()
            if not force and self._is_calendar_cache_fresh(cached):
                cached = dict(cached)
                cached["source"] = f"{cached.get('source', 'zju-ical-py')} cache"
                return cached
            fetched = self._fetch_zju_ical_py_calendar_config()
            if fetched.get("term_configs"):
                self._save_calendar_cache_sync(fetched)
                return fetched
            if cached.get("term_configs"):
                cached = dict(cached)
                cached["source"] = f"{cached.get('source', 'zju-ical-py')} cache"
                return cached

        if manual.get("term_configs"):
            return manual
        cached = self._load_calendar_cache_sync()
        if cached.get("term_configs"):
            cached = dict(cached)
            cached["source"] = f"{cached.get('source', 'zju-ical-py')} cache"
            return cached
        return {"source": "empty", "updated_at": "", "term_configs": [], "holiday_tweaks": []}

    def _is_calendar_cache_fresh(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict) or not payload.get("term_configs"):
            return False
        fetched_at = self._cfg_like_str(payload.get("fetched_at", ""))
        if not fetched_at:
            return False
        try:
            refreshed_at = self._parse_dt(fetched_at)
        except Exception:
            return False
        ttl = timedelta(seconds=ZJU_ICAL_PY_CONFIG_CACHE_SECONDS)
        return self._now() - refreshed_at < ttl

    def _merge_calendar_configs(self, primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        if not fallback.get("term_configs"):
            return dict(primary)
        merged = dict(primary)
        term_configs_by_key = {
            (item["year"], int(item["term"])): dict(item)
            for item in primary.get("term_configs", [])
        }
        added_manual = False
        for item in fallback.get("term_configs", []):
            key = (item["year"], int(item["term"]))
            if key not in term_configs_by_key:
                term_configs_by_key[key] = dict(item)
                added_manual = True
        merged["term_configs"] = sorted(term_configs_by_key.values(), key=lambda x: (x["begin"], x["term"]))
        merged["holiday_tweaks"] = list(primary.get("holiday_tweaks", []))
        if added_manual:
            merged["source"] = f"{primary.get('source', 'calendar')}+manual"
        return merged

    def _fetch_zju_ical_py_calendar_config(self) -> dict[str, Any]:
        term_configs_by_key: dict[tuple[str, int], dict[str, Any]] = {}
        tweaks_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        latest_updated_at = ""
        with requests.Session() as session:
            for file_name in self._zju_ical_py_config_file_names():
                payload = self._fetch_zju_ical_py_config_file(session, file_name)
                if not payload:
                    continue
                parsed = self._parse_zju_ical_py_config(payload)
                latest_updated_at = max(latest_updated_at, parsed.get("updated_at", ""))
                for item in parsed.get("term_configs", []):
                    term_configs_by_key[(item["year"], int(item["term"]))] = item
                for item in parsed.get("holiday_tweaks", []):
                    tweaks_by_key[(item["type"], item["from"], item["to"])] = item

        term_configs = sorted(term_configs_by_key.values(), key=lambda x: (x["begin"], x["term"]))
        holiday_tweaks = sorted(tweaks_by_key.values(), key=lambda x: (x["from"], x["to"], x["type"]))
        return {
            "source": "zju-ical-py",
            "updated_at": latest_updated_at,
            "fetched_at": self._now().isoformat(),
            "term_configs": term_configs,
            "holiday_tweaks": holiday_tweaks,
        }

    def _zju_ical_py_config_file_names(self) -> list[str]:
        today = self._now().date()
        academic_year_start = today.year if today.month >= 8 else today.year - 1
        candidates = ["config.json"]
        for year in range(academic_year_start - 1, academic_year_start + 2):
            candidates.append(f"config.{year}-{year + 1}.FW.json")
            candidates.append(f"config.{year}-{year + 1}.SS.json")

        manual_year = self._cfg_str("manual_calendar_year", "")
        if re.fullmatch(r"\d{4}-\d{4}", manual_year):
            candidates.append(f"config.{manual_year}.FW.json")
            candidates.append(f"config.{manual_year}.SS.json")

        result: list[str] = []
        for item in candidates:
            if item not in result:
                result.append(item)
        return result

    def _fetch_zju_ical_py_config_file(self, session: requests.Session, file_name: str) -> dict[str, Any] | None:
        for base_url in ZJU_ICAL_PY_CONFIG_BASE_URLS:
            url = f"{base_url.rstrip('/')}/{file_name}"
            try:
                resp = session.get(url, timeout=ZJU_ICAL_PY_CONFIG_TIMEOUT_SECONDS)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                payload = resp.json()
                return payload if isinstance(payload, dict) else None
            except Exception:
                logger.warning(f"failed to fetch zju-ical-py calendar config: {url}", exc_info=True)
        return None

    def _parse_zju_ical_py_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        term_configs: list[dict[str, Any]] = []
        for item in payload.get("termConfigs", []):
            if not isinstance(item, dict):
                continue
            year = self._clean_text(str(item.get("Year", "")))
            term_name = self._clean_text(str(item.get("Term", "")))
            term = TERM_NAME_TO_ID.get(term_name)
            begin = self._parse_compact_date(item.get("Begin"))
            end = self._parse_compact_date(item.get("End"))
            if not year or term is None or not begin or not end:
                continue
            term_configs.append(
                {
                    "year": year,
                    "term": term,
                    "begin": begin.isoformat(),
                    "end": end.isoformat(),
                    "first_week_no": int(item.get("FirstWeekNo", 1) or 1),
                }
            )

        holiday_tweaks: list[dict[str, Any]] = []
        for item in payload.get("tweaks", []):
            if not isinstance(item, dict):
                continue
            from_day = self._parse_compact_date(item.get("From"))
            to_day = self._parse_compact_date(item.get("To"))
            if not from_day or not to_day:
                continue
            tweak_type = self._clean_text(str(item.get("TweakType", "")))
            from_text = from_day.isoformat()
            to_text = to_day.isoformat()
            if tweak_type == "Clear":
                holiday_tweaks.append({"type": TWEAK_CLEAR, "from": from_text, "to": to_text})
            elif tweak_type == "Copy":
                holiday_tweaks.append({"type": TWEAK_COPY, "from": from_text, "to": to_text})
            elif tweak_type == "Move":
                holiday_tweaks.append({"type": TWEAK_COPY, "from": from_text, "to": to_text})
                holiday_tweaks.append({"type": TWEAK_CLEAR, "from": from_text, "to": from_text})
            elif tweak_type == "Exchange":
                holiday_tweaks.append({"type": TWEAK_EXCHANGE, "from": from_text, "to": to_text})

        updated_at = ""
        raw_updated_at = payload.get("lastUpdated")
        updated_day = self._parse_compact_date(raw_updated_at)
        if updated_day:
            updated_at = updated_day.isoformat()

        return {
            "updated_at": updated_at,
            "term_configs": term_configs,
            "holiday_tweaks": holiday_tweaks,
        }

    def _manual_calendar_config(self) -> dict[str, Any]:
        if not self._cfg_bool("manual_calendar_enabled", False):
            return {"source": "manual", "updated_at": "", "term_configs": [], "holiday_tweaks": []}
        year = self._cfg_str("manual_calendar_year", "2025-2026")
        terms = (
            (TERM_AUTUMN, self._cfg_str("manual_autumn_begin", ""), self._cfg_str("manual_autumn_end", "")),
            (TERM_WINTER, self._cfg_str("manual_winter_begin", ""), self._cfg_str("manual_winter_end", "")),
            (TERM_SPRING, self._cfg_str("manual_spring_begin", ""), self._cfg_str("manual_spring_end", "")),
            (TERM_SUMMER, self._cfg_str("manual_summer_begin", ""), self._cfg_str("manual_summer_end", "")),
        )
        term_configs: list[dict[str, Any]] = []
        for term, raw_begin, raw_end in terms:
            begin = self._parse_flexible_date(raw_begin)
            end = self._parse_flexible_date(raw_end)
            if not begin or not end or end < begin:
                continue
            term_configs.append(
                {
                    "year": year,
                    "term": term,
                    "begin": begin.isoformat(),
                    "end": end.isoformat(),
                    "first_week_no": 1,
                }
            )
        term_configs.sort(key=lambda x: (x["begin"], x["term"]))
        return {
            "source": "manual",
            "updated_at": "",
            "term_configs": term_configs,
            "holiday_tweaks": [],
        }

    def _load_calendar_cache_sync(self) -> dict[str, Any]:
        return self._load_json_file(self.calendar_cache_path, {})

    def _save_calendar_cache_sync(self, payload: dict[str, Any]):
        tmp_path = self.calendar_cache_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.calendar_cache_path)

    def _parse_compact_date(self, value: Any) -> date | None:
        text = self._clean_text(str(value or ""))
        if not text:
            return None
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").date()
        return self._parse_flexible_date(text)

    def _parse_flexible_date(self, value: Any) -> date | None:
        text = self._clean_text(str(value or ""))
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    def _class_offsets(self) -> list[int]:
        return self._parse_offsets("class_reminder_offsets_minutes", "30")

    def _exam_offsets(self) -> list[int]:
        return self._parse_offsets("exam_reminder_offsets_minutes", "1440,180,30")

    def _task_offsets(self) -> list[int]:
        return self._parse_offsets("task_reminder_offsets_minutes", "1440,180,30")

    def _parse_offsets(self, key: str, default: str) -> list[int]:
        raw = self._cfg_str(key, default)
        values: list[int] = []
        for chunk in raw.replace("，", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                num = int(chunk)
            except Exception:
                continue
            if num >= 0:
                values.append(num)
        return sorted(set(values), reverse=True)

    def _format_offsets(self, values: list[int]) -> str:
        return ", ".join(f"{item} 分钟" for item in values) if values else "无"

    def _unique_class_terms(self, term_configs: list[dict[str, Any]]) -> list[tuple[str, int]]:
        result = {(item["year"], int(item["term"])) for item in term_configs if int(item["term"]) in TERM_LABELS}
        return sorted(result, key=lambda x: (x[0], x[1]))

    def _unique_exam_terms(self, term_configs: list[dict[str, Any]]) -> list[tuple[str, int]]:
        result = set()
        for item in term_configs:
            term = int(item["term"])
            if term in {TERM_AUTUMN, TERM_WINTER}:
                result.add((item["year"], EXAM_AUTUMN_WINTER))
            elif term in {TERM_SPRING, TERM_SUMMER}:
                result.add((item["year"], EXAM_SPRING_SUMMER))
        return sorted(result, key=lambda x: (x[0], x[1]))

    def _monday_of_first_week(self, begin: date, first_week_no: int) -> date:
        weekday = begin.isoweekday()
        return begin - timedelta(days=weekday - 1) - timedelta(weeks=max(0, first_week_no - 1))

    def _week_number(self, monday_of_first_week: date, target: date) -> int:
        return ((target - monday_of_first_week).days // 7) + 1

    def _is_even_week(self, monday_of_first_week: date, target: date) -> bool:
        return self._week_number(monday_of_first_week, target) % 2 == 0

    def _session_label(self, event: AstrMessageEvent) -> str:
        return event.unified_msg_origin or "unknown"

    def _stable_id(self, *parts: str) -> str:
        payload = "|".join(parts)
        return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _prefixed_text(self, text: str) -> str:
        clean = str(text or "").strip()
        if clean.startswith(MESSAGE_PREFIX):
            return clean
        return f"{MESSAGE_PREFIX}{clean}" if clean else MESSAGE_PREFIX

    def _coerce_task_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=self._tz())
            return value.astimezone(self._tz())
        text = self._clean_text(str(value))
        if not text:
            return None
        candidates = [text, text.replace("Z", "+00:00")]
        for candidate in candidates:
            try:
                dt = datetime.fromisoformat(candidate)
            except ValueError:
                continue
            if dt.tzinfo is None:
                return dt.replace(tzinfo=self._tz())
            return dt.astimezone(self._tz())
        match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", text)
        if not match:
            return None
        year, month, day, hour, minute, second = [int(part or 0) for part in match.groups()]
        return datetime(year, month, day, hour, minute, second, tzinfo=self._tz())


class PintiaClient:
    LOGIN_URL = "https://passport.pintia.cn/api/users/sessions"
    PROBLEM_SETS_URL = "https://pintia.cn/api/problem-sets"

    def __init__(
        self,
        session_cookie: str = "",
        username: str = "",
        password: str = "",
        timeout: int = 20,
    ):
        self.timeout = timeout
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self.cookie_header = self._normalize_cookie(session_cookie)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://pintia.cn/problem-sets",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            }
        )

    def get_active_problem_sets(self, now: datetime | None = None) -> list[dict[str, Any]]:
        self._ensure_session()
        now_utc = now.astimezone(timezone.utc) if now and now.tzinfo else datetime.now(timezone.utc)
        filter_payload = {"endAtAfter": now_utc.isoformat().replace("+00:00", "Z")}
        resp = self.session.get(
            self.PROBLEM_SETS_URL,
            params={"filter": json.dumps(filter_payload, ensure_ascii=False)},
            headers={"Cookie": self.cookie_header},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        problem_sets = payload.get("problemSets")
        if not isinstance(problem_sets, list):
            raise RuntimeError(PTA_LOGIN_FAILED_MESSAGE)
        return [item for item in problem_sets if isinstance(item, dict)]

    def _ensure_session(self):
        if self.cookie_header:
            return
        if not self.username or not self.password:
            raise RuntimeError(PTA_CREDENTIALS_REQUIRED_MESSAGE)

        self.login()

    def login(self, ticket: str = "", rand_str: str = "") -> str:
        resp = self.session.post(
            self.LOGIN_URL,
            json=self._login_payload(ticket=ticket, rand_str=rand_str),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Referer": "https://pintia.cn/",
                "Origin": "https://pintia.cn",
            },
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(self._login_error(resp))

        session_value = self._read_session_cookie(resp)
        if not session_value:
            raise RuntimeError(PTA_LOGIN_FAILED_MESSAGE)
        self.cookie_header = self._normalize_cookie(session_value)
        return session_value

    def _login_payload(self, ticket: str = "", rand_str: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "password": self.password,
            "rememberMe": True,
            "inMiniProgram": False,
        }
        if "@" in self.username:
            payload["email"] = self.username
        else:
            payload["phone"] = self.username
        ticket = str(ticket or "").strip()
        rand_str = str(rand_str or "").strip()
        if ticket:
            payload["ticket"] = ticket
        if rand_str:
            payload["randStr"] = rand_str
        return payload

    def _login_error(self, resp: requests.Response) -> str:
        return PTA_LOGIN_FAILED_MESSAGE

    def _read_session_cookie(self, resp: requests.Response) -> str:
        cookie_value = self.session.cookies.get("PTASession") or resp.cookies.get("PTASession")
        if cookie_value:
            return str(cookie_value).strip()
        set_cookie = resp.headers.get("Set-Cookie", "")
        match = re.search(r"PTASession=([^;]+)", set_cookie)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _normalize_cookie(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        if "PTASession=" in text:
            return text
        return f"PTASession={text}"
