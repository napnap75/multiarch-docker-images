"""Offline ML inference for document field extraction and routing decisions.

Model artifacts are trusted, locally trained joblib objects. No rule-based or
configuration-based classifier is used when an artifact is unavailable.
Each single-label artifact exposes predict([sample]) and, optionally,
predict_proba([sample]). The group selector returns an iterable of group labels
for a (company, document_type) sample.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ModelUnavailableError(RuntimeError):
    """Raised when a required local ML model is missing or invalid."""


class AmbiguousPredictionError(RuntimeError):
    """Raised when an ML prediction does not meet the configured confidence."""


class LocalMLPipeline:
    """Load local model artifacts and provide confidence-gated predictions."""

    def __init__(self, model_dir: str | None = None, confidence_threshold: float | None = None):
        self.model_dir = Path(model_dir or os.environ.get("MODEL_DIR", "./models"))
        self.confidence_threshold = (
            float(confidence_threshold)
            if confidence_threshold is not None
            else float(os.environ.get("CONFIDENCE_THRESHOLD", "0.8"))
        )
        self._models: dict[str, Any] = {}

    def _load(self, name: str) -> Any:
        if name in self._models:
            return self._models[name]
        active_path = self.model_dir / "active_models.json"
        try:
            active = __import__("json").loads(active_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            active = {}
        version = active.get(name)
        path = (self.model_dir / "versions" / name / f"{version}.joblib") if version else (self.model_dir / f"{name}.joblib")
        if not path.is_file():
            raise ModelUnavailableError(
                f"No active model artifact for {name}; train a version and activate it in Maintenance."
            )
        try:
            import joblib
            model = joblib.load(path)
        except Exception as exc:
            raise ModelUnavailableError(f"Could not load ML model {path.name}: {exc}") from exc
        self._models[name] = model
        return model

    def _predict_label(self, name: str, sample: Any) -> tuple[str, float]:
        model = self._load(name)
        try:
            predictions = model.predict([sample])
            if len(predictions) != 1:
                raise ValueError("Expected exactly one prediction")
            label = predictions[0]
            if not isinstance(label, str) or not label.strip():
                raise ValueError("The model returned an empty or non-string label")
            confidence = 0.0
            if hasattr(model, "predict_proba"):
                probabilities = model.predict_proba([sample])
                confidence = float(max(probabilities[0]))
            elif hasattr(model, "decision_function"):
                # Decision scores are not calibrated probabilities; do not
                # misrepresent them as confidence.
                raise ModelUnavailableError(
                    f"Model {name} must expose calibrated predict_proba() scores"
                )
            else:
                raise ModelUnavailableError(
                    f"Model {name} must expose predict_proba() for confidence gating"
                )
            if confidence < self.confidence_threshold:
                raise AmbiguousPredictionError(
                    f"Model {name} confidence {confidence:.3f} is below "
                    f"threshold {self.confidence_threshold:.3f}"
                )
            return label.strip(), confidence
        except (ModelUnavailableError, AmbiguousPredictionError):
            raise
        except Exception as exc:
            raise ModelUnavailableError(f"ML inference failed for {name}: {exc}") from exc

    def extract_initial_fields(self, text: str) -> dict[str, tuple[str, float]]:
        """Predict common fields directly from full document text."""
        return {
            field: self._predict_label(f"field_{field}", text)
            for field in ("emitting_company", "document_type", "emission_date")
        }

    def predict_additional_groups(self, company: str, document_type: str) -> tuple[list[str], float]:
        """Predict applicable additional-field groups from company and type."""
        model = self._load("additional_field_groups")
        sample = f"{company}\n{document_type}"
        try:
            predictions = model.predict([sample])
            if len(predictions) != 1:
                raise ValueError("Expected exactly one group prediction")
            predicted = predictions[0]
            if isinstance(predicted, str):
                groups = [predicted] if predicted.strip() else []
            else:
                groups = [str(value).strip() for value in predicted if str(value).strip()]
            if not groups:
                raise AmbiguousPredictionError("ML model predicted no additional-field groups")
            if not hasattr(model, "predict_proba"):
                raise ModelUnavailableError(
                    "Model additional_field_groups must expose calibrated predict_proba() scores"
                )
            probabilities = model.predict_proba([sample])
            first = probabilities[0]
            if hasattr(first, "toarray"):
                first = first.toarray().ravel()
            try:
                confidence = float(max(first))
            except (TypeError, ValueError):
                confidence = float(getattr(model, "confidence_threshold", 0.0))
            if confidence < self.confidence_threshold:
                raise AmbiguousPredictionError(
                    f"Additional-field group confidence {confidence:.3f} is below "
                    f"threshold {self.confidence_threshold:.3f}"
                )
            return groups, confidence
        except (ModelUnavailableError, AmbiguousPredictionError):
            raise
        except Exception as exc:
            raise ModelUnavailableError(f"ML inference failed for additional_field_groups: {exc}") from exc

    def predict_additional_value(self, group_name: str, text: str) -> tuple[str, float]:
        """Predict a field value from document text without a configured allow-list."""
        safe_name = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in group_name)
        return self._predict_label(f"additional_value_{safe_name}", text)

    def predict_template_and_period_format(
        self, company: str, document_type: str
    ) -> tuple[str, float, str, float]:
        """Use independent ML classifiers to select template and period format."""
        sample = f"{company}\n{document_type}"
        template, template_confidence = self._predict_label("template_name", sample)
        period_format, period_confidence = self._predict_label("period_format", sample)
        return template, template_confidence, period_format, period_confidence
