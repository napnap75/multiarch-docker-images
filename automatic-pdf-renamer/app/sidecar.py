"""Sidecar module for Automatic PDF Renamer.

Provides the sidecar data model, schema validation, atomic writes, and I/O helpers.
Sidecars are the source of truth for file metadata (PRD section 7).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Current schema version per PRD section 7
SCHEMA_VERSION = "1.0"


class SidecarStatus(Enum):
    """File processing status (PRD section 4)."""

    NOT_PROCESSED = "not processed"
    PROCESSED = "processed"
    VALIDATED = "validated"


class EventType(Enum):
    """Event types logged in sidecar history."""

    CLASSIFIED = "classified"
    RENAMED = "renamed"
    VALIDATED = "validated"
    CORRECTED = "corrected"
    IMPORTED = "imported"
    REPROCESSED = "reprocessed"


@dataclass
class SidecarEvent:
    """A single event in the sidecar history."""

    type: str
    payload: dict[str, Any]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SidecarEvent:
        return cls(
            type=data["type"],
            payload=data["payload"],
            timestamp=data["timestamp"],
        )


@dataclass
class Sidecar:
    """Sidecar metadata for a PDF file (PRD section 7)."""

    schema_version: str = SCHEMA_VERSION
    sha256: str = ""
    original_key: str = ""
    current_key: str = ""
    status: str = SidecarStatus.NOT_PROCESSED.value
    template_name: str = ""
    confidence: float = 0.0
    emission_date: str = ""
    period: str = ""
    emitting_company: str = ""
    document_type: str = ""
    optional_fields: dict[str, Any] = field(default_factory=dict)
    extracted_text: str = ""
    created_at: str = ""
    updated_at: str = ""
    events: list[SidecarEvent] = field(default_factory=list)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = self._now_iso()
        if not self.updated_at:
            self.updated_at = self._now_iso()
        if not isinstance(self.optional_fields, dict):
            self.optional_fields = {}
        if not isinstance(self.events, list):
            self.events = []

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["events"] = [e.to_dict() for e in self.events]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Sidecar:
        events = [SidecarEvent.from_dict(e) for e in data.get("events", [])]
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            sha256=data.get("sha256", ""),
            original_key=data.get("original_key", ""),
            current_key=data.get("current_key", ""),
            status=data.get("status", SidecarStatus.NOT_PROCESSED.value),
            template_name=data.get("template_name", ""),
            confidence=data.get("confidence", 0.0),
            emission_date=data.get("emission_date", ""),
            period=data.get("period", ""),
            emitting_company=data.get("emitting_company", ""),
            document_type=data.get("document_type", ""),
            optional_fields=data.get("optional_fields", {}),
            extracted_text=data.get("extracted_text", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            events=events,
        )

    def add_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append a timestamped event to the sidecar history."""
        self.events.append(
            SidecarEvent(
                type=event_type,
                payload=payload,
                timestamp=self._now_iso(),
            )
        )
        self.updated_at = self._now_iso()

    def set_status(self, status: SidecarStatus) -> None:
        """Update the sidecar status."""
        self.status = status.value
        self.updated_at = self._now_iso()


# Required common fields per PRD section 4.1
_REQUIRED_COMMON_FIELDS = {
    "template_name",
    "emission_date",
    "emitting_company",
    "document_type",
    "period",
}

# All required fields for a valid sidecar
_REQUIRED_SIDECAR_FIELDS = {
    "schema_version",
    "sha256",
    "original_key",
    "current_key",
    "status",
    *"template_name emission_date emitting_company document_type period".split(),
    "created_at",
    "updated_at",
    "events",
}


def _check_field_presence(data: dict[str, Any], required: set[str]) -> list[str]:
    """Return list of missing required fields."""
    return [f for f in required if f not in data]


