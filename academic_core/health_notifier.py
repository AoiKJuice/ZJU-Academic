from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .messages import ERROR_MESSAGE, NEXT_TERM_CALENDAR_PENDING_MESSAGE
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
            return NEXT_TERM_CALENDAR_PENDING_MESSAGE
        return ERROR_MESSAGE

    def _recovery_text(self, before: SourceHealth, after: SourceHealth) -> str:
        return "已恢复"

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
