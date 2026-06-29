import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from academic_core.messages import DATA_FETCH_CACHE_MESSAGE, NEXT_TERM_CALENDAR_PENDING_MESSAGE
from academic_core.models import SourceResult, SourceStatus, migrate_cache
from academic_core.plugin_integration import (
    AcademicPluginRuntime,
    calendar_has_current_or_future_term,
    calendar_refresh_state,
    parse_repository_calendar_config,
    run_background_tick,
    source_status_payload,
)


NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone(timedelta(hours=8)))
ERROR_MESSAGE = DATA_FETCH_CACHE_MESSAGE


class SourceFailure(RuntimeError):
    def __init__(self, code, user_message):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


class PluginIntegrationTest(unittest.TestCase):
    def test_startup_migrates_legacy_cache_and_preserves_events(self):
        runtime = AcademicPluginRuntime(
            {
                "academic_refresh": "2026-06-20T10:53:00+08:00",
                "task_refresh": "2026-06-20T22:25:00+08:00",
                "class_events": [{"id": "class-1"}],
                "exam_events": [{"id": "exam-1"}],
                "task_events": [{"id": "task-1"}],
            },
            now_provider=lambda: NOW,
        )

        self.assertEqual(runtime.cache["class_events"], [{"id": "class-1"}])
        self.assertEqual(runtime.cache["exam_events"], [{"id": "exam-1"}])
        self.assertEqual(runtime.cache["task_events"], [{"id": "task-1"}])
        self.assertEqual(runtime.cache["schema_version"], 2)

    def test_schedule_failure_does_not_block_exams_or_tasks(self):
        runtime = AcademicPluginRuntime(
            migrate_cache(
                {
                    "academic_refresh": "2026-06-20T10:53:00+08:00",
                    "class_events": [{"id": "old-class"}],
                }
            ),
            now_provider=lambda: NOW,
        )

        result = runtime.refresh(
            {
                "calendar": lambda: SourceResult(data={"term_configs": []}),
                "schedule": lambda: (_ for _ in ()).throw(
                    SourceFailure("upstream_http", "学校接口返回 HTTP 504")
                ),
                "exams": lambda: SourceResult(data=[{"id": "exam-new"}]),
                "tasks": lambda: SourceResult(data=[{"id": "task-new", "due_at": NOW.isoformat()}]),
            }
        )

        self.assertEqual(result.cache["class_events"], [{"id": "old-class"}])
        self.assertEqual(result.cache["exam_events"], [{"id": "exam-new"}])
        self.assertEqual(result.cache["task_events"], [{"id": "task-new", "due_at": NOW.isoformat()}])
        self.assertEqual(
            result.cache["source_health"]["schedule"]["status"],
            SourceStatus.FAILED.value,
        )

    def test_background_refresh_exception_still_runs_reminders(self):
        calls = []

        async def refresh():
            calls.append("refresh")
            raise RuntimeError("refresh failed")

        async def notify():
            calls.append("notify")

        async def remind():
            calls.append("remind")

        result = asyncio.run(run_background_tick(refresh, notify, remind))

        self.assertEqual(calls, ["refresh", "notify", "remind"])
        self.assertFalse(result["refresh_ok"])
        self.assertTrue(result["notification_ok"])

    def test_calendar_pending_keeps_templates_and_query_is_annotated(self):
        runtime = AcademicPluginRuntime(
            migrate_cache(
                {
                    "academic_refresh": "2026-06-20T10:53:00+08:00",
                    "class_events": [{"id": "old-class"}],
                }
            ),
            now_provider=lambda: NOW,
        )
        runtime.refresh(
            {
                "schedule": lambda: SourceResult(
                    data={
                        "templates": [{"id": "template-1"}],
                        "events": [{"id": "should-not-be-used"}],
                    },
                    metadata={
                        "status": "calendar_pending",
                        "message": NEXT_TERM_CALENDAR_PENDING_MESSAGE,
                    },
                )
            }
        )

        self.assertEqual(runtime.cache["source_data"]["schedule_templates"], [{"id": "template-1"}])
        self.assertEqual(runtime.cache["class_events"], [{"id": "old-class"}])
        payload = runtime.annotate_query_payload("schedule", {"ok": True})
        self.assertEqual(payload["source_status"]["status"], SourceStatus.WAITING_CALENDAR.value)
        self.assertEqual(payload["source_status"]["message"], NEXT_TERM_CALENDAR_PENDING_MESSAGE)

    def test_calendar_recovery_replaces_schedule_events_and_clears_query_annotation(self):
        runtime = AcademicPluginRuntime(
            migrate_cache(
                {
                    "academic_refresh": "2026-06-20T10:53:00+08:00",
                    "class_events": [{"id": "old-class"}],
                }
            ),
            now_provider=lambda: NOW,
        )
        runtime.refresh(
            {
                "schedule": lambda: SourceResult(
                    data={"templates": [{"id": "template-1"}], "events": []},
                    metadata={"status": "calendar_pending"},
                )
            }
        )
        runtime.refresh(
            {
                "schedule": lambda: SourceResult(
                    data={
                        "templates": [{"id": "template-1"}],
                        "events": [{"id": "new-class"}],
                    }
                )
            },
            force=True,
        )

        self.assertEqual(runtime.cache["class_events"], [{"id": "new-class"}])
        self.assertNotIn("source_status", runtime.annotate_query_payload("schedule", {"ok": True}))

    def test_status_annotation_only_applies_to_relevant_source(self):
        runtime = AcademicPluginRuntime(
            migrate_cache({"class_events": [{"id": "class-1"}]}),
            now_provider=lambda: NOW,
        )
        runtime.refresh(
            {
                "tasks": lambda: (_ for _ in ()).throw(
                    SourceFailure("upstream_http", "任务接口失败")
                )
            }
        )

        self.assertNotIn("source_status", runtime.annotate_query_payload("schedule", {"ok": True}))
        status = runtime.annotate_query_payload("tasks", {"ok": True})["source_status"]
        self.assertEqual(status["status"], SourceStatus.FAILED.value)
        self.assertEqual(status["message"], ERROR_MESSAGE)

    def test_post_known_term_vacation_is_not_calendar_pending(self):
        today = datetime(2026, 6, 29, 12, 0, tzinfo=timezone(timedelta(hours=8))).date()
        past_terms = [
            {"year": "2025-2026", "term": 0, "begin": "2025-09-15", "end": "2025-11-09"},
            {"year": "2025-2026", "term": 1, "begin": "2025-11-10", "end": "2026-01-04"},
            {"year": "2025-2026", "term": 4, "begin": "2026-03-02", "end": "2026-04-26"},
            {"year": "2025-2026", "term": 5, "begin": "2026-04-27", "end": "2026-06-21"},
        ]

        self.assertEqual(calendar_refresh_state(today, past_terms), "vacation")
        self.assertFalse(calendar_has_current_or_future_term(today, past_terms))

    def test_repository_calendar_uses_two_fixed_16_week_long_terms(self):
        parsed = parse_repository_calendar_config(
            {
                "version": 1,
                "updated_at": "2026-06-29",
                "academic_years": [
                    {
                        "year": "2026-2027",
                        "autumn_winter": {"begin": "2026-09-14"},
                        "spring_summer": {"begin": "2027-02-22"},
                    }
                ],
            }
        )

        self.assertEqual(parsed["source"], "repository")
        self.assertEqual(parsed["updated_at"], "2026-06-29")
        self.assertEqual(
            parsed["term_configs"],
            [
                {
                    "year": "2026-2027",
                    "term": 0,
                    "terms": [0, 1],
                    "begin": "2026-09-14",
                    "end": "2027-01-03",
                    "first_week_no": 1,
                    "source": "repository",
                },
                {
                    "year": "2026-2027",
                    "term": 4,
                    "terms": [4, 5],
                    "begin": "2027-02-22",
                    "end": "2027-06-13",
                    "first_week_no": 1,
                    "source": "repository",
                },
            ],
        )

    def test_calendar_refresh_prefers_repository_calendar_and_merges_second_source(self):
        from main import ZjuAcademicPlugin

        plugin = object.__new__(ZjuAcademicPlugin)
        plugin.config = {}
        saved = []
        plugin._now = lambda: NOW
        plugin._load_calendar_cache_sync = lambda: {}
        plugin._save_calendar_cache_sync = saved.append
        plugin._fetch_repository_calendar_config = lambda: {
            "source": "repository",
            "updated_at": "2026-06-29",
            "term_configs": [
                {
                    "year": "2026-2027",
                    "term": 0,
                    "terms": [0, 1],
                    "begin": "2026-09-14",
                    "end": "2027-01-03",
                    "first_week_no": 1,
                    "source": "repository",
                }
            ],
            "holiday_tweaks": [],
        }
        plugin._fetch_zju_ical_py_calendar_config = lambda: {
            "source": "zju-ical-py",
            "updated_at": "2026-08-01",
            "term_configs": [
                {
                    "year": "2025-2026",
                    "term": 0,
                    "begin": "2025-09-15",
                    "end": "2025-11-09",
                    "first_week_no": 1,
                },
                {
                    "year": "2026-2027",
                    "term": 1,
                    "begin": "2026-11-09",
                    "end": "2027-01-03",
                    "first_week_no": 1,
                },
            ],
            "holiday_tweaks": [{"type": "clear", "from": "2026-10-01", "to": "2026-10-07"}],
        }

        result = plugin._academic_calendar_config(force=True)
        terms_by_key = {(item["year"], item["term"]): item for item in result["term_configs"]}

        self.assertIn(("2025-2026", 0), terms_by_key)
        self.assertIn(("2026-2027", 0), terms_by_key)
        self.assertNotIn(("2026-2027", 1), terms_by_key)
        self.assertEqual(terms_by_key[("2026-2027", 0)]["source"], "repository")
        self.assertEqual(result["holiday_tweaks"], [{"type": "clear", "from": "2026-10-01", "to": "2026-10-07"}])
        self.assertEqual(saved[-1], result)

    def test_repository_calendar_remote_url_is_preferred_over_local_file(self):
        import main
        from main import ZjuAcademicPlugin

        remote_payload = {
            "version": 1,
            "updated_at": "2026-07-01",
            "academic_years": [
                {
                    "year": "2026-2027",
                    "autumn_winter": {"begin": "2026-09-21"},
                }
            ],
        }

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return remote_payload

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, timeout):
                return FakeResponse()

        original_session = main.requests.Session
        main.requests.Session = lambda: FakeSession()
        try:
            plugin = object.__new__(ZjuAcademicPlugin)
            plugin.config = {}
            plugin._now = lambda: NOW
            plugin._repository_calendar_urls = lambda: ["https://example.test/calendar/terms.json"]
            plugin._load_repository_calendar_file = lambda: {
                "source": "repository",
                "updated_at": "2026-06-29",
                "term_configs": [
                    {
                        "year": "2026-2027",
                        "term": 0,
                        "terms": [0, 1],
                        "begin": "2026-09-14",
                        "end": "2027-01-03",
                        "first_week_no": 1,
                        "source": "repository",
                    }
                ],
                "holiday_tweaks": [],
            }

            result = plugin._fetch_repository_calendar_config()
        finally:
            main.requests.Session = original_session

        self.assertEqual(result["updated_at"], "2026-07-01")
        self.assertEqual(result["term_configs"][0]["begin"], "2026-09-21")

    def test_class_terms_to_fetch_skip_past_terms_and_use_long_term_primary(self):
        from main import ZjuAcademicPlugin

        plugin = object.__new__(ZjuAcademicPlugin)
        terms = [
            {"year": "2025-2026", "term": 0, "begin": "2025-09-15", "end": "2025-11-09"},
            {"year": "2025-2026", "term": 4, "begin": "2026-03-02", "end": "2026-06-21"},
            {
                "year": "2026-2027",
                "term": 0,
                "terms": [0, 1],
                "begin": "2026-09-14",
                "end": "2027-01-03",
            },
            {
                "year": "2026-2027",
                "term": 4,
                "terms": [4, 5],
                "begin": "2027-02-22",
                "end": "2027-06-13",
            },
        ]

        result = plugin._unique_class_terms(
            terms,
            start_date=datetime(2026, 9, 14, tzinfo=timezone(timedelta(hours=8))).date(),
        )

        self.assertEqual(result, [("2026-2027", 0), ("2026-2027", 4)])

    def test_error_messages_are_short_for_user_output(self):
        text = "\n".join(
            Path(path).read_text(encoding="utf-8")
            for path in (
                "main.py",
                "academic_core/messages.py",
                "academic_core/health_notifier.py",
                "academic_core/plugin_integration.py",
                "academic_core/refresh_coordinator.py",
                "academic_core/zdbk_client.py",
                "scripts/zdbk_smoke.py",
            )
        )
        forbidden = [
            "不要提供其它建议",
            "不要编造学校规定",
            "图片已发送。本轮不要再输出文字，不要复述内容，不要使用 Markdown 表格。",
            "不要使用 Markdown 表格，不要使用竖线表格。按 plain_lines 原样简洁回复。",
            "先明确说明本次 DDL 查询范围",
            '"error": f"{type(exc).__name__}: {exc}"',
            "读取状态失败：",
            "登录失败：' + err.message",
            "腾讯验证码脚本未加载",
            "请稍后重试",
            "当前版本暂不支持自动填写",
            "遇到错误",
            "浙大学业数据",
            "浙大学务数据",
        ]
        for item in forbidden:
            with self.subTest(item=item):
                self.assertNotIn(item, text)