def _check_types(data: dict[str, Any]) -> list[str]:
    """Return list of fields with wrong types."""
    errors = []
    if "schema_version" in data and not isinstance(data["schema_version"], str):
        errors.append("schema_version must be str")
    if "sha256" in data and not isinstance(data["sha256"], str):
        errors.append("sha256 must be str")
    if "confidence" in data and not isinstance(data["confidence"], (int, float)):
        errors.append("confidence must be numeric")
    if "optional_fields" in data and not isinstance(data["optional_fields"], dict):
        errors.append("optional_fields must be dict")
    if "events" in data and not isinstance(data["events"], list):
        errors.append("events must be list")
    for i, event in enumerate(data.get("events", [])):
        if not isinstance(event, dict):
            errors.append(f"event[{i}] must be dict")
            continue
        if "type" not in event or not isinstance(event["type"], str):
            errors.append(f"event[{i}].type must be str")
        if "timestamp" not in event or not isinstance(event["timestamp"], str):
            errors.append(f"event[{i}].timestamp must be str")
        if "payload" not in event or not isinstance(event["payload"], dict):
            errors.append(f"event[{i}].payload must be dict")
    return errors


def validate_sidecar(data: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a sidecar dict against PRD schema.

    Returns:
        tuple of (is_valid, list of error messages)
    """
    errors: list[str] = []

    # Check required fields
    missing = _check_field_presence(data, _REQUIRED_SIDECAR_FIELDS)
    if missing:
        errors.append(f"Missing required fields: {', '.join(missing)}")

    # Check common fields presence (subset of required, but explicitly validated)
    missing_common = _check_field_presence(data, _REQUIRED_COMMON_FIELDS)
    if missing_common:
        errors.append(
            f"Missing required common fields: {', '.join(missing_common)}"
        )

    # Check types
    type_errors = _check_types(data)
    errors.extend(type_errors)

    # Check schema version
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"Unsupported schema_version: {data.get('schema_version')} "
            f"(expected {SCHEMA_VERSION})"
        )

    # Check status is valid
    valid_statuses = {s.value for s in SidecarStatus}
    if data.get("status") not in valid_statuses:
        errors.append(
            f"Invalid status '{data.get('status')}'. "
            f"Must be one of: {', '.join(valid_statuses)}"
        )

    return (len(errors) == 0, errors)


def build_sidecar(
    sha256: str,
    original_key: str,
    current_key: str,
    status: SidecarStatus,
    template_name: str,
    emission_date: str,
    period: str,
    emitting_company: str,
    document_type: str,
    confidence: float = 0.0,
    optional_fields: dict[str, Any] | None = None,
    extracted_text: str = "",
) -> Sidecar:
    """Construct a new Sidecar with required fields.

    Args:
        sha256: SHA256 hash of the PDF binary.
        original_key: Original S3 key of the file (before any renames).
        current_key: Current S3 key of the file.
        status: Current processing status.
        template_name: Document family/template name.
        emission_date: Date the document was emitted (ISO format YYYY-MM-DD).
        period: Human-readable period string.
        emitting_company: Company that emitted the document.
        document_type: Fine-grained document type within the template.
        confidence: Classification confidence score (0.0 to 1.0).
        optional_fields: Dict of template-specific optional fields.
        extracted_text: First-page extracted text.

    Returns:
        A new Sidecar instance.
    """
    sidecar = Sidecar(
        sha256=sha256,
        original_key=original_key,
        current_key=current_key,
        status=status.value,
        template_name=template_name,
        confidence=confidence,
        emission_date=emission_date,
        period=period,
        emitting_company=emitting_company,
        document_type=document_type,
        optional_fields=optional_fields or {},
        extracted_text=extracted_text,
    )
    return sidecar


def serialize(sidecar: Sidecar, indent: int = 2) -> str:
    """Serialize a Sidecar to JSON string.

    Args:
        sidecar: The Sidecar to serialize.
        indent: JSON indentation level.

    Returns:
        JSON string representation.
    """
    return json.dumps(sidecar.to_dict(), indent=indent, ensure_ascii=False, sort_keys=True)


def deserialize(json_str: str) -> Sidecar:
    """Deserialize a JSON string to a Sidecar.

    Args:
        json_str: JSON string of a sidecar.

    Returns:
        Sidecar instance.

    Raises:
        json.JSONDecodeError: If the JSON is invalid.
        ValueError: If required fields are missing.
    """
    data = json.loads(json_str)
    return Sidecar.from_dict(data)


def get_sidecar_key(pdf_key: str) -> str:
    """Return the sidecar key for a given PDF key.

    Args:
        pdf_key: The S3 key of the PDF file.

    Returns:
        The corresponding sidecar key (e.g., 'files/doc.pdf' -> 'files/doc.pdf.meta.json').
    """
    return f"{pdf_key}.meta.json"


# Default temp prefix for atomic writes
_DEFAULT_TEMP_PREFIX = "tmp/"


def write_atomic(
    storage_client: Any,
    sidecar: Sidecar,
    target_key: str,
    temp_prefix: str = _DEFAULT_TEMP_PREFIX,
) -> None:
    """Write a sidecar atomically using temp-key + copy + delete pattern.

    This ensures crash safety: if the script dies mid-write, the original
    sidecar (if any) remains intact, and the temp key can be cleaned up.

    Args:
        storage_client: An object with put_object, copy_object, delete_object methods.
            Expected interface:
                - put_object(key: str, body: str | bytes)
                - copy_object(src_key: str, dst_key: str)
                - delete_object(key: str)
                - object_exists(key: str) -> bool
        sidecar: The Sidecar to write.
        target_key: The final destination key (e.g., 'files/doc.pdf.meta.json').
        temp_prefix: Prefix for temporary keys (default: 'tmp/').

    Raises:
        RuntimeError: If any step fails.
    """
    import uuid

    # Generate a unique temp key
    temp_suffix = str(uuid.uuid4())
    temp_key = f"{temp_prefix}{temp_suffix}.meta.json"

    try:
        # Step 1: Write to temp key
        json_body = serialize(sidecar)
        storage_client.put_object(temp_key, json_body)

        # Step 2: Copy temp key to target key (atomic in S3)
        # If target exists, this overwrites it atomically
        storage_client.copy_object(temp_key, target_key)

        # Step 3: Delete temp key
        storage_client.delete_object(temp_key)

    except Exception as e:
        # Clean up temp key if it exists
        try:
            if hasattr(storage_client, "object_exists") and storage_client.object_exists(
                temp_key
            ):
                storage_client.delete_object(temp_key)
        except Exception:
            pass  # Best effort cleanup
        raise RuntimeError(f"Atomic sidecar write failed: {e}") from e


def read_sidecar(storage_client: Any, pdf_key: str) -> Sidecar | None:
    """Read a sidecar from storage.

    Args:
        storage_client: An object with get_object method.
            Expected interface:
                - get_object(key: str) -> str | bytes | None
        pdf_key: The S3 key of the PDF file.

    Returns:
        Sidecar if found, None otherwise.
    """
    sidecar_key = get_sidecar_key(pdf_key)
    try:
        json_body = storage_client.get_object(sidecar_key)
        if json_body is None:
            return None
        if isinstance(json_body, bytes):
            json_body = json_body.decode("utf-8")
        return deserialize(json_body)
    except (json.JSONDecodeError, ValueError):
        return None


def sidecar_exists(storage_client: Any, pdf_key: str) -> bool:
    """Check if a sidecar exists for the given PDF key.

    Args:
        storage_client: An object with object_exists method.
        pdf_key: The S3 key of the PDF file.

    Returns:
        True if sidecar exists, False otherwise.
    """
    sidecar_key = get_sidecar_key(pdf_key)
    return storage_client.object_exists(sidecar_key)


def matching_sha256(storage_client: Any, pdf_key: str, expected_sha: str) -> bool:
    """Check if the sidecar for pdf_key has the expected SHA256.

    Used for resumability: skip documents already imported with matching hash.

    Args:
        storage_client: An object with get_object method.
        pdf_key: The S3 key of the PDF file.
        expected_sha: The expected SHA256 hash.

    Returns:
        True if sidecar exists and sha256 matches, False otherwise.
    """
    sidecar = read_sidecar(storage_client, pdf_key)
    if sidecar is None:
        return False
    return sidecar.sha256 == expected_sha
