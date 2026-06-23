import unittest
from datetime import datetime, timedelta, timezone

from academic_core.health_notifier import HealthNotifier
from academic_core.models import SourceHealth, SourceStatus


NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def healthy(last_success_at="2026-06-22T11:50:00+08:00"):
    return SourceHealth(status=SourceStatus.HEALTHY, last_success_at=last_success_at)


def failed(code="upstream_http", message="学校接口返回 HTTP 504"):
    return SourceHealth(
        status=SourceStatus.FAILED,
        last_attempt_at=NOW.isoformat(),
        last_success_at="2026-06-20T10:53:00+08:00",
        last_error_code=code,
        last_error_message=message,
        failure_started_at=NOW.isoformat(),
        next_retry_at=(NOW + timedelta(minutes=5)).isoformat(),
        consecutive_failures=1,
    )


def waiting_calendar():
    return SourceHealth(
        status=SourceStatus.WAITING_CALENDAR,
        last_attempt_at=NOW.isoformat(),
        last_success_at="2026-06-20T10:53:00+08:00",
        last_error_code="calendar_pending",
        last_error_message="下一学期校历尚未发布。",
        failure_started_at=NOW.isoformat(),
        next_retry_at=(NOW + timedelta(hours=6)).isoformat(),
        consecutive_failures=1,
    )


class HealthNotifierTest(unittest.TestCase):
    def test_new_failure_and_calendar_pending_notify_immediately(self):
        notifier = HealthNotifier(source="schedule", source_label="课表")

        failure_notes = notifier.pending(healthy(), failed(), ["session-1"], NOW)
        self.assertEqual(len(failure_notes), 1)
        self.assertEqual(failure_notes[0].recipient, "session-1")
        self.assertIn("课表", failure_notes[0].text)
        self.assertIn("学校接口返回 HTTP 504", failure_notes[0].text)
        self.assertIn("2026-06-20T10:53:00+08:00", failure_notes[0].text)
        self.assertIn("最近一次成功数据", failure_notes[0].text)
        self.assertIn("可执行操作", failure_notes[0].text)

        pending_notes = notifier.pending(healthy(), waiting_calendar(), ["session-1"], NOW)
        self.assertEqual(len(pending_notes), 1)
        self.assertIn("校历", pending_notes[0].text)

    def test_same_error_dedupes_for_24_hours_then_repeats_daily(self):
        notifier = HealthNotifier(source="schedule", source_label="课表")
        before = healthy()
        after = failed()

        notes = notifier.pending(before, after, ["session-1"], NOW)
        self.assertEqual(len(notes), 1)
        sent_health = notifier.mark_sent(after, "session-1", NOW)

        self.assertEqual(
            notifier.pending(after, sent_health, ["session-1"], NOW + timedelta(hours=23)),
            [],
        )
        self.assertEqual(
            len(notifier.pending(after, sent_health, ["session-1"], NOW + timedelta(hours=25))),
            1,
        )

    def test_changed_error_code_notifies_immediately(self):
        notifier = HealthNotifier(source="schedule", source_label="课表")
        sent = notifier.mark_sent(failed("upstream_http"), "session-1", NOW)
        changed = failed("captcha_required", "教务系统要求验证码。")

        notes = notifier.pending(sent, changed, ["session-1"], NOW + timedelta(minutes=10))

        self.assertEqual(len(notes), 1)
        self.assertIn("验证码", notes[0].text)

    def test_recovery_notification_after_failure_or_calendar_pending(self):
        notifier = HealthNotifier(source="schedule", source_label="课表")
        for before in (failed(), waiting_calendar()):
            with self.subTest(before=before.status):
                notes = notifier.pending(before, healthy(), ["session-1"], NOW)
                self.assertEqual(len(notes), 1)
                self.assertIn("已恢复", notes[0].text)

    def test_send_failure_does_not_update_delivery_and_partial_success_is_per_recipient(self):
        notifier = HealthNotifier(source="schedule", source_label="课表")
        after = failed()

        first = notifier.pending(healthy(), after, ["session-1", "session-2"], NOW)
        self.assertEqual({note.recipient for note in first}, {"session-1", "session-2"})

        still_pending = notifier.pending(healthy(), after, ["session-1", "session-2"], NOW)
        self.assertEqual({note.recipient for note in still_pending}, {"session-1", "session-2"})

        sent_one = notifier.mark_sent(after, "session-1", NOW)
        remaining = notifier.pending(after, sent_one, ["session-1", "session-2"], NOW)
        self.assertEqual({note.recipient for note in remaining}, {"session-2"})

    def test_notification_text_is_sanitized(self):
        notifier = HealthNotifier(source="schedule", source_label="课表")
        unsafe = failed(
            message=(
                "学校接口失败 Cookie=abc username=student password=secret "
                "Traceback body=xnm=2025-2026&xqm=2|夏"
            )
        )

        notes = notifier.pending(healthy(), unsafe, ["session-1"], NOW)
        text = notes[0].text

        self.assertNotIn("Cookie=abc", text)
        self.assertNotIn("username=student", text)
        self.assertNotIn("password=secret", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("xnm=2025-2026", text)


if __name__ == "__main__":
    unittest.main()