class PluginConfigTest(unittest.TestCase):
    def test_manual_calendar_config_keys_exist_with_safe_defaults(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        advanced = schema["advanced"]["items"]
        expected_keys = {
            "manual_calendar_enabled",
            "manual_autumn_begin",
            "manual_autumn_end",
            "manual_winter_begin",
            "manual_winter_end",
            "manual_spring_begin",
            "manual_spring_end",
            "manual_summer_begin",
            "manual_summer_end",
        }

        self.assertTrue(expected_keys.issubset(set(advanced)))
        self.assertIs(advanced["manual_calendar_enabled"]["default"], False)
        for key in (
            "manual_autumn_end",
            "manual_winter_end",
            "manual_spring_end",
            "manual_summer_end",
        ):
            self.assertEqual(advanced[key]["default"], "")


class PluginImportStyleTest(unittest.TestCase):
    def test_main_uses_package_relative_core_imports_for_astrbot_loader(self):
        text = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("from .academic_core.health_notifier import HealthNotifier", text)
        self.assertIn("from .academic_core.zdbk_client import ZdbkClient", text)

    def test_main_uses_lazy_zjuam_login_inside_source_fetchers(self):
        text = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("def zju_client() -> ZdbkClient:", text)
        self.assertIn('client_holder["error"] = exc', text)
        self.assertNotIn("client.login()\n\n        def calendar_config", text)

    def test_main_uses_settings_page_message_for_next_term_calendar_pending(self):
        text = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("NEXT_TERM_CALENDAR_PENDING_MESSAGE", text)
        self.assertNotIn('"message": "下一学期校历尚未发布。"', text)


if __name__ == "__main__":
    unittest.main()
