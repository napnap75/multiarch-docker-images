"""Training, versioning, and activation for local document classifiers."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from joblib import dump
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MultiLabelBinarizer


SINGLE_LABEL_MODELS = {
    "field_emitting_company": ("extracted_text", "emitting_company"),
    "field_document_type": ("extracted_text", "document_type"),
    "field_emission_date": ("extracted_text", "emission_date"),
    "template_name": ("company_type", "template_name"),
    "period_format": ("company_type", "period_format"),
}
ADDITIONAL_VALUES_MODEL = "additional_field_values"
BASE_MODELS = (
    "field_emitting_company",
    "field_document_type",
    "field_emission_date",
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
    """One versioned artifact containing a shared vectorizer and field-specific output heads."""

    def __init__(self, vectorizer: TfidfVectorizer, field_models: dict[str, Any]):
        self.vectorizer = vectorizer
        self.field_models = field_models

    def predict_values(self, text: str, fields: list[str]) -> dict[str, tuple[str, float]]:
        features = self.vectorizer.transform([text])
        results: dict[str, tuple[str, float]] = {}
        for field_name in fields:
            classifier = self.field_models.get(field_name)
            if classifier is None:
                raise ValueError(
                    f"No trained output for additional field '{field_name}'. "
                    "Validate more documents containing this field, then retrain the shared model."
                )
            label = str(classifier.predict(features)[0]).strip()
            probabilities = classifier.predict_proba(features)[0]
            confidence = float(max(probabilities))
            if not label:
                raise ValueError(f"The shared model returned an empty value for '{field_name}'.")
            results[field_name] = (label, confidence)
        return results


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
    root = model_root()
    active = _read_active()
    versions_root = root / "versions"
    model_names = set(BASE_MODELS)
    if versions_root.exists():
        for model_dir in versions_root.iterdir():
            # Hide legacy per-field artifacts from the current inventory. They remain
            # on disk for rollback/inspection but are no longer used by inference.
            if model_dir.is_dir() and not model_dir.name.startswith("additional_value_"):
                model_names.add(model_dir.name)
    result = []
    for name in sorted(model_names):
        directory = versions_root / name
        versions = sorted((p.stem for p in directory.glob("*.joblib")), reverse=True) if directory.exists() else []
        active_version = active.get(name)
        result.append({
            "name": name,
            "active_version": active_version,
            "versions": [{"version": version, "active": version == active_version}
                         for version in versions],
            "has_active_model": bool(active_version and (directory / f"{active_version}.joblib").is_file()),
        })
    return {"models": result, "model_dir": str(root), "training_source": "validated documents"}


def activate_model(model_name: str, version: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", model_name) or not re.fullmatch(r"[A-Za-z0-9T_.-]+", version):
        raise ValueError("Invalid model name or version")
    artifact = model_root() / "versions" / model_name / f"{version}.joblib"
    if not artifact.is_file():
        raise FileNotFoundError(f"Model version '{version}' does not exist for '{model_name}'")
    active = _read_active()
    active[model_name] = version
    root = model_root()
    root.mkdir(parents=True, exist_ok=True)
    temporary = _active_file().with_suffix(".tmp")
    temporary.write_text(json.dumps(active, indent=2), encoding="utf-8")
    temporary.replace(_active_file())
    return {"name": model_name, "active_version": version, "status": "active"}


def _new_classifier() -> Pipeline:
    return Pipeline([
        ("vectorizer", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)),
        ("classifier", LogisticRegression(max_iter=1000, class_weight="balanced")),
    ])


def train_model(model_name: str, records: list[Any]) -> dict[str, Any]:
    """Train one requested model from validated records and save a versioned artifact."""
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
        field_samples: dict[str, list[str]] = {}
        field_labels: dict[str, list[str]] = {}
        for record in usable:
            for raw_name, raw_value in (record.optional_fields or {}).items():
                field_name = str(raw_name).strip()
                value = str(raw_value).strip() if raw_value is not None else ""
                if not field_name or not value:
                    continue
                field_samples.setdefault(field_name, []).append(record.extracted_text)
                field_labels.setdefault(field_name, []).append(value)

        eligible_fields = {
            field_name: (samples, field_labels[field_name])
            for field_name, samples in field_samples.items()
            if len(samples) >= 2 and len(set(field_labels[field_name])) >= 2
        }
        if not eligible_fields:
            raise ValueError(
                "At least one additional field needs two validated examples with distinct non-empty values."
            )

        # A single vectorizer is shared by every output head. Each field has its own
        # categorical output head because different fields have different value domains.
        all_texts = [text for samples, _ in eligible_fields.values() for text in samples]
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
        vectorizer.fit(all_texts)
        field_models: dict[str, Any] = {}
        for field_name, (samples, labels) in eligible_fields.items():
            classifier = LogisticRegression(max_iter=1000, class_weight="balanced")
            classifier.fit(vectorizer.transform(samples), labels)
            field_models[field_name] = classifier
        model = AdditionalValuesModel(vectorizer, field_models)
        sample_count = sum(len(samples) for samples, _ in eligible_fields.values())
        trained_fields = sorted(field_models)
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
            if source == "company_type":
                if not record.emitting_company or not record.document_type:
                    continue
                sample = f"{record.emitting_company}\n{record.document_type}"
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

    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = model_root() / "versions" / model_name
    destination.mkdir(parents=True, exist_ok=True)
    artifact = destination / f"{version}.joblib"
    dump(model, artifact)
    result = {
        "name": model_name,
        "version": version,
        "samples": sample_count,
        "status": "trained",
        "active": False,
        "message": "Model trained and saved. Activate it explicitly to use it for inference.",
    }
    if model_name == ADDITIONAL_VALUES_MODEL:
        result["trained_fields"] = trained_fields
    return result
