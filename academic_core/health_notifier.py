from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import SourceHealth, SourceStatus


@dataclass(frozen=True)
class HealthNotification:
    recipient: str
    text: str
    source: str
    kind: str


class HealthNotifier:
    DAILY_INTERVAL = timedelta(hours=24)

    def __init__(self, source: str, source_label: str | None = None):
        self.source = source
        self.source_label = source_label or source

    def pending(
        self,
        before: SourceHealth,
        after: SourceHealth,
        recipients: list[str],
        now: datetime,
    ) -> list[HealthNotification]:
        unique_recipients = list(dict.fromkeys(recipient for recipient in recipients if recipient))
        if not unique_recipients:
            return []

        if after.status == SourceStatus.HEALTHY:
            if before.status in (SourceStatus.FAILED, SourceStatus.WAITING_CALENDAR):
                return [
                    HealthNotification(
                        recipient=recipient,
                        text=self._recovery_text(before, after),
                        source=self.source,
                        kind="recovery",
                    )
                    for recipient in unique_recipients
                    if not self._has_recent_delivery(after, recipient, "recovery", now)
                ]
            return []

        if after.status not in (SourceStatus.FAILED, SourceStatus.WAITING_CALENDAR):
            return []

        new_incident = (
            before.status != after.status
            or before.last_error_code != after.last_error_code
        )
        notifications: list[HealthNotification] = []
        for recipient in unique_recipients:
            delivery = after.notification_deliveries.get(recipient, {})
            same_incident = (
                delivery.get("kind") == "problem"
                and delivery.get("status") == after.status.value
                and delivery.get("error_code") == after.last_error_code
            )
            if same_incident and not self._delivery_is_daily_due(delivery, now):
                continue
            if not new_incident and same_incident and not self._delivery_is_daily_due(delivery, now):
                continue
            notifications.append(
                HealthNotification(
                    recipient=recipient,
                    text=self._problem_text(after),
                    source=self.source,
                    kind="problem",
                )
            )
        return notifications

    def mark_sent(self, health: SourceHealth, recipient: str, sent_at: datetime) -> SourceHealth:
        updated = copy.deepcopy(health)
        updated.last_notification_at = sent_at.isoformat()
        kind = "recovery" if updated.status == SourceStatus.HEALTHY else "problem"
        updated.notification_deliveries[recipient] = {
            "kind": kind,
            "status": updated.status.value,
            "error_code": updated.last_error_code,
            "sent_at": sent_at.isoformat(),
        }
        return updated

    def _has_recent_delivery(
        self,
        health: SourceHealth,
        recipient: str,
        kind: str,
        now: datetime,
    ) -> bool:
        delivery = health.notification_deliveries.get(recipient, {})
        if delivery.get("kind") != kind:
            return False
        return not self._delivery_is_daily_due(delivery, now)

    def _delivery_is_daily_due(self, delivery: dict[str, str], now: datetime) -> bool:
        sent_at = delivery.get("sent_at", "")
        if not sent_at:
            return True
        try:
            last_sent = datetime.fromisoformat(sent_at)
        except ValueError:
            return True
        return now - last_sent >= self.DAILY_INTERVAL

    def _problem_text(self, health: SourceHealth) -> str:
        if health.status == SourceStatus.WAITING_CALENDAR:
            headline = f"{self.source_label}等待校历：{self._sanitize(health.last_error_message)}"
        else:
            headline = f"{self.source_label}数据异常：{self._sanitize(health.last_error_message)}"
        last_success = health.last_success_at or "暂无"
        next_retry = health.next_retry_at or "稍后"
        return "\n".join(
            [
                headline,
                f"最后成功：{last_success}",
                "当前会使用最近一次成功数据；没有可靠日期的数据不会生成提醒。",
                f"下次自动尝试：{next_retry}",
                "可执行操作：稍后手动刷新；如提示验证码，需要人工登录处理。",
            ]
        )

    def _recovery_text(self, before: SourceHealth, after: SourceHealth) -> str:
        last_success = after.last_success_at or after.last_attempt_at or "刚刚"
        previous = self._sanitize(before.last_error_message)
        return "\n".join(
            [
                f"{self.source_label}已恢复。",
                f"恢复时间：{last_success}",
                f"此前异常：{previous or before.last_error_code or '未记录'}",
                "后续查询将直接使用最新数据。",
            ]
        )

    def _sanitize(self, text: str) -> str:
        sanitized = str(text or "")
        sanitized = re.sub(
            r"(?i)\b(cookie|password|passwd|pwd|username|body)[=:：]\s*\S+",
            "[敏感信息已隐藏]",
            sanitized,
        )
        sanitized = re.sub(
            r"(?i)\b(xnm|xqm|captcha_value|ticket|execution)=[^&\s]+",
            r"\1=[已隐藏]",
            sanitized,
        )
        sanitized = re.sub(r"Traceback.*", "[堆栈已隐藏]", sanitized, flags=re.S)
        return sanitized.strip()
