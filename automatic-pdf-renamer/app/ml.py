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
import re
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

    def predict_labels(
        self,
        fields: dict[str, tuple[str, Any]],
    ) -> tuple[dict[str, tuple[str, float]], dict[str, Exception]]:
        """Predict independent fields without letting one failure block the others."""
        predictions: dict[str, tuple[str, float]] = {}
        errors: dict[str, Exception] = {}
        for field, (model_name, sample) in fields.items():
            try:
                predictions[field] = self._predict_label(model_name, sample)
            except (ModelUnavailableError, AmbiguousPredictionError) as exc:
                errors[field] = exc
        return predictions, errors

    def extract_initial_fields(self, text: str) -> dict[str, tuple[str, float]]:
        """Predict common fields directly from full document text."""
        fields, errors = self.predict_labels({
            field: (f"field_{field}", text)
            for field in ("emitting_company", "document_type")
        })
        if errors:
            raise next(iter(errors.values()))
        logger.debug("Extracted initial fields %r from text=%r", fields, text)
        return fields

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
            logger.debug('Predicted additional-field group %r (confidence=%.3f)', groups[0], confidence)
            return groups, confidence
        except (ModelUnavailableError, AmbiguousPredictionError):
            raise
        except Exception as exc:
            raise ModelUnavailableError(f"ML inference failed for additional_field_groups: {exc}") from exc

    def predict_additional_values(
        self,
        field_names: list[str],
        text: str,
        *,
        allow_partial: bool = False,
    ) -> dict[str, tuple[str, float]]:
        """Extract requested fields, optionally omitting individual low-confidence results."""
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
                    error = AmbiguousPredictionError(
                        f"Additional field '{field_name}' confidence {confidence:.3f} is below "
                        f"threshold {self.confidence_threshold:.3f}"
                    )
                    if allow_partial:
                        logger.debug("Omitting low-confidence additional field %s: %s", field_name, error)
                        continue
                    raise error
                results[field_name] = (str(value), float(confidence))
                logger.debug('Predicted additional field %s=%r (confidence=%.3f)', field_name, value, confidence)
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
        fields_text = "\n".join(f"{key}: {value}" for key, value in sorted(additional_fields.items()))
        sample = f"{emitting_company}\n{document_type}\n{fields_text}"
        predictions, errors = self.predict_labels({
            "template_name": ("template_name", sample),
            "period_format": ("period_format", sample),
        })
        if errors:
            raise next(iter(errors.values()))
        template, template_confidence = predictions["template_name"]
        period_format, period_confidence = predictions["period_format"]
        logger.debug(
            "Predicted template=%s (confidence=%.3f), period_format=%s (confidence=%.3f) "
            "for company=%r, document_type=%r, additional_fields=%r",
            template,
            template_confidence,
            period_format,
            period_confidence,
            emitting_company,
            document_type,
            additional_fields,
        )
        return template, template_confidence, period_format, period_confidence

    @staticmethod
    def match_configured_additional_value(
        text: str,
        predicted_value: str,
        configured_values: list[str],
    ) -> str | None:
        """Resolve an extracted span to a complete, unambiguous configured value."""
        matched_values = []
        for raw_value in configured_values:
            value = str(raw_value).strip()
            words = value.split()
            if not words:
                continue
            pattern = r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)"
            if re.search(pattern, text, flags=re.IGNORECASE):
                matched_values.append(value)

        if len(matched_values) == 1:
            return matched_values[0]
        if not matched_values:
            return None

        predicted_words = set(re.findall(r"\w+", predicted_value.casefold()))
        scores = {
            value: len(predicted_words & set(re.findall(r"\w+", value.casefold())))
            for value in matched_values
        }
        best_score = max(scores.values())
        best_matches = [value for value, score in scores.items() if score == best_score]
        return best_matches[0] if best_score and len(best_matches) == 1 else None

    def suggest_document_fields(
        self,
        text: str,
        company: str,
        document_type: str,
        current_fields: dict[str, str],
        configured_values: dict[str, list[str]],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """Collect independent model and configured-value suggestions for one document."""
        suggestions: dict[str, dict[str, Any]] = {}
        decisions = {
            "emitting_company": "skipped: model not evaluated",
            "document_type": "skipped: model not evaluated",
            "template_name": "skipped: prerequisites not met",
            "period_format": "skipped: prerequisites not met",
            "additional_fields": "skipped: groups not evaluated",
        }
        initial, initial_errors = self.predict_labels({
            "emitting_company": ("field_emitting_company", text),
            "document_type": ("field_document_type", text),
        })
        for field, (value, confidence) in initial.items():
            suggestions[field] = {"value": value, "confidence": confidence, "source": "model"}
            decisions[field] = f"suggested: {value!r} ({confidence:.3f})"
        for field, error in initial_errors.items():
            decisions[field] = f"unavailable: {error}" if isinstance(error, ModelUnavailableError) else f"ambiguous: {error}"

        suggested_company = initial.get("emitting_company", (company, 0.0))[0]
        suggested_type = initial.get("document_type", (document_type, 0.0))[0]
        if not suggested_company or not suggested_type:
            missing = [
                name for name, value in (("emitting_company", suggested_company), ("document_type", suggested_type))
                if not value
            ]
            reason = f"skipped: missing {', '.join(missing)}"
            decisions["template_name"] = reason
            decisions["period_format"] = reason
            return suggestions, decisions

        try:
            groups, _ = self.predict_additional_groups(suggested_company, suggested_type)
            value_model = self._load("additional_field_values")
            trained_fields = getattr(value_model, "trained_fields", None)
            if trained_fields is None:
                extractable_groups = groups
                unsupported_groups: list[str] = []
            else:
                supported_fields = set(trained_fields)
                extractable_groups = [name for name in groups if name in supported_fields]
                unsupported_groups = sorted(set(groups) - supported_fields)
            decisions["additional_fields"] = (
                f"predicted={groups}; extracting={extractable_groups}; unsupported={unsupported_groups}"
            )
            extracted = self.predict_additional_values(
                extractable_groups, text, allow_partial=True
            ) if extractable_groups else {}
            matched = {
                name: value
                for name, (span, _confidence) in extracted.items()
                if (value := self.match_configured_additional_value(
                    text, span, configured_values.get(name, [])
                )) is not None
            }
            if matched:
                suggestions["optional_fields"] = {"value": matched, "source": "configured"}
            model_spans = {
                name: {"value": value, "confidence": confidence}
                for name, (value, confidence) in extracted.items()
            }
            decisions["additional_fields"] += (
                f"; model_spans={model_spans}; configured_matches={matched}; "
                f"omitted_low_confidence={sorted(set(extractable_groups) - set(extracted))}; "
                f"omitted_without_configured_match={sorted(set(extracted) - set(matched))}"
            )
            inference_fields = {**matched, **current_fields}
            fields_text = "\n".join(
                f"{key}: {value}" for key, value in sorted(inference_fields.items())
            )
            sample = f"{suggested_company}\n{suggested_type}\n{fields_text}"
            final, final_errors = self.predict_labels({
                "template_name": ("template_name", sample),
                "period_format": ("period_format", sample),
            })
            for field, (value, confidence) in final.items():
                suggestions[field] = {"value": value, "confidence": confidence, "source": "model"}
                decisions[field] = f"suggested: {value!r} ({confidence:.3f})"
            for field, error in final_errors.items():
                decisions[field] = f"unavailable: {error}" if isinstance(error, ModelUnavailableError) else f"ambiguous: {error}"
        except (ModelUnavailableError, AmbiguousPredictionError) as exc:
            reason = f"skipped: additional-field inference failed ({exc})"
            decisions["additional_fields"] = f"unavailable: {exc}"
            decisions["template_name"] = reason
            decisions["period_format"] = reason
        return suggestions, decisions
