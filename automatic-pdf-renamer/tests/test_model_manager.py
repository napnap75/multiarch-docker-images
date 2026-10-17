"""Tests for versioned models and shared additional-field extraction."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.model_manager import activate_model, list_models, train_model
from app.ml import LocalMLPipeline, ModelUnavailableError


class ModelActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.env = patch.dict(os.environ, {"MODEL_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_activation_updates_active_version_atomically(self):
        artifact = self.root / "versions" / "field_document_type" / "v1.joblib"
        artifact.parent.mkdir(parents=True)
        artifact.touch()
        result = activate_model("field_document_type", "v1")
        self.assertEqual(result["active_version"], "v1")
        active = json.loads((self.root / "active_models.json").read_text())
        self.assertEqual(active["field_document_type"], "v1")
        model = next(item for item in list_models()["models"] if item["name"] == "field_document_type")
        self.assertTrue(model["has_active_model"])
        self.assertTrue(model["versions"][0]["active"])

    def test_inference_requires_explicit_active_version(self):
        pipeline = LocalMLPipeline(str(self.root), 0.8)
        with self.assertRaises(ModelUnavailableError):
            pipeline._load("field_document_type")

    def test_activation_rejects_missing_version(self):
        with self.assertRaises(FileNotFoundError):
            activate_model("field_document_type", "missing")

    def test_shared_additional_value_model_trains_and_predicts_multiple_fields(self):
        records = [
            SimpleNamespace(extracted_text="Invoice number INV-100 for account AC-10", optional_fields={"invoice_number": "INV-100", "account_number": "AC-10"}, emitting_company="A", document_type="invoice"),
            SimpleNamespace(extracted_text="Invoice number INV-200 for account AC-20", optional_fields={"invoice_number": "INV-200", "account_number": "AC-20"}, emitting_company="A", document_type="invoice"),
            SimpleNamespace(extracted_text="Invoice number INV-300 for account AC-30", optional_fields={"invoice_number": "INV-300", "account_number": "AC-30"}, emitting_company="A", document_type="invoice"),
        ]
        result = train_model("additional_field_values", records)
        self.assertEqual(result["name"], "additional_field_values")
        self.assertEqual(set(result["trained_fields"]), {"invoice_number", "account_number"})
        self.assertEqual(len(list((self.root / "versions" / "additional_field_values").glob("*.joblib"))), 1)
        activate_model("additional_field_values", result["version"])
        pipeline = LocalMLPipeline(str(self.root), 0.0)
        predictions = pipeline.predict_additional_values(
            ["invoice_number", "account_number"], "Invoice number INV-200 for account AC-20"
        )
        self.assertEqual(set(predictions), {"invoice_number", "account_number"})
        self.assertTrue(predictions["invoice_number"][0])
        self.assertTrue(predictions["account_number"][0])

    def test_per_field_model_names_are_rejected(self):
        with self.assertRaises(ValueError):
            train_model("additional_value_invoice_number", [])


if __name__ == "__main__":
    unittest.main()
