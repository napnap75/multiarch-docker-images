"""Tests for configured-regex emission-date extraction."""

import re
import unittest
from types import SimpleNamespace

from app.pipeline import DocumentProcessor
from app.period import get_period_key_values


class EmissionDateExtractionTests(unittest.TestCase):
    def processor(self, patterns):
        processor = object.__new__(DocumentProcessor)
        processor.registry = SimpleNamespace(
            get_date_extraction_compiled=lambda: [re.compile(pattern) for pattern in patterns]
        )
        return processor

    def test_uses_first_date_in_document_order_not_earliest_calendar_date(self):
        processor = self.processor([
            r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})",
            r"([0-9]{4})-([0-9]{2})-([0-9]{2})",
        ])
        date, error = processor._extract_emission_date(
            "Issued 2025-02-03; reference date 01/01/2024"
        )
        self.assertIsNone(error)
        self.assertEqual(date, "2025-02-03")

    def test_normalizes_french_month_names(self):
        processor = self.processor([
            r"([0-9]{1,2}) (janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre) ([0-9]{4})"
        ])
        date, error = processor._extract_emission_date("Émis le 1 janvier 2025")
        self.assertIsNone(error)
        self.assertEqual(date, "2025-01-01")

    def test_does_not_fall_back_when_no_configured_regex_matches(self):
        processor = self.processor([r"([0-9]{4})-([0-9]{2})-([0-9]{2})"])
        date, error = processor._extract_emission_date("Date: 01/02/2025")
        self.assertEqual(date, "")
        self.assertIsNotNone(error)


class PeriodKeyPlaceholderTests(unittest.TestCase):
    def test_key_values_use_the_computed_period_and_representative_day(self):
        values = get_period_key_values(
            "2026-01-20",
            {"granularity": "month", "offset": "previous"},
        )

        self.assertEqual(
            values,
            {"year": "2025", "month": "12", "month_name": "Décembre", "day": "20"},
        )

    def test_key_pattern_renders_period_placeholders(self):
        processor = object.__new__(DocumentProcessor)

        key = processor._format_key_pattern(
            "{year}/{month}/{month_name}/{day}/document.pdf",
            **get_period_key_values("2026-09-20", {"granularity": "month"}),
        )

        self.assertEqual(key, "2026/09/Septembre/20/document.pdf")


if __name__ == "__main__":
    unittest.main()
