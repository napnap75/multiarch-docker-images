"""Tests for full-set model retraining and shared additional-field extraction."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.model_manager import BASE_MODELS, list_models, train_all_models
from app.ml import LocalMLPipeline, ModelUnavailableError


def validated_records():
    return [
        SimpleNamespace(
            extracted_text="Invoice number INV-100 for account AC-10",
            optional_fields={"invoice_number": "INV-100", "account_number": "AC-10"},
            emitting_company="Company A",
            document_type="invoice",
            template_name="invoice_template",
            period_format="monthly",
        ),
        SimpleNamespace(
            extracted_text="Invoice number INV-200",
            optional_fields={"invoice_number": "INV-200"},
            emitting_company="Company A",
            document_type="invoice",
            template_name="invoice_template",
            period_format="quarterly",
        ),
        SimpleNamespace(
            extracted_text="Account statement for AC-30",
            optional_fields={"account_number": "AC-30"},
            emitting_company="Company B",
            document_type="statement",
            template_name="statement_template",
            period_format="yearly",
        ),
    ]


class FullModelRetrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.env = patch.dict(os.environ, {"MODEL_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_retraining_replaces_all_models_without_history(self):
        old_artifact = self.root / "sets" / "old-version" / "field_document_type.joblib"
        old_artifact.parent.mkdir(parents=True)
        old_artifact.write_bytes(b"old artifact")
        (self.root / "active_models.json").write_text(
            json.dumps({"field_document_type": "old-version"}), encoding="utf-8"
        )

        progress = []
        result = train_all_models(validated_records(), lambda value, message: progress.append((value, message)))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["count"], len(BASE_MODELS))
        active = json.loads((self.root / "active_models.json").read_text())
        self.assertEqual(set(active), set(BASE_MODELS))
        self.assertEqual(len(set(active.values())), 1)
        for name, version in active.items():
            self.assertTrue((self.root / "sets" / version / f"{name}.joblib").is_file())
        self.assertFalse(old_artifact.exists())
        self.assertEqual(
            {path.name.removesuffix(".joblib") for path in (self.root / "sets" / next(iter(active.values()))).glob("*.joblib")},
            set(BASE_MODELS),
        )
        self.assertEqual(len(list((self.root / "sets").glob("*"))), 1)
        self.assertFalse((self.root / "versions").exists())
        self.assertEqual(progress[-1][0], 100)

    def test_inventory_has_no_version_history_or_emission_date_model(self):
        result = list_models()
        names = {model["name"] for model in result["models"]}
        self.assertNotIn("field_emission_date", names)
        self.assertTrue(all("versions" not in model for model in result["models"]))

    def test_inference_requires_a_complete_active_model_set(self):
        pipeline = LocalMLPipeline(str(self.root), 0.8)
        with self.assertRaises(ModelUnavailableError):
            pipeline._load("field_document_type")

    def test_shared_additional_value_model_extracts_multiple_fields(self):
        train_all_models(validated_records())
        pipeline = LocalMLPipeline(str(self.root), 0.0)
        predictions = pipeline.predict_additional_values(
            ["invoice_number", "account_number"],
            "Invoice number INV-200 for account AC-20",
        )
        self.assertEqual(set(predictions), {"invoice_number", "account_number"})
        self.assertTrue(predictions["invoice_number"][0])
        self.assertTrue(predictions["account_number"][0])


if __name__ == "__main__":
    unittest.main()
