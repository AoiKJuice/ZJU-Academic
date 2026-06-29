from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from .messages import NEXT_TERM_CALENDAR_PENDING_MESSAGE, SOURCE_TEMPORARILY_UNAVAILABLE_MESSAGE
from .models import SourceHealth, SourceStatus, migrate_cache
from .refresh_coordinator import Fetcher, RefreshCoordinator, RefreshResult, SourceTransition


ACADEMIC_YEAR_RE = re.compile(r"^\d{4}-\d{4}$")
REPOSITORY_LONG_TERM_WEEKS = 16
REPOSITORY_LONG_TERMS = (
    ("autumn_winter", 0, [0, 1]),
    ("spring_summer", 4, [4, 5]),
)


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


def calendar_refresh_state(today: date | str, term_configs: list[dict[str, Any]]) -> str:
    current_date = _coerce_date(today)
    parsed_terms = _parse_term_ranges(term_configs)
    if not parsed_terms:
        return "calendar_pending"
    if any(begin <= current_date <= end for begin, end in parsed_terms):
        return "active"
    return "vacation"


def calendar_has_current_or_future_term(today: date | str, term_configs: list[dict[str, Any]]) -> bool:
    current_date = _coerce_date(today)
    return any(end >= current_date for _, end in _parse_term_ranges(term_configs))


def parse_repository_calendar_config(payload: dict[str, Any]) -> dict[str, Any]:
    result = {
        "source": "repository",
        "updated_at": str(payload.get("updated_at") or "").strip() if isinstance(payload, dict) else "",
        "term_configs": [],
        "holiday_tweaks": _parse_repository_holiday_tweaks(payload.get("holiday_tweaks", []))
        if isinstance(payload, dict)
        else [],
    }
    if not isinstance(payload, dict):
        return result

    term_configs: list[dict[str, Any]] = []
    for academic_year in payload.get("academic_years", []):
        if not isinstance(academic_year, dict):
            continue
        year = str(academic_year.get("year") or "").strip()
        if not _valid_academic_year(year):
            continue
        for key, primary_term, term_ids in REPOSITORY_LONG_TERMS:
            raw_term = academic_year.get(key)
            if not isinstance(raw_term, dict):
                continue
            try:
                begin = _coerce_date(raw_term.get("begin", ""))
            except Exception:
                continue
            end = begin + timedelta(weeks=REPOSITORY_LONG_TERM_WEEKS) - timedelta(days=1)
            try:
                first_week_no = int(raw_term.get("first_week_no") or 1)
            except Exception:
                first_week_no = 1
            if first_week_no < 1:
                first_week_no = 1
            term_configs.append(
                {
                    "year": year,
                    "term": primary_term,
                    "terms": list(term_ids),
                    "begin": begin.isoformat(),
                    "end": end.isoformat(),
                    "first_week_no": first_week_no,
                    "source": "repository",
                }
            )

    result["term_configs"] = sorted(term_configs, key=lambda item: (item["begin"], item["term"]))
    return result


def _parse_repository_holiday_tweaks(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        tweak_type = str(item.get("type") or "").strip()
        from_text = str(item.get("from") or "").strip()
        to_text = str(item.get("to") or "").strip()
        if not tweak_type or not from_text or not to_text:
            continue
        try:
            _coerce_date(from_text)
            _coerce_date(to_text)
        except Exception:
            continue
        result.append({"type": tweak_type, "from": from_text, "to": to_text})
    return result


def _parse_term_ranges(term_configs: list[dict[str, Any]]) -> list[tuple[date, date]]:
    parsed_terms: list[tuple[date, date]] = []
    for item in term_configs:
        try:
            begin = _coerce_date(item["begin"])
            end = _coerce_date(item["end"])
        except Exception:
            continue
        parsed_terms.append((begin, end))
    return sorted(parsed_terms, key=lambda item: item[0])


def _coerce_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _valid_academic_year(value: str) -> bool:
    if not ACADEMIC_YEAR_RE.match(value):
        return False
    start_text, end_text = value.split("-", 1)
    return int(end_text) == int(start_text) + 1


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
