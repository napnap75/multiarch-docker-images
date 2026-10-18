"""Train and replace the complete local document model set."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
import shutil
import uuid
from pathlib import Path
from typing import Any

from joblib import dump
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MultiLabelBinarizer


SINGLE_LABEL_MODELS = {
    "field_emitting_company": ("extracted_text", "emitting_company"),
    "field_document_type": ("extracted_text", "document_type"),
    "template_name": ("company_type_fields", "template_name"),
    "period_format": ("company_type_fields", "period_format"),
}
ADDITIONAL_VALUES_MODEL = "additional_field_values"
BASE_MODELS = (
    "field_emitting_company",
    "field_document_type",
    "additional_field_groups",
    ADDITIONAL_VALUES_MODEL,
    "template_name",
    "period_format",
)


class AdditionalGroupsModel:
    """Predict a multi-label set of additional-field groups."""

    def __init__(self, vectorizer: TfidfVectorizer, classifier: OneVsRestClassifier, labels: list[str]):
        self.vectorizer = vectorizer
        self.classifier = classifier
        self.labels = labels

    def predict(self, samples):
        probabilities = self.predict_proba(samples)
        return [[label for label, probability in zip(self.labels, row) if probability >= 0.5]
                for row in probabilities]

    def predict_proba(self, samples):
        return self.classifier.predict_proba(self.vectorizer.transform(samples))


class AdditionalValuesModel:
    """One shared token-level model that extracts values as spans from document text."""

    def __init__(self, vectorizer: DictVectorizer, classifier: Any, trained_fields: list[str]):
        self.vectorizer = vectorizer
        self.classifier = classifier
        self.trained_fields = trained_fields

    @staticmethod
    def _tokens(text: str) -> list[tuple[str, int, int]]:
        return [(match.group(), match.start(), match.end()) for match in re.finditer(r"\S+", text)]

    @staticmethod
    def _features(tokens: list[tuple[str, int, int]], index: int) -> dict[str, Any]:
        token = tokens[index][0]
        normalized = token.casefold()
        features: dict[str, Any] = {
            "token": normalized, "prefix2": normalized[:2], "prefix4": normalized[:4],
            "suffix2": normalized[-2:], "suffix4": normalized[-4:],
            "shape_has_digit": any(char.isdigit() for char in token),
            "shape_has_alpha": any(char.isalpha() for char in token),
            "shape_upper": token.isupper(), "shape_title": token.istitle(),
            "shape_length": min(len(token), 30), "position_bucket": min(index // 10, 20),
        }
        for offset in (-2, -1, 1, 2):
            neighbor_index = index + offset
            features[f"token_{offset}"] = (
                tokens[neighbor_index][0].casefold()
                if 0 <= neighbor_index < len(tokens) else "<BOUNDARY>"
            )
        return features

    def predict_values(self, text: str, fields: list[str]) -> dict[str, tuple[str, float]]:
        tokens = self._tokens(text)
        if not tokens:
            raise ValueError("The document contains no tokens for additional-field extraction.")
        unknown = sorted(set(fields) - set(self.trained_fields))
        if unknown:
            raise ValueError(
                "The shared extraction model has no training spans for: " + ", ".join(unknown)
                + ". Validate documents where these values appear in the extracted text, then retrain."
            )
        features = self.vectorizer.transform([
            self._features(tokens, index) for index in range(len(tokens))
        ])
        probabilities = self.classifier.predict_proba(features)
        classes = list(self.classifier.classes_)
        predicted = self.classifier.predict(features)
        output: dict[str, tuple[str, float]] = {}
        for field_name in fields:
            spans: list[tuple[int, int, float]] = []
            current_start: int | None = None
            current_end: int | None = None
            scores: list[float] = []
            for index, tag in enumerate(predicted):
                token_tag = str(tag)
                if token_tag == f"B::{field_name}":
                    if current_start is not None and current_end is not None:
                        spans.append((current_start, current_end, sum(scores) / len(scores)))
                    current_start, current_end = tokens[index][1], tokens[index][2]
                    scores = [float(probabilities[index][classes.index(token_tag)])]
                elif token_tag == f"I::{field_name}" and current_start is not None:
                    current_end = tokens[index][2]
                    scores.append(float(probabilities[index][classes.index(token_tag)]))
                elif current_start is not None and current_end is not None:
                    spans.append((current_start, current_end, sum(scores) / len(scores)))
                    current_start = current_end = None
                    scores = []
            if current_start is not None and current_end is not None:
                spans.append((current_start, current_end, sum(scores) / len(scores)))
            if not spans:
                output[field_name] = ("", 0.0)
                continue
            span_start, span_end, confidence = max(spans, key=lambda span: span[2])
            output[field_name] = (text[span_start:span_end].strip(" :;,. \t\r\n"), confidence)
        return output


def model_root() -> Path:
    return Path(os.environ.get("MODEL_DIR", "./models")).resolve()


def _active_file() -> Path:
    return model_root() / "active_models.json"


def _read_active() -> dict[str, str]:
    path = _active_file()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def list_models() -> dict[str, Any]:
    """Return the current model set without exposing model history."""
    root = model_root()
    active = _read_active()
    models = []
    for name in BASE_MODELS:
        version = active.get(name)
        artifact = root / "sets" / version / f"{name}.joblib" if version else None
        if version and not (artifact and artifact.is_file()):
            artifact = root / "versions" / name / f"{version}.joblib"
        models.append({"name": name, "has_active_model": bool(artifact and artifact.is_file())})
    return {"models": models, "model_dir": str(root), "training_source": "validated documents",
            "complete": all(model["has_active_model"] for model in models)}


def _new_classifier() -> Pipeline:
    return Pipeline([
        ("vectorizer", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)),
        ("classifier", LogisticRegression(max_iter=1000, class_weight="balanced")),
    ])


def train_model(model_name: str, records: list[Any], output_root: Path) -> dict[str, Any]:
    """Train one component of the full model set into a staging directory."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", model_name):
        raise ValueError("Model names may contain only letters, numbers, underscores, and hyphens.")
    if model_name not in SINGLE_LABEL_MODELS and model_name not in {
        "additional_field_groups", ADDITIONAL_VALUES_MODEL
    }:
        if model_name.startswith("additional_value_"):
            raise ValueError(
                "Per-field value models have been replaced by the shared "
                "'additional_field_values' model."
            )
        raise ValueError(f"Unsupported model type: {model_name}")

    usable = [record for record in records if (record.extracted_text or "").strip()]
    if model_name == ADDITIONAL_VALUES_MODEL:
        token_samples: list[dict[str, Any]] = []
        token_labels: list[str] = []
        field_counts: dict[str, int] = {}
        for record in usable:
            text = str(record.extracted_text or "")
            tokens = AdditionalValuesModel._tokens(text)
            if not tokens:
                continue
            labels_for_tokens = ["O"] * len(tokens)
            occupied: set[int] = set()
            for raw_name, raw_value in (record.optional_fields or {}).items():
                field_name = str(raw_name).strip()
                value = str(raw_value).strip() if raw_value is not None else ""
                if not field_name or not value:
                    continue
                match = re.search(re.escape(value), text, flags=re.IGNORECASE)
                if not match:
                    continue
                covered = [
                    index for index, (_, token_start, token_end) in enumerate(tokens)
                    if token_start < match.end() and token_end > match.start()
                ]
                if not covered or any(index in occupied for index in covered):
                    continue
                labels_for_tokens[covered[0]] = f"B::{field_name}"
                for index in covered[1:]:
                    labels_for_tokens[index] = f"I::{field_name}"
                occupied.update(covered)
                field_counts[field_name] = field_counts.get(field_name, 0) + 1
            token_samples.extend(
                AdditionalValuesModel._features(tokens, index) for index in range(len(tokens))
            )
            token_labels.extend(labels_for_tokens)

        trained_fields = sorted(name for name, count in field_counts.items() if count >= 2)
        if not trained_fields:
            raise ValueError(
                "At least one additional field must have two validated examples whose labeled values "
                "appear verbatim in the extracted document text."
            )
        valid_tags = {"O"} | {
            tag for field_name in trained_fields for tag in (f"B::{field_name}", f"I::{field_name}")
        }
        filtered_samples = [sample for sample, label in zip(token_samples, token_labels) if label in valid_tags]
        filtered_labels = [label for label in token_labels if label in valid_tags]
        vectorizer = DictVectorizer(sparse=True)
        features = vectorizer.fit_transform(filtered_samples)
        classifier = LogisticRegression(max_iter=1000, class_weight="balanced")
        classifier.fit(features, filtered_labels)
        model = AdditionalValuesModel(vectorizer, classifier, trained_fields)
        sample_count = len(usable)
    elif model_name == "additional_field_groups":
        samples, labels = [], []
        for record in usable:
            if not record.emitting_company or not record.document_type:
                continue
            groups = sorted(str(key) for key in (record.optional_fields or {}).keys() if str(key).strip())
            samples.append(f"{record.emitting_company}\n{record.document_type}")
            labels.append(groups)
        unique_labels = sorted({label for row in labels for label in row})
        if len(samples) < 2 or not unique_labels:
            raise ValueError("At least two validated documents and one labeled additional-field group are required.")
        eligible_labels = [
            label for label in unique_labels
            if 0 < sum(label in row for row in labels) < len(labels)
        ]
        if not eligible_labels:
            raise ValueError("No additional-field group has both positive and negative validated examples.")
        unique_labels = eligible_labels
        labels = [[label for label in row if label in unique_labels] for row in labels]
        mlb = MultiLabelBinarizer(classes=unique_labels)
        target = mlb.fit_transform(labels)
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
        features = vectorizer.fit_transform(samples)
        classifier = OneVsRestClassifier(LogisticRegression(max_iter=1000, class_weight="balanced"))
        classifier.fit(features, target)
        model = AdditionalGroupsModel(vectorizer, classifier, unique_labels)
        sample_count = len(samples)
        trained_fields = []
    else:
        samples, labels = [], []
        source, target_name = SINGLE_LABEL_MODELS[model_name]
        for record in usable:
            if source in {"company_type", "company_type_fields"}:
                if not record.emitting_company or not record.document_type:
                    continue
                sample = f"{record.emitting_company}\\n{record.document_type}"
                if source == "company_type_fields":
                    fields = record.optional_fields or {}
                    sample += "\\n" + "\\n".join(
                        f"{key}: {value}" for key, value in sorted(fields.items())
                    )
                label = getattr(record, target_name, None)
            else:
                sample = record.extracted_text
                label = getattr(record, target_name, None)
            if label is None or not str(label).strip():
                continue
            samples.append(str(sample))
            labels.append(str(label).strip())
        if len(samples) < 2 or len(set(labels)) < 2:
            raise ValueError("At least two labeled validated documents and two distinct target values are required.")
        model = _new_classifier()
        model.fit(samples, labels)
        sample_count = len(samples)
        trained_fields = []

    version = "current"
    destination = output_root / "versions" / model_name
    destination.mkdir(parents=True, exist_ok=True)
    artifact = destination / f"{version}.joblib"
    dump(model, artifact)
    result = {
        "name": model_name,
        "version": version,
        "samples": sample_count,
        "status": "trained",
        "active": False,
        "message": "Model trained in staging; it is activated only when the full model set is replaced.",
    }
    if model_name == ADDITIONAL_VALUES_MODEL:
        result["trained_fields"] = trained_fields
    return result


