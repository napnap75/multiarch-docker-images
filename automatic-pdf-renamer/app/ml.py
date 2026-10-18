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
        self._model_versions: dict[str, str] = {}

    def _load(self, name: str) -> Any:
        active_path = self.model_dir / "active_models.json"
        try:
            active = __import__("json").loads(active_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            active = {}
        version = active.get(name)
        if not version:
            raise ModelUnavailableError(
                f"No active model artifact for {name}; retrain all models from Maintenance."
            )
        if name in self._models and self._model_versions.get(name) == version:
            return self._models[name]
        path = self.model_dir / "sets" / version / f"{name}.joblib"
        if not path.is_file():
            # Keep the pre-retraining model layout usable until the first full replacement.
            path = self.model_dir / "versions" / name / f"{version}.joblib"
        if not path.is_file():
            raise ModelUnavailableError(
                f"Active model artifact is missing for {name} version {version}."
            )
        try:
            import joblib
            model = joblib.load(path)
        except Exception as exc:
            raise ModelUnavailableError(f"Could not load ML model {path.name}: {exc}") from exc
        self._models[name] = model
        self._model_versions[name] = version
        return model

    def _predict_label(self, name: str, sample: Any) -> tuple[str, float]:
        model = self._load(name)
        try:
            predictions = model.predict([sample])
            logger.debug("ML model %s predicted %s for sample=%r", name, predictions, sample)
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
        except (ModelUnavailableError, AmbiguousPredictionError) as exc:
            logger.debug("ML model %s failed for sample=%r: %s", name, sample, exc)
            raise
        except Exception as exc:
            raise ModelUnavailableError(f"ML inference failed for {name}: {exc}") from exc

    def extract_initial_fields(self, text: str) -> dict[str, tuple[str, float]]:
        """Predict common fields directly from full document text."""
        return {
            field: self._predict_label(f"field_{field}", text)
            for field in ("emitting_company", "document_type")
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

    def predict_additional_values(self, field_names: list[str], text: str) -> dict[str, tuple[str, float]]:
        """Extract all requested additional fields using one shared model artifact."""
        model = self._load("additional_field_values")
        if not hasattr(model, "predict_values"):
            raise ModelUnavailableError(
                "The active additional_field_values artifact does not support shared extraction; retrain it."
            )
        try:
            predictions = model.predict_values(text, field_names)
            results: dict[str, tuple[str, float]] = {}
            for field_name in field_names:
                value, confidence = predictions[field_name]
                if not value or confidence < self.confidence_threshold:
                    raise AmbiguousPredictionError(
                        f"Additional field '{field_name}' confidence {confidence:.3f} is below "
                        f"threshold {self.confidence_threshold:.3f}"
                    )
                results[field_name] = (str(value), float(confidence))
            return results
        except (ModelUnavailableError, AmbiguousPredictionError):
            raise
        except ValueError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except Exception as exc:
            raise ModelUnavailableError(f"ML inference failed for additional_field_values: {exc}") from exc

    def predict_template_and_period_format(
        self,
        emitting_company: str,
        document_type: str,
        additional_fields: dict[str, str],
    ) -> tuple[str, float, str, float]:
        """Predict template and period format from company, type, and extracted fields."""
        fields_text = "\n".join(
            f"{key}: {value}" for key, value in sorted(additional_fields.items())
        )
        sample = f"{emitting_company}\n{document_type}\n{fields_text}"
        template, template_confidence = self._predict_label("template_name", sample)
        period_format, period_confidence = self._predict_label("period_format", sample)
        return template, template_confidence, period_format, period_confidence
