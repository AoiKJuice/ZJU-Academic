from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any


ACADEMIC_YEAR_RE = re.compile(r"^\d{4}-\d{4}$")


@dataclass(frozen=True)
class CalendarTerm:
    year: str
    term: int
    begin: date
    end: date
    first_week_no: int = 1
    source: str = "third_party"


@dataclass(frozen=True)
class CalendarIssue:
    code: str
    message: str
    source: str
    year: str = ""
    term: int | None = None


@dataclass(frozen=True)
class CalendarResolution:
    terms: list[CalendarTerm]
    issues: list[CalendarIssue] = field(default_factory=list)
    holidays: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CalendarState:
    status: str
    current_term: CalendarTerm | None = None
    next_term: CalendarTerm | None = None


class CalendarResolver:
    def merge(
        self,
        third_party: list[dict[str, Any]],
        manual_enabled: bool,
        manual_terms: list[dict[str, Any]],
    ) -> CalendarResolution:
        issues: list[CalendarIssue] = []
        by_key: dict[tuple[str, int], CalendarTerm] = {}

        if manual_enabled:
            for raw in manual_terms or []:
                term = self._parse_term(raw, source="manual", issues=issues)
                if term is not None:
                    by_key[(term.year, term.term)] = term

        for raw in third_party or []:
            source = str(raw.get("source") or "third_party") if isinstance(raw, dict) else "third_party"
            term = self._parse_term(raw, source=source, issues=issues)
            if term is not None:
                by_key[(term.year, term.term)] = term

        ordered = sorted(by_key.values(), key=lambda item: (item.begin, item.end, item.year, item.term))
        accepted: list[CalendarTerm] = []
        for term in ordered:
            if accepted and term.begin <= accepted[-1].end:
                issues.append(
                    CalendarIssue(
                        code="term_date_overlap",
                        message="学期日期存在重叠，已忽略冲突项。",
                        source=term.source,
                        year=term.year,
                        term=term.term,
                    )
                )
                continue
            if accepted and term.year == accepted[-1].year and term.term <= accepted[-1].term:
                issues.append(
                    CalendarIssue(
                        code="term_order",
                        message="同一学年内学期顺序不正确，已忽略该项。",
                        source=term.source,
                        year=term.year,
                        term=term.term,
                    )
                )
                continue
            accepted.append(term)

        return CalendarResolution(terms=accepted, issues=issues)

    def state_on(self, today: str | date, terms: list[CalendarTerm]) -> CalendarState:
        current_date = self._coerce_date(today)
        ordered = sorted(terms or [], key=lambda item: item.begin)

        current_term = next(
            (term for term in ordered if term.begin <= current_date <= term.end),
            None,
        )
        next_term = next((term for term in ordered if term.begin > current_date), None)

        if current_term is not None:
            return CalendarState(
                status="active",
                current_term=current_term,
                next_term=next_term,
            )
        if ordered:
            return CalendarState(status="vacation", current_term=None, next_term=next_term)
        return CalendarState(status="calendar_pending", current_term=None, next_term=None)

    def terms_for_range(
        self,
        start: str | date,
        end: str | date,
        terms: list[CalendarTerm],
    ) -> list[CalendarTerm]:
        start_date = self._coerce_date(start)
        end_date = self._coerce_date(end)
        if end_date < start_date:
            return []
        return [
            term
            for term in sorted(terms or [], key=lambda item: item.begin)
            if term.begin <= end_date and term.end >= start_date
        ]

    def _parse_term(
        self,
        raw: Any,
        source: str,
        issues: list[CalendarIssue],
    ) -> CalendarTerm | None:
        if not isinstance(raw, dict):
            issues.append(
                CalendarIssue(
                    code="invalid_term_payload",
                    message="学期配置不是对象，已忽略。",
                    source=source,
                )
            )
            return None

        year = str(raw.get("year") or "")
        term_value = raw.get("term")
        begin_text = str(raw.get("begin") or "")
        end_text = str(raw.get("end") or "")

        if source == "manual" and (not begin_text or not end_text):
            issues.append(
                CalendarIssue(
                    code="manual_term_incomplete",
                    message="人工学期必须同时设置开始和结束日期，已忽略该项。",
                    source=source,
                    year=year,
                    term=term_value if isinstance(term_value, int) else None,
                )
            )
            return None

        if not ACADEMIC_YEAR_RE.match(year) or not self._is_consecutive_year(year):
            issues.append(
                CalendarIssue(
                    code="invalid_year",
                    message="学年格式必须为 YYYY-YYYY，且后一年度应为前一年度加一。",
                    source=source,
                    year=year,
                    term=term_value if isinstance(term_value, int) else None,
                )
            )
            return None

        try:
            term = int(term_value)
        except (TypeError, ValueError):
            term = -1
        if term not in (0, 1, 2, 3):
            issues.append(
                CalendarIssue(
                    code="invalid_term",
                    message="学期编号必须为 0、1、2 或 3。",
                    source=source,
                    year=year,
                    term=None if term == -1 else term,
                )
            )
            return None

        try:
            begin = self._coerce_date(begin_text)
            end = self._coerce_date(end_text)
        except ValueError:
            issues.append(
                CalendarIssue(
                    code="invalid_date",
                    message="学期日期格式必须为 YYYY-MM-DD。",
                    source=source,
                    year=year,
                    term=term,
                )
            )
            return None

        if end < begin:
            issues.append(
                CalendarIssue(
                    code="invalid_date_order",
                    message="学期结束日期不能早于开始日期。",
                    source=source,
                    year=year,
                    term=term,
                )
            )
            return None

        try:
            first_week_no = int(raw.get("first_week_no") or 1)
        except (TypeError, ValueError):
            first_week_no = 1

        return CalendarTerm(
            year=year,
            term=term,
            begin=begin,
            end=end,
            first_week_no=first_week_no,
            source=source,
        )

    def _coerce_date(self, value: str | date) -> date:
        if isinstance(value, date):
            return value
        return date.fromisoformat(value)

    def _is_consecutive_year(self, value: str) -> bool:
        start_text, end_text = value.split("-", 1)
        return int(end_text) == int(start_text) + 1
