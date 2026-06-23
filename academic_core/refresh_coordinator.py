from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .models import SOURCE_NAMES, SourceHealth, SourceResult, SourceStatus, migrate_cache


Fetcher = Callable[[], SourceResult]


@dataclass(frozen=True)
class SourceTransition:
    source: str
    before: SourceHealth
    after: SourceHealth


@dataclass(frozen=True)
class RefreshResult:
    cache: dict[str, Any]
    transitions: list[SourceTransition] = field(default_factory=list)
    skipped_sources: list[str] = field(default_factory=list)


class RefreshCoordinator:
    RETRY_INTERVALS = (
        timedelta(minutes=5),
        timedelta(minutes=15),
        timedelta(hours=1),
    )
    WAITING_CALENDAR_INTERVAL = timedelta(hours=6)

    def __init__(self, cache: dict[str, Any], now_provider: Callable[[], datetime]):
        self._cache = migrate_cache(cache)
        self._now_provider = now_provider

    def refresh(self, sources: dict[str, Fetcher], force: bool = False) -> RefreshResult:
        cache = migrate_cache(self._cache)
        transitions: list[SourceTransition] = []
        skipped_sources: list[str] = []

        for source in SOURCE_NAMES:
            fetcher = sources.get(source)
            if fetcher is None:
                continue

            now = self._now_provider()
            before = SourceHealth.from_dict(cache["source_health"].get(source))
            if not force and self._should_skip(before, now):
                skipped_sources.append(source)
                continue

            before_snapshot = copy.deepcopy(before)
            before.last_attempt_at = now.isoformat()
            try:
                result = fetcher()
                if not isinstance(result, SourceResult):
                    result = SourceResult(data=result)
                if result.metadata.get("status") == "calendar_pending":
                    self._apply_waiting_calendar(cache, source, result, before, now)
                else:
                    self._apply_success(cache, source, result, before, now)
            except Exception as exc:
                self._apply_failure(cache, source, before, now, exc)

            after = SourceHealth.from_dict(cache["source_health"].get(source))
            if after != before_snapshot:
                transitions.append(SourceTransition(source=source, before=before_snapshot, after=after))

        return RefreshResult(cache=cache, transitions=transitions, skipped_sources=skipped_sources)

    def _should_skip(self, health: SourceHealth, now: datetime) -> bool:
        if health.status not in (SourceStatus.FAILED, SourceStatus.WAITING_CALENDAR):
            return False
        if not health.next_retry_at:
            return False
        try:
            next_retry = datetime.fromisoformat(health.next_retry_at)
        except ValueError:
            return False
        return now < next_retry

    def _apply_success(
        self,
        cache: dict[str, Any],
        source: str,
        result: SourceResult,
        health: SourceHealth,
        now: datetime,
    ) -> None:
        self._update_source_data(cache, source, result.data)
        health.status = SourceStatus.HEALTHY
        health.last_attempt_at = now.isoformat()
        health.last_success_at = now.isoformat()
        health.last_error_code = ""
        health.last_error_message = ""
        health.failure_started_at = ""
        health.next_retry_at = ""
        health.consecutive_failures = 0
        cache["source_health"][source] = health.to_dict()

    def _apply_failure(
        self,
        cache: dict[str, Any],
        source: str,
        health: SourceHealth,
        now: datetime,
        exc: Exception,
    ) -> None:
        previous_failures = health.consecutive_failures if health.status == SourceStatus.FAILED else 0
        health.status = SourceStatus.FAILED
        health.last_attempt_at = now.isoformat()
        health.last_error_code = str(getattr(exc, "code", "") or "unexpected")
        health.last_error_message = str(getattr(exc, "user_message", "") or str(exc))
        if not health.failure_started_at:
            health.failure_started_at = now.isoformat()
        health.consecutive_failures = previous_failures + 1
        health.next_retry_at = (now + self._retry_interval(health.consecutive_failures)).isoformat()
        cache["source_health"][source] = health.to_dict()

    def _apply_waiting_calendar(
        self,
        cache: dict[str, Any],
        source: str,
        result: SourceResult,
        health: SourceHealth,
        now: datetime,
    ) -> None:
        if source == "schedule" and isinstance(result.data, dict):
            templates = result.data.get("templates")
            if isinstance(templates, list):
                cache["source_data"]["schedule_templates"] = copy.deepcopy(templates)

        previous_failures = (
            health.consecutive_failures
            if health.status == SourceStatus.WAITING_CALENDAR
            else 0
        )
        health.status = SourceStatus.WAITING_CALENDAR
        health.last_attempt_at = now.isoformat()
        health.last_error_code = "calendar_pending"
        health.last_error_message = str(
            result.metadata.get("message") or "学期安排尚未发布，暂不生成具体课程日期。"
        )
        if not health.failure_started_at:
            health.failure_started_at = now.isoformat()
        health.consecutive_failures = previous_failures + 1
        health.next_retry_at = (now + self.WAITING_CALENDAR_INTERVAL).isoformat()
        cache["source_health"][source] = health.to_dict()

    def _retry_interval(self, consecutive_failures: int) -> timedelta:
        index = max(0, consecutive_failures - 1)
        if index >= len(self.RETRY_INTERVALS):
            return self.RETRY_INTERVALS[-1]
        return self.RETRY_INTERVALS[index]

    def _update_source_data(self, cache: dict[str, Any], source: str, data: Any) -> None:
        source_data = cache["source_data"]
        if source == "calendar":
            source_data["calendar"] = copy.deepcopy(data)
            return
        if source == "schedule":
            if isinstance(data, dict):
                templates = data.get("templates")
                events = data.get("events", [])
            else:
                templates = None
                events = data
            if isinstance(templates, list):
                source_data["schedule_templates"] = copy.deepcopy(templates)
            source_data["schedule_events"] = copy.deepcopy(events or [])
            cache["class_events"] = copy.deepcopy(source_data["schedule_events"])
            return
        if source == "exams":
            source_data["exam_events"] = copy.deepcopy(data or [])
            cache["exam_events"] = copy.deepcopy(source_data["exam_events"])
            return
        if source == "tasks":
            source_data["task_events"] = copy.deepcopy(data or [])
            cache["task_events"] = copy.deepcopy(source_data["task_events"])
            return
        if source == "pta_tasks":
            source_data["pta_task_events"] = copy.deepcopy(data or [])
            return
