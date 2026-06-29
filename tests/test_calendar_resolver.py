import unittest

from academic_core.calendar_resolver import CalendarResolver


THIRD_PARTY = [
    {
        "year": "2025-2026",
        "term": 0,
        "begin": "2025-09-15",
        "end": "2025-11-09",
        "first_week_no": 1,
    },
    {
        "year": "2025-2026",
        "term": 1,
        "begin": "2025-11-10",
        "end": "2026-01-25",
        "first_week_no": 1,
    },
    {
        "year": "2025-2026",
        "term": 2,
        "begin": "2026-03-02",
        "end": "2026-04-26",
        "first_week_no": 1,
    },
    {
        "year": "2025-2026",
        "term": 3,
        "begin": "2026-04-27",
        "end": "2026-06-28",
        "first_week_no": 1,
    },
]

MANUAL_NEXT = [
    {
        "year": "2026-2027",
        "term": 0,
        "begin": "2026-09-14",
        "end": "2026-11-08",
        "first_week_no": 1,
    },
]


class CalendarResolverTest(unittest.TestCase):
    def setUp(self):
        self.resolver = CalendarResolver()

    def test_state_active_vacation_and_calendar_pending(self):
        known_with_next = self.resolver.merge(
            THIRD_PARTY,
            manual_enabled=True,
            manual_terms=MANUAL_NEXT,
        ).terms
        self.assertEqual(
            self.resolver.state_on("2026-06-22", known_with_next).status,
            "active",
        )
        self.assertEqual(
            self.resolver.state_on("2026-07-20", known_with_next).status,
            "vacation",
        )

        known_without_next = self.resolver.merge(
            THIRD_PARTY,
            manual_enabled=False,
            manual_terms=MANUAL_NEXT,
        ).terms
        self.assertEqual(
            self.resolver.state_on("2026-07-20", known_without_next).status,
            "vacation",
        )

    def test_manual_terms_only_fill_missing_terms(self):
        manual = [
            {
                "year": "2025-2026",
                "term": 1,
                "begin": "2099-01-01",
                "end": "2099-02-01",
                "first_week_no": 1,
            },
            MANUAL_NEXT[0],
        ]
        result = self.resolver.merge(THIRD_PARTY, manual_enabled=True, manual_terms=manual)
        by_key = {(term.year, term.term): term for term in result.terms}

        self.assertEqual(by_key[("2025-2026", 1)].begin.isoformat(), "2025-11-10")
        self.assertEqual(by_key[("2025-2026", 1)].source, "third_party")
        self.assertEqual(by_key[("2026-2027", 0)].begin.isoformat(), "2026-09-14")
        self.assertEqual(by_key[("2026-2027", 0)].source, "manual")

    def test_manual_term_requires_begin_and_end_when_enabled(self):
        result = self.resolver.merge(
            THIRD_PARTY,
            manual_enabled=True,
            manual_terms=[
                {
                    "year": "2026-2027",
                    "term": 1,
                    "begin": "2026-11-09",
                    "end": "",
                    "first_week_no": 1,
                }
            ],
        )

        self.assertNotIn(("2026-2027", 1), {(term.year, term.term) for term in result.terms})
        self.assertIn("manual_term_incomplete", {issue.code for issue in result.issues})

    def test_invalid_term_configurations_are_reported_and_ignored(self):
        result = self.resolver.merge(
            [
                {
                    "year": "20252026",
                    "term": 0,
                    "begin": "2025-09-15",
                    "end": "2025-11-09",
                    "first_week_no": 1,
                },
                {
                    "year": "2025-2026",
                    "term": 4,
                    "begin": "2025-11-10",
                    "end": "2026-01-25",
                    "first_week_no": 1,
                },
                {
                    "year": "2025-2026",
                    "term": 2,
                    "begin": "2026-04-26",
                    "end": "2026-03-02",
                    "first_week_no": 1,
                },
            ],
            manual_enabled=False,
            manual_terms=[],
        )

        self.assertEqual(result.terms, [])
        self.assertIn("invalid_year", {issue.code for issue in result.issues})
        self.assertIn("invalid_term", {issue.code for issue in result.issues})
        self.assertIn("invalid_date_order", {issue.code for issue in result.issues})

    def test_term_order_and_overlap_errors_reject_offending_terms(self):
        result = self.resolver.merge(
            [
                {
                    "year": "2025-2026",
                    "term": 1,
                    "begin": "2025-09-15",
                    "end": "2025-10-10",
                    "first_week_no": 1,
                },
                {
                    "year": "2025-2026",
                    "term": 0,
                    "begin": "2025-10-11",
                    "end": "2025-11-09",
                    "first_week_no": 1,
                },
                {
                    "year": "2025-2026",
                    "term": 2,
                    "begin": "2025-10-01",
                    "end": "2026-01-01",
                    "first_week_no": 1,
                },
            ],
            manual_enabled=False,
            manual_terms=[],
        )

        self.assertEqual([(term.year, term.term) for term in result.terms], [("2025-2026", 1)])
        self.assertIn("term_order", {issue.code for issue in result.issues})
        self.assertIn("term_date_overlap", {issue.code for issue in result.issues})

    def test_cross_academic_year_order_and_range_overlap(self):
        terms = self.resolver.merge(
            THIRD_PARTY,
            manual_enabled=True,
            manual_terms=MANUAL_NEXT,
        ).terms
        self.assertEqual(
            [(term.year, term.term) for term in terms],
            [
                ("2025-2026", 0),
                ("2025-2026", 1),
                ("2025-2026", 2),
                ("2025-2026", 3),
                ("2026-2027", 0),
            ],
        )

        overlapping = self.resolver.terms_for_range("2026-01-20", "2026-03-05", terms)
        self.assertEqual(
            [(term.year, term.term) for term in overlapping],
            [("2025-2026", 1), ("2025-2026", 2)],
        )


if __name__ == "__main__":
    unittest.main()
