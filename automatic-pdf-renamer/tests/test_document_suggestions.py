"""Tests for asynchronous, confidence-gated document suggestions."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.api import routes
from app.ml import AmbiguousPredictionError


class FakeDatabase:
    def __init__(self, record):
        self.record = record

    def get_by_id(self, file_id):
        return self.record if file_id == self.record.id else None


class FakeRegistry:
    def get_period_format_config(self, name):
        return SimpleNamespace(to_dict=lambda: {})

    def get_key_pattern(self, name):
        return "example/{company}/{period}/{document_type}"


class FakeMLPipeline:
    def __init__(self, confidence_threshold=None):
        self.threshold = confidence_threshold

    def _predict_label(self, model_name, sample):
        values = {
            "field_emitting_company": ("Suggested Company", 0.96),
            "field_document_type": ("invoice", 0.95),
            "template_name": ("invoice-template", 0.94),
            "period_format": ("monthly", 0.93),
        }
        if model_name == "template_name" and "LowConfidence" in sample:
            raise AmbiguousPredictionError("Below threshold")
        return values[model_name]


class DocumentSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.record = SimpleNamespace(
            id="document-1",
            extracted_text="Suggested Company invoice",
            emitting_company="Old Company",
            document_type="receipt",
            template_name="old-template",
            period_format="monthly",
            emission_date="2026-09-20",
            optional_fields={"invoice_number": "INV-123"},
            original_key="inbox/original.pdf",
        )
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    def test_returns_confident_model_suggestions_and_derived_values(self):
        with (
            patch.object(routes, "_db", return_value=FakeDatabase(self.record)),
            patch.object(routes, "_registry", return_value=FakeRegistry()),
            patch.object(routes, "LocalMLPipeline", FakeMLPipeline),
            patch.object(routes, "format_period_from_emission_date", return_value="2026-09"),
        ):
            result = asyncio.run(routes.get_document_model_suggestions(
                self.request,
                "document-1",
                {
                    "emitting_company": "Edited Company",
                    "document_type": "invoice",
                    "template_name": "edited-template",
                    "period_format": "monthly",
                    "emission_date": "2026-09-20",
                },
                current_user=None,
            ))

        suggestions = result["suggestions"]
        self.assertEqual(suggestions["emitting_company"]["value"], "Suggested Company")
        self.assertEqual(suggestions["template_name"]["value"], "invoice-template")
        self.assertEqual(suggestions["period"]["value"], "2026-09")
        self.assertEqual(suggestions["current_key"]["value"], "files/example/2026-09/invoice.pdf")

    def test_unavailable_or_low_confidence_predictions_are_omitted(self):
        class LowConfidencePipeline(FakeMLPipeline):
            def _predict_label(self, model_name, sample):
                if model_name == "template_name":
                    raise AmbiguousPredictionError("Below threshold")
                return super()._predict_label(model_name, sample)

        with (
            patch.object(routes, "_db", return_value=FakeDatabase(self.record)),
            patch.object(routes, "_registry", return_value=FakeRegistry()),
            patch.object(routes, "LocalMLPipeline", LowConfidencePipeline),
            patch.object(routes, "format_period_from_emission_date", return_value="2026-09"),
        ):
            result = asyncio.run(routes.get_document_model_suggestions(
                self.request,
                "document-1",
                {"emission_date": "2026-09-20"},
                current_user=None,
            ))

        self.assertNotIn("template_name", result["suggestions"])
        self.assertIn("period_format", result["suggestions"])


if __name__ == "__main__":
    unittest.main()
