from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


SOURCE_NAMES = ("calendar", "schedule", "exams", "tasks", "pta_tasks")


class SourceStatus(str, Enum):
    HEALTHY = "healthy"
    FAILED = "failed"
    WAITING_CALENDAR = "waiting_calendar"


@dataclass
class SourceHealth:
    status: SourceStatus = SourceStatus.HEALTHY
    last_attempt_at: str = ""
    last_success_at: str = ""
    last_error_code: str = ""
    last_error_message: str = ""
    failure_started_at: str = ""
    next_retry_at: str = ""
    last_notification_at: str = ""
    consecutive_failures: int = 0
    notification_deliveries: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message,
            "failure_started_at": self.failure_started_at,
            "next_retry_at": self.next_retry_at,
            "last_notification_at": self.last_notification_at,
            "consecutive_failures": self.consecutive_failures,
            "notification_deliveries": copy.deepcopy(self.notification_deliveries),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SourceHealth":
        if not isinstance(value, dict):
            return cls()

        status_value = value.get("status", SourceStatus.HEALTHY.value)
        status = SourceStatus(status_value)

        deliveries = value.get("notification_deliveries", {})
        if not isinstance(deliveries, dict):
            deliveries = {}

        return cls(
            status=status,
            last_attempt_at=str(value.get("last_attempt_at") or ""),
            last_success_at=str(value.get("last_success_at") or ""),
            last_error_code=str(value.get("last_error_code") or ""),
            last_error_message=str(value.get("last_error_message") or ""),
            failure_started_at=str(value.get("failure_started_at") or ""),
            next_retry_at=str(value.get("next_retry_at") or ""),
            last_notification_at=str(value.get("last_notification_at") or ""),
            consecutive_failures=int(value.get("consecutive_failures") or 0),
            notification_deliveries=copy.deepcopy(deliveries),
        )


@dataclass
class SourceResult:
    data: Any
    metadata: dict[str, Any] = field(default_factory=dict)


def migrate_cache(cache: Any) -> dict[str, Any]:
    migrated = copy.deepcopy(cache) if isinstance(cache, dict) else {}
    migrated["schema_version"] = 2

    existing_health = migrated.get("source_health")
    if not isinstance(existing_health, dict):
        existing_health = {}

    defaults = {
        "calendar": "",
        "schedule": str(migrated.get("academic_refresh") or ""),
        "exams": str(migrated.get("academic_refresh") or ""),
        "tasks": str(migrated.get("task_refresh") or ""),
        "pta_tasks": str(migrated.get("task_refresh") or ""),
    }

    source_health: dict[str, dict[str, Any]] = {}
    for source_name in SOURCE_NAMES:
        health = SourceHealth.from_dict(existing_health.get(source_name))
        if not health.last_success_at:
            health.last_success_at = defaults[source_name]
        source_health[source_name] = health.to_dict()
    migrated["source_health"] = source_health

    existing_data = migrated.get("source_data")
    if not isinstance(existing_data, dict):
        existing_data = {}

    source_data = {
        "calendar": copy.deepcopy(existing_data.get("calendar", {})),
        "schedule_templates": copy.deepcopy(existing_data.get("schedule_templates", [])),
        "schedule_events": copy.deepcopy(
            existing_data.get("schedule_events", migrated.get("class_events", []))
        ),
        "exam_events": copy.deepcopy(
            existing_data.get("exam_events", migrated.get("exam_events", []))
        ),
        "task_events": copy.deepcopy(
            existing_data.get("task_events", migrated.get("task_events", []))
        ),
        "pta_task_events": copy.deepcopy(existing_data.get("pta_task_events", [])),
    }
    migrated["source_data"] = source_data

    migrated.setdefault("class_events", copy.deepcopy(source_data["schedule_events"]))
    migrated.setdefault("exam_events", copy.deepcopy(source_data["exam_events"]))
    migrated.setdefault("task_events", copy.deepcopy(source_data["task_events"]))

    return migrated
