import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from academic_core.models import SourceResult, SourceStatus, migrate_cache
from academic_core.plugin_integration import AcademicPluginRuntime, run_background_tick


NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone(timedelta(hours=8)))
NEXT_TERM_CALENDAR_PENDING_MESSAGE = "下一学期校历尚未发布，请前往插件设置页面查看"
ERROR_MESSAGE = "遇到错误"


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
        self.assertEqual(set(status), {"status", "message"})
        self.assertNotIn("以下内容来自最近一次成功数据", status["message"])

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
            "PTA 登录失败",
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
