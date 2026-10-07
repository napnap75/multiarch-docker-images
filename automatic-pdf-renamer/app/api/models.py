from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Simple health payload."""

    status: str = "ok"
    app: str = "automatic-pdf-renamer"


class FileListItem(BaseModel):
    """File metadata returned by the dashboard list endpoint."""

    id: str
    status: str
    current_key: str
    original_key: str | None = None
    template_name: str | None = None
    period_format: str | None = None
    document_type: str | None = None
    emitting_company: str | None = None
    period: str | None = None
    emission_date: str | None = None
    confidence: float | None = None
    updated_at: str | None = None


class FileDetail(FileListItem):
    """Detailed file payload."""

    schema_version: str | None = None
    created_at: str | None = None
    extracted_text: str | None = None
    optional_fields: dict[str, Any] = Field(default_factory=dict)
    history: list[dict[str, Any]] = Field(default_factory=list)


class ValidationRequest(BaseModel):
    """Body for validating or correcting a file."""

    template_id: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class DocumentEditRequest(BaseModel):
    """Editable document classification and naming fields."""

    template_name: str = Field(min_length=1)
    period_format: str = Field(min_length=1)
    emitting_company: str = Field(min_length=1)
    document_type: str = Field(min_length=1)
    emission_date: date
    optional_fields: dict[str, Any] = Field(default_factory=dict)


class ValidationResponse(BaseModel):
    """Validation result payload."""

    id: str
    status: str
    current_key: str
    template_name: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    updated: bool = False