def train_all_models(records: list[Any], progress_callback=None) -> dict[str, Any]:
    """Train all models in staging and atomically switch the active-set pointer."""
    root = model_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{uuid.uuid4().hex}"
    model_names = list(BASE_MODELS)
    generation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    set_published = False
    pointer_switched = False
    try:
        staging.mkdir()
        results = []
        for index, model_name in enumerate(model_names, start=1):
            if progress_callback:
                progress_callback(
                    round((index - 1) * 90 / len(model_names)),
                    f"Training {model_name} ({index}/{len(model_names)})",
                )
            results.append(train_model(model_name, records, output_root=staging))

        staged_set = staging / "sets" / generation
        staged_set.mkdir(parents=True)
        for model_name, result in zip(model_names, results):
            artifact = staging / "versions" / model_name / "current.joblib"
            artifact.rename(staged_set / f"{model_name}.joblib")
            result["version"] = generation
        shutil.rmtree(staging / "versions", ignore_errors=True)

        active = {name: generation for name in model_names}
        staged_active = staging / "active_models.json"
        staged_active.write_text(json.dumps(active, indent=2), encoding="utf-8")
        if progress_callback:
            progress_callback(95, "Switching to the new model set")

        sets_root = root / "sets"
        sets_root.mkdir(parents=True, exist_ok=True)
        published_set = sets_root / generation
        staged_set.rename(published_set)
        set_published = True

        # A single atomic pointer replacement switches every model at once.
        os.replace(staged_active, _active_file())
        pointer_switched = True

        # The new set is active and complete; remove all old artifacts and history.
        for old_set in sets_root.iterdir():
            if old_set.is_dir() and old_set.name != generation:
                shutil.rmtree(old_set, ignore_errors=True)
        shutil.rmtree(root / "versions", ignore_errors=True)
        if progress_callback:
            progress_callback(100, "All models trained and replaced successfully")
        return {"status": "completed", "models": results, "count": len(results)}
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Full model retraining failed; the previous active model set was retained"
        )
        if set_published and not pointer_switched:
            shutil.rmtree(root / "sets" / generation, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
