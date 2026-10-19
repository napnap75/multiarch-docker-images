"""Tests for asynchronous, confidence-gated document suggestions."""

import asyncio
import json
import re
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.api import routes
from app.api.models import DocumentEditRequest
from app.ml import AmbiguousPredictionError, LocalMLPipeline


class FakeDatabase:
    def __init__(self, record):
        self.record = record

    def get_by_id(self, file_id):
        return self.record if file_id == self.record.id else None


class FakeRegistry:
    def get_additional_fields(self):
        return {
            "invoice_number": ["INV-123"],
            "amount": ["42"],
        }

    def get_date_extraction_compiled(self):
        return [re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})")]

    def get_period_format_config(self, name):
        return SimpleNamespace(to_dict=lambda: {})

    def get_key_pattern(self, name):
        return "example/{year}/{month}/{month_name}/{day}/{company}/{period}/{document_type}"


class FakeMLPipeline(LocalMLPipeline):
    def __init__(self, confidence_threshold=None):
        super().__init__(confidence_threshold=confidence_threshold)

    def _load(self, model_name):
        if model_name == "additional_field_values":
            return SimpleNamespace(trained_fields=["invoice_number", "amount"])
        raise AssertionError(f"Unexpected model load: {model_name}")

    def _predict_label(self, model_name, sample):
        values = {
            "field_emitting_company": ("Suggested Company", 0.96),
            "field_document_type": ("invoice", 0.95),
            "template_name": ("invoice-template", 0.94),
            "period_format": ("monthly", 0.93),
        }
        if model_name == "template_name" and "invoice_number: INV-123" not in sample:
            raise AmbiguousPredictionError("Expected extracted additional field in sample")
        if model_name == "template_name" and "LowConfidence" in sample:
            raise AmbiguousPredictionError("Below threshold")
        return values[model_name]

    def predict_additional_groups(self, emitting_company, document_type):
        return ["invoice_number", "tags"], 0.91

    def predict_additional_values(self, field_names, text, *, allow_partial=False):
        if "tags" in field_names:
            raise ValueError("The shared extraction model has no training spans for: tags")
        return {
            field_name: ("INV-123" if field_name == "invoice_number" else "42", 0.92)
            for field_name in field_names
        }

class DocumentSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.record = SimpleNamespace(
            id="document-1",
            extracted_text="Suggested Company invoice\nNumber: INV-123\nAmount: 42\nDate: 20/09/2026",
            emitting_company="Old Company",
            document_type="receipt",
            template_name="old-template",
            period_format="monthly",
            emission_date="2026-09-20",
            optional_fields={"invoice_number": "INV-123"},
            original_key="inbox/original.pdf",
        )
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    def test_partial_additional_value_extraction_omits_only_low_confidence_fields(self):
        pipeline = LocalMLPipeline(confidence_threshold=0.3)
        pipeline._load = lambda _name: SimpleNamespace(
            predict_values=lambda _text, _fields: {
                "appartement": ("", 0.0),
                "impots": ("Impots sur le revenu", 0.84),
            }
        )

        result = pipeline.predict_additional_values(
            ["appartement", "impots"], "document text", allow_partial=True
        )

        self.assertEqual(result, {"impots": ("Impots sur le revenu", 0.84)})
        with self.assertRaises(AmbiguousPredictionError):
            pipeline.predict_additional_values(["appartement", "impots"], "document text")

    def test_returns_confident_model_suggestions_and_derived_values(self):
        with (
            patch.object(routes, "_db", return_value=FakeDatabase(self.record)),
            patch.object(routes, "_registry", return_value=FakeRegistry()),
            patch.object(routes, "LocalMLPipeline", FakeMLPipeline),
            patch.object(routes, "format_period_from_emission_date", return_value="2026-09"),
            patch.object(routes.logger, "debug") as debug_log,
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
                    "optional_fields": {},
                },
                current_user=None,
            ))

        suggestions = result["suggestions"]
        self.assertEqual(suggestions["emission_date"]["value"], "2026-09-20")
        self.assertEqual(suggestions["emission_date"]["source"], "content")
        self.assertEqual(suggestions["emitting_company"]["value"], "Suggested Company")
        self.assertEqual(
            suggestions["optional_fields"]["value"],
            {"invoice_number": "INV-123"},
        )
        self.assertEqual(suggestions["optional_fields"]["source"], "configured")
        decisions = next(
            call.args[2]
            for call in debug_log.call_args_list
            if call.args[0] == "API POST /files/%s/suggestions decisions=%s"
        )
        self.assertIn("configured_matches={'invoice_number': 'INV-123'}", decisions["additional_fields"])
        self.assertEqual(suggestions["template_name"]["value"], "invoice-template")
        self.assertEqual(suggestions["period_format"]["value"], "monthly")
        self.assertEqual(suggestions["period"]["value"], "2026-09")
        self.assertEqual(
            suggestions["current_key"]["value"],
            "files/example/2026/09/Septembre/20/Edited Company/2026-09/invoice.pdf",
        )

    def test_derived_values_use_form_values_not_model_predictions(self):
        class ConflictingPipeline(FakeMLPipeline):
            def _predict_label(self, model_name, sample):
                predictions = {
                    "field_emitting_company": ("Model Company", 0.96),
                    "field_document_type": ("model-document", 0.95),
                    "template_name": ("model-template", 0.94),
                    "period_format": ("model-period", 0.93),
                }
                return predictions[model_name]

        class TrackingRegistry(FakeRegistry):
            def __init__(self):
                self.period_format_used = None
                self.template_used = None

            def get_period_format_config(self, name):
                self.period_format_used = name
                return SimpleNamespace(to_dict=lambda: {})

            def get_key_pattern(self, name):
                self.template_used = name
                return "{template_name}/{company}/{document_type}/{period_format}/{period}"

        registry = TrackingRegistry()
        with (
            patch.object(routes, "_db", return_value=FakeDatabase(self.record)),
            patch.object(routes, "_registry", return_value=registry),
            patch.object(routes, "LocalMLPipeline", ConflictingPipeline),
            patch.object(routes, "format_period_from_emission_date", return_value="user-date-period"),
        ):
            result = asyncio.run(routes.get_document_model_suggestions(
                self.request,
                "document-1",
                {
                    "emitting_company": "User Company",
                    "document_type": "user-document",
                    "template_name": "user-template",
                    "period_format": "user-period-format",
                    "emission_date": "2026-09-20",
                },
                current_user=None,
            ))

        suggestions = result["suggestions"]
        self.assertEqual(suggestions["emitting_company"]["value"], "Model Company")
        self.assertEqual(suggestions["document_type"]["value"], "model-document")
        self.assertEqual(suggestions["template_name"]["value"], "model-template")
        self.assertEqual(suggestions["period_format"]["value"], "model-period")
        self.assertEqual(suggestions["period"]["value"], "user-date-period")
        self.assertEqual(registry.period_format_used, "user-period-format")
        self.assertEqual(registry.template_used, "user-template")
        self.assertEqual(
            suggestions["current_key"]["value"],
            "files/user-template/User Company/user-document/user-period-format/user-date-period.pdf",
        )

    def test_suggestion_orchestrator_collects_independent_model_results(self):
        pipeline = FakeMLPipeline(confidence_threshold=0.3)

        suggestions, decisions = pipeline.suggest_document_fields(
            self.record.extracted_text,
            "Edited Company",
            "invoice",
            {"invoice_number": "INV-123"},
            FakeRegistry().get_additional_fields(),
        )

        self.assertEqual(suggestions["emitting_company"]["value"], "Suggested Company")
        self.assertEqual(suggestions["document_type"]["value"], "invoice")
        self.assertEqual(suggestions["optional_fields"]["value"], {"invoice_number": "INV-123"})
        self.assertEqual(suggestions["template_name"]["value"], "invoice-template")
        self.assertEqual(suggestions["period_format"]["value"], "monthly")
        self.assertIn("configured_matches={'invoice_number': 'INV-123'}", decisions["additional_fields"])

    def test_configured_value_matching_corrects_partial_case_insensitive_spans(self):
        text = "65 RUE RAYMOND LOSSERAND\nTaxe foncière"

        self.assertEqual(
            LocalMLPipeline.match_configured_additional_value(
                text,
                "65 RUE RAYMOND",
                ["65 rue Raymond Losserand", "20 rue Paul Fort"],
            ),
            "65 rue Raymond Losserand",
        )
        self.assertEqual(
            LocalMLPipeline.match_configured_additional_value(
                text,
                "Taxe",
                ["Taxe foncière", "Taxe d'habitation"],
            ),
            "Taxe foncière",
        )
        self.assertIsNone(
            LocalMLPipeline.match_configured_additional_value(
                "Some unrelated text",
                "unrelated",
                ["Taxe foncière"],
            )
        )
        self.assertIsNone(
            LocalMLPipeline.match_configured_additional_value(
                "Taxe foncière et Taxe d'habitation",
                "Taxe",
                ["Taxe foncière", "Taxe d'habitation"],
            )
        )

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
        self.assertEqual(result["suggestions"]["period_format"]["value"], "monthly")

    def test_low_confidence_additional_field_does_not_block_other_suggestions(self):
        class PartialConfidencePipeline(FakeMLPipeline):
            def predict_additional_groups(self, emitting_company, document_type):
                return ["invoice_number", "amount"], 0.91

            def predict_additional_values(self, field_names, text, *, allow_partial=False):
                if field_names == ["invoice_number"]:
                    raise AmbiguousPredictionError("invoice number below threshold")
                return {"amount": ("42", 0.87)}

        with (
            patch.object(routes, "_db", return_value=FakeDatabase(self.record)),
            patch.object(routes, "_registry", return_value=FakeRegistry()),
            patch.object(routes, "LocalMLPipeline", PartialConfidencePipeline),
            patch.object(routes, "format_period_from_emission_date", return_value="2026-09"),
        ):
            result = asyncio.run(routes.get_document_model_suggestions(
                self.request,
                "document-1",
                {"emission_date": "2026-09-20"},
                current_user=None,
            ))

        suggestions = result["suggestions"]
        self.assertEqual(suggestions["optional_fields"]["value"], {"amount": "42"})
        self.assertEqual(suggestions["optional_fields"]["source"], "configured")
        self.assertEqual(suggestions["template_name"]["value"], "invoice-template")
        self.assertEqual(suggestions["period_format"]["value"], "monthly")

    def test_edit_route_uses_computed_period_placeholders_in_storage_key(self):
        record = SimpleNamespace(
            id="document-1",
            sha256="document-1",
            current_key="pending/original.pdf",
            original_key="pending/original.pdf",
            template_name="old-template",
            period_format="monthly",
            emitting_company="Old Company",
            document_type="receipt",
            emission_date="2026-09-20",
            period="September 2026",
            optional_fields={},
            status="pending",
            confidence=None,
            updated_at="2026-09-20T00:00:00+00:00",
            created_at="2026-09-20T00:00:00+00:00",
            extracted_text="document text",
        )
        metadata = json.dumps({"sha256": record.sha256, "current_key": record.current_key})
        storage = Mock()
        storage.get_object.return_value = metadata
        storage.object_exists.side_effect = lambda key: key == record.current_key
        database = Mock()
        database.get_by_id.return_value = record
        registry = Mock()
        registry.get_template.return_value = object()
        registry.get_period_format_config.return_value = SimpleNamespace(
            to_dict=lambda: {"granularity": "month"}
        )
        registry.get_key_pattern.return_value = (
            "{year}/{month}/{month_name}/{day}/{original_filename}"
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace()),
            state=SimpleNamespace(),
        )
        expected_key = "files/2026/09/Septembre/20/original.pdf"

        with (
            patch.object(routes, "_storage", return_value=storage),
            patch.object(routes, "_db", return_value=database),
            patch.object(routes, "_registry", return_value=registry),
            patch.object(routes, "get_file", new=AsyncMock(return_value=None)),
        ):
            asyncio.run(routes.edit_file(
                request,
                record.id,
                DocumentEditRequest(
                    template_name="invoice-template",
                    period_format="monthly",
                    emitting_company="Example Company",
                    document_type="invoice",
                    emission_date=date(2026, 9, 20),
                ),
                current_user=None,
            ))

        self.assertEqual(record.current_key, expected_key)
        storage.copy_object.assert_called_once_with(record.original_key, expected_key)


if __name__ == "__main__":
    unittest.main()
