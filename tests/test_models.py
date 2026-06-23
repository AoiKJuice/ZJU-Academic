import unittest

from academic_core.models import SourceHealth, SourceStatus, migrate_cache


NEXT_TERM_CALENDAR_PENDING_MESSAGE = "下一学期校历尚未发布，请前往插件设置页面查看"


class ModelsTest(unittest.TestCase):
    def test_source_health_round_trip(self):
        health = SourceHealth(
            status=SourceStatus.FAILED,
            last_attempt_at="2026-06-22T12:00:00+08:00",
            last_success_at="2026-06-20T10:53:00+08:00",
            last_error_code="upstream_http",
            last_error_message="学校接口返回 HTTP 504",
            failure_started_at="2026-06-22T12:00:00+08:00",
            next_retry_at="2026-06-22T12:05:00+08:00",
        )
        self.assertEqual(SourceHealth.from_dict(health.to_dict()), health)

    def test_migrate_legacy_cache_preserves_existing_payload(self):
        old = {
            "academic_refresh": "2026-06-20T10:53:00+08:00",
            "task_refresh": "2026-06-20T22:25:00+08:00",
            "class_events": [{"id": "class-1"}],
            "exam_events": [{"id": "exam-1"}],
            "task_events": [{"id": "task-1"}],
        }
        migrated = migrate_cache(old)
        self.assertEqual(migrated["class_events"], old["class_events"])
        self.assertEqual(migrated["exam_events"], old["exam_events"])
        self.assertEqual(migrated["task_events"], old["task_events"])
        self.assertEqual(
            migrated["source_health"]["schedule"]["last_success_at"],
            old["academic_refresh"],
        )
        self.assertEqual(
            migrated["source_health"]["tasks"]["last_success_at"],
            old["task_refresh"],
        )

    def test_migrate_waiting_calendar_message_points_to_settings_page(self):
        old = {
            "source_health": {
                "schedule": {
                    "status": "waiting_calendar",
                    "last_error_code": "calendar_pending",
                    "last_error_message": "下一学期校历尚未发布。",
                }
            }
        }

        migrated = migrate_cache(old)

        self.assertEqual(
            migrated["source_health"]["schedule"]["last_error_message"],
            NEXT_TERM_CALENDAR_PENDING_MESSAGE,
        )


if __name__ == "__main__":
    unittest.main()
