"""Tests for local ML inference and confidence-gated routing decisions."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ml import LocalMLPipeline, AmbiguousPredictionError, ModelUnavailableError


class FakeModel:
    def __init__(self, label, confidence=0.95):
        self.label = label
        self.confidence = confidence

    def predict(self, samples):
        return [self.label]

    def predict_proba(self, samples):
        return [[self.confidence, 1.0 - self.confidence]]


class FakeSharedValuesModel:
    def predict_values(self, text, fields):
        return {field: (f"value-{field}", 0.95) for field in fields}


class FakeGroupsModel:
    def __init__(self, groups, confidence=0.95):
        self.groups = groups
        self.confidence = confidence

    def predict(self, samples):
        return [self.groups]

    def predict_proba(self, samples):
        return [[self.confidence, 1.0 - self.confidence]]


class LocalMLPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.model_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def pipeline_with(self, models, threshold=0.8):
        active = {}
        for name in models:
            version = "v1"
            artifact = self.model_dir / "versions" / name / f"{version}.joblib"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.touch()
            active[name] = version
        (self.model_dir / "active_models.json").write_text(json.dumps(active))
        pipeline = LocalMLPipeline(str(self.model_dir), threshold)
        loader = patch("joblib.load", side_effect=lambda path: models[Path(path).parent.name])
        loader.start()
        self.addCleanup(loader.stop)
        return pipeline

    def test_common_fields_are_predicted_by_models(self):
        models = {
            "field_emitting_company": FakeModel("Example Energy"),
            "field_document_type": FakeModel("invoice"),
        }
        pipeline = self.pipeline_with(models)
        fields = pipeline.extract_initial_fields("document text")
        self.assertEqual(fields["emitting_company"][0], "Example Energy")
        self.assertEqual(fields["document_type"][0], "invoice")
        self.assertEqual(set(fields), {"emitting_company", "document_type"})

    def test_predict_labels_keeps_successful_fields_when_another_is_ambiguous(self):
        pipeline = self.pipeline_with({
            "field_emitting_company": FakeModel("Example Energy", 0.4),
            "field_document_type": FakeModel("invoice", 0.95),
        })

        predictions, errors = pipeline.predict_labels({
            "emitting_company": ("field_emitting_company", "document text"),
            "document_type": ("field_document_type", "document text"),
        })

        self.assertEqual(predictions, {"document_type": ("invoice", 0.95)})
        self.assertIsInstance(errors["emitting_company"], AmbiguousPredictionError)

    def test_extract_initial_fields_remains_strict(self):
        pipeline = self.pipeline_with({
            "field_emitting_company": FakeModel("Example Energy", 0.4),
            "field_document_type": FakeModel("invoice", 0.95),
        })

        with self.assertRaises(AmbiguousPredictionError):
            pipeline.extract_initial_fields("document text")

    def test_additional_groups_are_predicted_with_confidence(self):
        pipeline = self.pipeline_with({
            "additional_field_groups": FakeGroupsModel(["appartement", "impots"]),
        })

        groups, confidence = pipeline.predict_additional_groups("Impots", "Avis")

        self.assertEqual(groups, ["appartement", "impots"])
        self.assertEqual(confidence, 0.95)

    def test_low_confidence_prediction_is_rejected(self):
        pipeline = self.pipeline_with({"field_document_type": FakeModel("invoice", 0.4)})
        with self.assertRaises(AmbiguousPredictionError):
            pipeline._predict_label("field_document_type", "document text")

    def test_shared_model_extracts_multiple_fields(self):
        pipeline = self.pipeline_with({"additional_field_values": FakeSharedValuesModel()}, threshold=0.8)
        values = pipeline.predict_additional_values(["invoice_number", "account_number"], "document text")
        self.assertEqual(values["invoice_number"][0], "value-invoice_number")
        self.assertEqual(values["account_number"][0], "value-account_number")

    def test_missing_model_is_not_replaced_by_rules(self):
        pipeline = LocalMLPipeline(str(self.model_dir), 0.8)
        with self.assertRaises(ModelUnavailableError):
            pipeline._predict_label("field_document_type", "invoice text")

    def test_low_confidence_additional_field_is_rejected(self):
        pipeline = self.pipeline_with({"additional_field_values": FakeSharedValuesModel()}, threshold=0.99)
        with self.assertRaises(AmbiguousPredictionError):
            pipeline.predict_additional_values(["invoice_number"], "document text")

    def test_template_and_period_format_are_predicted_together(self):
        pipeline = self.pipeline_with({
            "template_name": FakeModel("invoice-template", 0.94),
            "period_format": FakeModel("monthly", 0.91),
        })

        result = pipeline.predict_template_and_period_format(
            "Example Energy", "invoice", {"invoice_number": "INV-123"}
        )

        self.assertEqual(result, ("invoice-template", 0.94, "monthly", 0.91))

    def test_template_and_period_format_remain_strict(self):
        pipeline = self.pipeline_with({
            "template_name": FakeModel("invoice-template", 0.4),
            "period_format": FakeModel("monthly", 0.91),
        })

        with self.assertRaises(AmbiguousPredictionError):
            pipeline.predict_template_and_period_format("Example Energy", "invoice", {})


if __name__ == "__main__":
    unittest.main()
