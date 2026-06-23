from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Awaitable, Callable

from .messages import NEXT_TERM_CALENDAR_PENDING_MESSAGE, SOURCE_TEMPORARILY_UNAVAILABLE_MESSAGE
from .models import SourceHealth, SourceStatus, migrate_cache
from .refresh_coordinator import Fetcher, RefreshCoordinator, RefreshResult, SourceTransition


class AcademicPluginRuntime:
    def __init__(self, cache: dict[str, Any], now_provider: Callable[[], datetime]):
        self.cache = migrate_cache(cache)
        self.now_provider = now_provider
        self.last_transitions: list[SourceTransition] = []

    def refresh(self, sources: dict[str, Fetcher], force: bool = False) -> RefreshResult:
        result = RefreshCoordinator(self.cache, self.now_provider).refresh(sources, force=force)
        cache = migrate_cache(result.cache)
        self._merge_task_sources(cache)
        self._update_legacy_refresh_times(cache)
        self.cache = cache
        self.last_transitions = result.transitions
        return RefreshResult(
            cache=self.cache,
            transitions=result.transitions,
            skipped_sources=result.skipped_sources,
        )

    def annotate_query_payload(self, source: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        status = source_status_payload(self.cache, source)
        if status is not None:
            result["source_status"] = status
        return result

    def _merge_task_sources(self, cache: dict[str, Any]) -> None:
        source_data = cache.get("source_data", {})
        tasks = list(source_data.get("task_events", []) or [])
        pta_tasks = list(source_data.get("pta_task_events", []) or [])
        merged = tasks + pta_tasks
        merged.sort(key=lambda item: str(item.get("due_at", "")))
        cache["task_events"] = merged

    def _update_legacy_refresh_times(self, cache: dict[str, Any]) -> None:
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


def source_status_payload(cache: dict[str, Any], source: str) -> dict[str, Any] | None:
    source_health = cache.get("source_health", {})
    health = SourceHealth.from_dict(source_health.get(source))
    if health.status == SourceStatus.HEALTHY:
        return None
    if health.status == SourceStatus.WAITING_CALENDAR:
        message = NEXT_TERM_CALENDAR_PENDING_MESSAGE
    else:
        message = SOURCE_TEMPORARILY_UNAVAILABLE_MESSAGE
    return {
        "status": health.status.value,
        "message": message,
    }


async def run_background_tick(
    refresh: Callable[[], Awaitable[Any]],
    notify: Callable[[], Awaitable[Any]],
    remind: Callable[[], Awaitable[Any]],
) -> dict[str, bool]:
    result = {"refresh_ok": True, "notification_ok": True}
    try:
        await refresh()
    except Exception:
        result["refresh_ok"] = False

    try:
        await notify()
        await remind()
    except Exception:
        result["notification_ok"] = False
    return result
