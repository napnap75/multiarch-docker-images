"""Training, versioning, and activation for local document classifiers."""

from __future__ import annotations

import json
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
BASE_MODELS = ("field_emitting_company", "field_document_type", "field_emission_date",
               "additional_field_groups", "template_name", "period_format")


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
        values = self.classifier.predict_proba(self.vectorizer.transform(samples))
        return values


def model_root() -> Path:
    return Path(__import__("os").environ.get("MODEL_DIR", "./models")).resolve()


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
            if model_dir.is_dir():
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


def train_model(model_name: str, records: list[Any]) -> dict[str, Any]:
    """Train one requested model from validated records and save a versioned artifact."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", model_name):
        raise ValueError("Model names may contain only letters, numbers, underscores, and hyphens.")
    is_additional_value = model_name.startswith("additional_value_")
    if is_additional_value and not model_name.removeprefix("additional_value_"):
        raise ValueError("An additional-field group name is required.")
    if model_name not in SINGLE_LABEL_MODELS and model_name != "additional_field_groups" and not is_additional_value:
        raise ValueError(f"Unsupported model type: {model_name}")

    usable = [record for record in records if (record.extracted_text or "").strip()]
    if model_name == "additional_field_groups":
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
        # Each one-vs-rest classifier needs positive and negative examples.
        eligible_labels = [
            label for label in unique_labels
            if 0 < sum(label in row for row in labels) < len(labels)
        ]
        if not eligible_labels:
            raise ValueError(
                "No additional-field group has both positive and negative validated examples."
            )
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
    else:
        samples, labels = [], []
        if is_additional_value:
            field_name = model_name[len("additional_value_"):]
            source = "optional_fields"
            target_name = field_name
        else:
            source, target_name = SINGLE_LABEL_MODELS[model_name]
        for record in usable:
            if source == "company_type":
                if not record.emitting_company or not record.document_type:
                    continue
                sample = f"{record.emitting_company}\n{record.document_type}"
                label = getattr(record, target_name, None)
            elif source == "optional_fields":
                sample = record.extracted_text
                label = (record.optional_fields or {}).get(target_name)
            else:
                sample = record.extracted_text
                label = getattr(record, target_name, None)
            if label is None or not str(label).strip():
                continue
            samples.append(str(sample))
            labels.append(str(label).strip())
        if len(samples) < 2 or len(set(labels)) < 2:
            raise ValueError("At least two labeled validated documents and two distinct target values are required.")
        model = Pipeline([
            ("vectorizer", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)),
            ("classifier", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ])
        model.fit(samples, labels)
        sample_count = len(samples)

    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = model_root() / "versions" / model_name
    destination.mkdir(parents=True, exist_ok=True)
    artifact = destination / f"{version}.joblib"
    dump(model, artifact)
    return {
        "name": model_name,
        "version": version,
        "samples": sample_count,
        "status": "trained",
        "active": False,
        "message": "Model trained and saved. Activate it explicitly to use it for inference.",
    }
