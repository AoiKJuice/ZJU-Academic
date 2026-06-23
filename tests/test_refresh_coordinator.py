import unittest
from datetime import datetime, timedelta, timezone

from academic_core.models import SourceResult, SourceStatus, migrate_cache
from academic_core.refresh_coordinator import RefreshCoordinator


NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone(timedelta(hours=8)))
NEXT_TERM_CALENDAR_PENDING_MESSAGE = "下一学期校历尚未发布，请前往插件设置页面查看"


class SourceFailure(RuntimeError):
    def __init__(self, code, user_message):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


class RefreshCoordinatorTest(unittest.TestCase):
    def test_failed_source_preserves_last_success_and_other_sources_continue(self):
        cache = migrate_cache(
            {
                "academic_refresh": "2026-06-20T10:53:00+08:00",
                "task_refresh": "2026-06-20T22:25:00+08:00",
                "class_events": [{"id": "old-class"}],
                "exam_events": [{"id": "old-exam"}],
                "task_events": [{"id": "old-task"}],
            }
        )
        calls = []

        def schedule():
            calls.append("schedule")
            raise SourceFailure("upstream_http", "学校接口返回 HTTP 504")

        def exams():
            calls.append("exams")
            return SourceResult(data=[{"id": "new-exam"}])

        def tasks():
            calls.append("tasks")
            return SourceResult(data=[{"id": "new-task"}])

        def pta_tasks():
            calls.append("pta_tasks")
            return SourceResult(data=[{"id": "new-pta"}])

        result = RefreshCoordinator(cache, now_provider=lambda: NOW).refresh(
            {
                "schedule": schedule,
                "exams": exams,
                "tasks": tasks,
                "pta_tasks": pta_tasks,
            }
        )

        self.assertEqual(calls, ["schedule", "exams", "tasks", "pta_tasks"])
        health = result.cache["source_health"]
        self.assertEqual(health["schedule"]["status"], SourceStatus.FAILED.value)
        self.assertEqual(health["schedule"]["last_success_at"], "2026-06-20T10:53:00+08:00")
        self.assertEqual(health["schedule"]["last_error_code"], "upstream_http")
        self.assertEqual(result.cache["class_events"], [{"id": "old-class"}])
        self.assertEqual(result.cache["exam_events"], [{"id": "new-exam"}])
        self.assertEqual(result.cache["task_events"], [{"id": "new-task"}])
        self.assertEqual(result.cache["source_data"]["pta_task_events"], [{"id": "new-pta"}])

    def test_retry_schedule_skips_background_until_due_and_force_bypasses_once(self):
        now = NOW
        cache = migrate_cache({})
        attempts = []

        def failing_schedule():
            attempts.append(now.isoformat())
            raise SourceFailure("upstream_http", "学校接口返回 HTTP 504")

        coordinator = RefreshCoordinator(cache, now_provider=lambda: now)
        first = coordinator.refresh({"schedule": failing_schedule})
        self.assertEqual(len(attempts), 1)
        self.assertEqual(
            first.cache["source_health"]["schedule"]["next_retry_at"],
            (NOW + timedelta(minutes=5)).isoformat(),
        )

        now = NOW + timedelta(minutes=1)
        skipped = RefreshCoordinator(first.cache, now_provider=lambda: now).refresh(
            {"schedule": failing_schedule}
        )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(skipped.skipped_sources, ["schedule"])

        forced = RefreshCoordinator(skipped.cache, now_provider=lambda: now).refresh(
            {"schedule": failing_schedule},
            force=True,
        )
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            forced.cache["source_health"]["schedule"]["next_retry_at"],
            (now + timedelta(minutes=15)).isoformat(),
        )

    def test_later_failures_retry_hourly(self):
        now = NOW
        cache = migrate_cache({})
        attempts = 0

        def failing_schedule():
            nonlocal attempts
            attempts += 1
            raise SourceFailure("upstream_http", "学校接口返回 HTTP 504")

        current_cache = cache
        for expected_interval in (
            timedelta(minutes=5),
            timedelta(minutes=15),
            timedelta(hours=1),
            timedelta(hours=1),
        ):
            result = RefreshCoordinator(current_cache, now_provider=lambda: now).refresh(
                {"schedule": failing_schedule},
                force=True,
            )
            self.assertEqual(
                result.cache["source_health"]["schedule"]["next_retry_at"],
                (now + expected_interval).isoformat(),
            )
            current_cache = result.cache
            now += timedelta(minutes=1)
        self.assertEqual(attempts, 4)

    def test_calendar_pending_keeps_templates_without_replacing_concrete_events(self):
        cache = migrate_cache(
            {
                "academic_refresh": "2026-06-20T10:53:00+08:00",
                "class_events": [{"id": "old-class"}],
            }
        )

        def schedule_waiting_calendar():
            return SourceResult(
                data={
                    "templates": [{"id": "template-1"}],
                    "events": [{"id": "should-not-be-used"}],
                },
                metadata={
                    "status": "calendar_pending",
                    "message": NEXT_TERM_CALENDAR_PENDING_MESSAGE,
                },
            )

        result = RefreshCoordinator(cache, now_provider=lambda: NOW).refresh(
            {"schedule": schedule_waiting_calendar}
        )

        health = result.cache["source_health"]["schedule"]
        self.assertEqual(health["status"], SourceStatus.WAITING_CALENDAR.value)
        self.assertEqual(health["next_retry_at"], (NOW + timedelta(hours=6)).isoformat())
        self.assertEqual(result.cache["source_data"]["schedule_templates"], [{"id": "template-1"}])
        self.assertEqual(result.cache["source_data"]["schedule_events"], [{"id": "old-class"}])
        self.assertEqual(result.cache["class_events"], [{"id": "old-class"}])

    def test_calendar_pending_default_message_points_to_settings_page(self):
        cache = migrate_cache({"class_events": [{"id": "old-class"}]})

        result = RefreshCoordinator(cache, now_provider=lambda: NOW).refresh(
            {
                "schedule": lambda: SourceResult(
                    data={"templates": [], "events": []},
                    metadata={"status": "calendar_pending"},
                )
            }
        )

        self.assertEqual(
            result.cache["source_health"]["schedule"]["last_error_message"],
            NEXT_TERM_CALENDAR_PENDING_MESSAGE,
        )

    def test_refresh_returns_a_copy_so_save_failures_do_not_partially_replace_input(self):
        original = migrate_cache({"class_events": [{"id": "old-class"}]})

        def schedule():
            return SourceResult(data=[{"id": "new-class"}])

        result = RefreshCoordinator(original, now_provider=lambda: NOW).refresh({"schedule": schedule})

        self.assertEqual(original["class_events"], [{"id": "old-class"}])
        self.assertEqual(result.cache["class_events"], [{"id": "new-class"}])


if __name__ == "__main__":
    unittest.main()
