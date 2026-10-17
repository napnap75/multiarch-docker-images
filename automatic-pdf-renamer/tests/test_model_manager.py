"""Tests for versioned model selection."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.model_manager import activate_model, list_models
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
        inventory = list_models()
        model = next(item for item in inventory["models"] if item["name"] == "field_document_type")
        self.assertTrue(model["has_active_model"])
        self.assertTrue(model["versions"][0]["active"])

    def test_inference_requires_explicit_active_version(self):
        pipeline = LocalMLPipeline(str(self.root), 0.8)
        with self.assertRaises(ModelUnavailableError):
            pipeline._load("field_document_type")

    def test_activation_rejects_missing_version(self):
        with self.assertRaises(FileNotFoundError):
            activate_model("field_document_type", "missing")


if __name__ == "__main__":
    unittest.main()
