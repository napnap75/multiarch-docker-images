from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.config import TemplateRegistry, load_templates
from app.db import FileRecord, SQLiteFileRepository
from app.period import format_period_from_emission_date
from app.pipeline import DocumentProcessor
from app.storage import StorageBackend
from app.model_manager import list_models, train_all_models
from app.ml import LocalMLPipeline

from .deps import get_current_user
from .models import (
    FileDetail,
    FileListItem,
    ConfigUpdateRequest,
    DocumentEditRequest,
    BulkEditRequest,
    HealthResponse,
    ValidationRequest,
    ValidationResponse,
)

router = APIRouter(tags=["files"])

logger = logging.getLogger('uvicorn.error')


def _storage(request: Request) -> StorageBackend:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(status_code=500, detail="Storage backend is not configured")
    return storage


def _db(request: Request) -> SQLiteFileRepository:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=500, detail="SQLite repository is not configured")
    return db


def _registry(request: Request):
    registry = getattr(request.app.state, "template_registry", None)
    if registry is None:
        registry = load_templates()
        request.app.state.template_registry = registry
    return registry


def _template_config_path() -> Path:
    configured_path = Path(os.environ.get("TEMPLATE_PATH", "./config.jsonc"))
    if configured_path.suffix.lower() not in {".json", ".jsonc"}:
        raise HTTPException(status_code=400, detail="The configured template file is not JSON or JSONC.")
    if not configured_path.is_file():
        raise HTTPException(status_code=404, detail="The configured template file does not exist.")
    return configured_path.resolve()


def _parse_file_metadata(raw: str | bytes | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _as_file_item(file_id: str, metadata: dict[str, Any]) -> FileListItem:
    return FileListItem(
        id=file_id,
        status=str(metadata.get("status", "unknown")),
        current_key=str(metadata.get("current_key", metadata.get("key", ""))),
        original_key=metadata.get("original_key"),
        template_name=metadata.get("template_name"),
        period_format=metadata.get("period_format"),
        document_type=metadata.get("document_type"),
        emitting_company=metadata.get("emitting_company"),
        period=metadata.get("period"),
        emission_date=metadata.get("emission_date"),
        confidence=metadata.get("confidence"),
        updated_at=metadata.get("updated_at"),
        optional_fields=dict(metadata.get("optional_fields") or {}),
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Service health checkpoint."""
    logger.debug("API GET /health: health check requested")
    result = HealthResponse(status="ok", app="automatic-pdf-renamer")
    logger.debug("API GET /health: status=%s", result.status)
    return result


@router.get("/files", response_model=list[FileListItem])
async def list_files(
    request: Request,
    response: Response,
    current_user: dict[str, str] | None = Depends(get_current_user),
    status: str | None = None,
    template: str | None = None,
    period_format: str | None = None,
    q: str | None = None,
    current_key: str | None = None,
    emitting_company: str | None = None,
    document_type: str | None = None,
    period: str | None = None,
    confidence: str | None = None,
    updated_at: str | None = None,
    emission_date: str | None = None,
    sort_by: str = "updated_at",
    sort_order: str = "desc",
    page: int = 1,
) -> list[FileListItem]:
    """List documents and report filtered and global counts in response headers."""
    logger.debug("API GET /files: user=%s page=%s sort_by=%s sort_order=%s", (current_user or {}).get("username", "anonymous"), page, sort_by, sort_order)
    logger.debug("API GET /files: active filters=%s", {key: value for key, value in {"status": status, "template": template, "period_format": period_format, "q": q, "current_key": current_key, "emitting_company": emitting_company, "document_type": document_type, "period": period, "confidence": confidence, "updated_at": updated_at, "emission_date": emission_date, **{f"additional_field__{k}": v for k, v in additional_fields.items()}}.items() if value is not None})
    storage = _storage(request)
    db = _db(request)
    additional_fields = {
        key.removeprefix("additional_field__"): value
        for key, value in request.query_params.items()
        if key.startswith("additional_field__") and value
    }
    filters = {
        "status": status,
        "template": template,
        "q": q,
        "current_key": current_key,
        "emitting_company": emitting_company,
        "document_type": document_type,
        "period": period,
        "period_format": period_format,
        "confidence": confidence,
        "updated_at": updated_at,
        "emission_date": emission_date,
        "additional_fields": additional_fields,
    }

    if db.count_files() == 0:
        logger.debug("SQLite is empty, rebuilding from sidecar metadata")
        for key in sorted(storage.list_objects(prefix="")):
            if not key.endswith(".meta.json"):
                continue
            metadata = _parse_file_metadata(storage.get_object(key))
            if metadata:
                db.upsert(FileRecord.from_metadata(metadata))

    records = db.list_files(
        **filters,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=25,
    )
    response.headers["X-Total-Count"] = str(db.count_files(**filters))
    response.headers["X-Status-Counts"] = json.dumps(db.get_status_counts())
    logger.debug("API GET /files: returning %s records; filtered_total=%s; status_counts=%s", len(records), response.headers["X-Total-Count"], response.headers["X-Status-Counts"])

    return [
        FileListItem(
            id=record.id,
            status=record.status,
            current_key=record.current_key,
            original_key=record.original_key,
            template_name=record.template_name,
            period_format=record.period_format,
            document_type=record.document_type,
            emitting_company=record.emitting_company,
            period=record.period,
            emission_date=record.emission_date,
            confidence=record.confidence,
            updated_at=record.updated_at,
            optional_fields=dict(record.optional_fields or {}),
        )
        for record in records
    ]


@router.get("/files/{file_id}", response_model=FileDetail)
async def get_file(
    request: Request,
    file_id: str,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> FileDetail:
    """Return metadata and extracted text for a single file."""
    logger.debug("API GET /files/%s: loading document metadata", file_id)
    storage = _storage(request)
    db = _db(request)
    record = db.get_by_id(file_id)

    if record is None:
        logger.debug(f"File '{file_id}' not found in SQLite, searching storage sidecars")
        keys = storage.list_objects(prefix="")
        for key in keys:
            if not key.endswith(".meta.json"):
                continue

            metadata = _parse_file_metadata(storage.get_object(key))
            if metadata and (metadata.get("sha256") == file_id or key == file_id):
                record = FileRecord.from_metadata(metadata)
                db.upsert(record)
                break

    if record is None:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found")

    sidecar_metadata: dict[str, Any] = {}
    if record.current_key:
        metadata_key = f"{record.current_key.split('.')[0]}.meta.json" if record.current_key.endswith(".pdf") else f"{record.current_key}.meta.json"
        sidecar_metadata = _parse_file_metadata(
            storage.get_object(metadata_key)
        )
    if not sidecar_metadata or sidecar_metadata.get("sha256") not in {
        record.sha256,
        record.id,
    }:
        for key in storage.list_objects(prefix=""):
            if not key.endswith(".meta.json"):
                continue
            candidate = _parse_file_metadata(storage.get_object(key))
            if candidate.get("sha256") in {record.sha256, record.id}:
                sidecar_metadata = candidate
                break

    metadata = {
        "schema_version": sidecar_metadata.get("schema_version"),
        "sha256": record.sha256,
        "status": record.status,
        "current_key": record.current_key,
        "original_key": record.original_key,
        "template_name": record.template_name,
        "period_format": record.period_format,
        "document_type": record.document_type,
        "emitting_company": record.emitting_company,
        "period": record.period,
        "emission_date": record.emission_date,
        "confidence": record.confidence,
        "updated_at": record.updated_at,
        "created_at": record.created_at,
        "extracted_text": record.extracted_text,
        "optional_fields": record.optional_fields,
        "history": sidecar_metadata.get("events", []),
    }
    item = _as_file_item(record.id, metadata)
    result = FileDetail(
        id=item.id,
        status=item.status,
        current_key=item.current_key,
        original_key=item.original_key,
        template_name=item.template_name,
        period_format=item.period_format,
        document_type=item.document_type,
        emitting_company=item.emitting_company,
        period=item.period,
        emission_date=item.emission_date,
        confidence=item.confidence,
        updated_at=item.updated_at,
        schema_version=metadata["schema_version"],
        created_at=metadata["created_at"],
        extracted_text=record.extracted_text,
        optional_fields=record.optional_fields,
        history=metadata["history"] if isinstance(metadata["history"], list) else [],
    )
    logger.debug("API GET /files/%s: loaded status=%s current_key=%s optional_field_count=%s", file_id, result.status, result.current_key, len(result.optional_fields or {}))
    return result


@router.post("/files/{file_id}/suggestions")
async def get_document_model_suggestions(
    request: Request,
    file_id: str,
    payload: dict[str, Any],
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Return confidence-gated model suggestions without delaying document rendering."""
    db = _db(request)
    record = db.get_by_id(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found")

    registry = _registry(request)
    threshold = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.8"))
    ml = LocalMLPipeline(confidence_threshold=threshold)
    text = record.extracted_text or ""
    company = str(payload.get("emitting_company") or record.emitting_company or "").strip()
    document_type = str(payload.get("document_type") or record.document_type or "").strip()
    current_fields = dict(record.optional_fields or {})
    submitted_fields = payload.get("optional_fields")
    if isinstance(submitted_fields, dict):
        current_fields.update({
            str(name): str(value)
            for name, value in submitted_fields.items()
            if value is not None and str(value).strip()
        })
    suggestions, field_decisions = ml.suggest_document_fields(
        text,
        company,
        document_type,
        current_fields,
        registry.get_additional_fields(),
    )
    field_decisions = {
        **field_decisions,
        "emission_date": "unavailable: no valid configured date or stored date",
        "period": "skipped: missing date or period format",
        "current_key": "skipped: missing template or key pattern",
    }

    # Recommend the first valid date matched in document order by configured regexes.
    # This is deterministic content extraction, not an ML prediction.
    try:
        recommended_date, date_error = DocumentProcessor(
            registry, ml_pipeline=ml
        )._extract_emission_date(text)
        if not recommended_date:
            # The stored date was originally produced by the same configured
            # content-extraction pipeline. Keep it available as a recommendation
            # when the current extraction configuration cannot reproduce it.
            stored_date = str(getattr(record, "emission_date", "") or "").strip()
            if stored_date:
                recommended_date = stored_date
                field_decisions["emission_date"] = "content: using stored date"
        if recommended_date:
            suggestions["emission_date"] = {
                "value": recommended_date,
                "source": "content",
            }
            field_decisions["emission_date"] = f"content: {recommended_date}"
        elif date_error:
            field_decisions["emission_date"] = f"unavailable: {date_error}"
    except Exception as exc:
        stored_date = str(getattr(record, "emission_date", "") or "").strip()
        if stored_date:
            suggestions["emission_date"] = {
                "value": stored_date,
                "source": "content",
            }
            field_decisions["emission_date"] = f"content: using stored date {stored_date}"
        else:
            field_decisions["emission_date"] = f"unavailable: extraction failed ({exc})"

    derived_template = str(payload.get("template_name") or record.template_name or "")
    derived_format = str(payload.get("period_format") or record.period_format or "")

    try:
        raw_date = str(payload.get("emission_date") or record.emission_date or "")
        emission_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        period_config = registry.get_period_format_config(derived_format)
        if period_config is not None:
            calculated_period = format_period_from_emission_date(emission_date, period_config.to_dict())
            suggestions["period"] = {
                "value": calculated_period,
                "source": "derived",
            }
            field_decisions["period"] = f"derived: {calculated_period}"
            key_pattern = registry.get_key_pattern(derived_template)
            if key_pattern:
                optional_fields = payload.get("optional_fields")
                if not isinstance(optional_fields, dict):
                    optional_fields = record.optional_fields or {}
                pattern_values = dict(optional_fields)
                pattern_values.update({
                    "company": company,
                    "document_type": document_type,
                    "emission_date": emission_date.isoformat(),
                    "period": calculated_period,
                    "period_format": derived_format,
                    "template_name": derived_template,
                    "emitting_company": company,
                    "additional_fields": ", ".join(str(value) for value in optional_fields.values()),
                    "original_filename": (record.original_key or "").rsplit("/", 1)[-1],
                })
                for _ in range(20):
                    try:
                        calculated_key = key_pattern.format(**pattern_values).strip("/")
                        break
                    except KeyError as error:
                        pattern_values[str(error.args[0])] = "unknown"
                else:
                    calculated_key = ""
                if calculated_key:
                    if not calculated_key.startswith("files/"):
                        calculated_key = f"files/{calculated_key}"
                    if not calculated_key.lower().endswith(".pdf"):
                        calculated_key = f"{calculated_key}.pdf"
                    suggestions["current_key"] = {
                        "value": calculated_key,
                        "source": "derived",
                    }
                    field_decisions["current_key"] = f"derived: {calculated_key}"
                else:
                    field_decisions["current_key"] = "unavailable: key pattern could not be formatted"
            else:
                field_decisions["current_key"] = "skipped: template has no key pattern"
        else:
            field_decisions["period"] = f"unavailable: unknown period format {derived_format!r}"
    except (TypeError, ValueError) as exc:
        field_decisions["period"] = f"unavailable: {exc}"

    logger.debug(
        "API POST /files/%s/suggestions decisions=%s",
        file_id,
        field_decisions,
    )
    return {"suggestions": suggestions, "confidence_threshold": threshold}


@router.get("/files/{file_id}/pdf")
async def get_file_pdf(
    request: Request,
    file_id: str,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> Response:
    """Stream a PDF by key from storage when present."""
    logger.debug("API GET /files/%s/pdf: resolving PDF storage key", file_id)
    storage = _storage(request)
    db = _db(request)
    record = db.get_by_id(file_id)

    key = record.current_key if record else None
    if key and storage.object_exists(key):
        payload = storage.get_object(key)
        if payload is not None:
            logger.debug(f"Streaming PDF for file_id={file_id} from key={key}")
            return Response(content=payload, media_type="application/pdf")
        raise HTTPException(status_code=404, detail="PDF bytes not found for the file")

    logger.debug(f"PDF for file_id={file_id} not found in SQLite, searching storage sidecars")
    keys = storage.list_objects(prefix="")
    for key in keys:
        if key.endswith(".meta.json"):
            metadata = _parse_file_metadata(storage.get_object(key))
            if metadata and (metadata.get("sha256") == file_id or key == file_id):
                current_key = metadata.get("current_key") or key.replace(".meta.json", "")
                pdf_keys = [current_key]
                if not current_key.lower().endswith(".pdf"):
                    pdf_keys.append(f"{current_key}.pdf")
                for pdf_key in pdf_keys:
                    payload = storage.get_object(pdf_key)
                    if payload is not None:
                        return Response(content=payload, media_type="application/pdf")
                raise HTTPException(status_code=404, detail="PDF bytes not found for the file")

    raise HTTPException(status_code=404, detail=f"PDF for '{file_id}' not found")


@router.post("/files/{file_id}/edit", response_model=FileDetail)
async def edit_file(
    request: Request,
    file_id: str,
    payload: DocumentEditRequest,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> FileDetail:
    """Recalculate and persist user-edited document metadata and its storage key."""
    logger.debug("API POST /files/%s/edit: template=%s period_format=%s company=%s document_type=%s optional_fields=%s", file_id, payload.template_name, payload.period_format, payload.emitting_company, payload.document_type, sorted((payload.optional_fields or {}).keys()))
    storage = _storage(request)
    db = _db(request)
    registry = _registry(request)
    record = db.get_by_id(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found")

    template_name = payload.template_name.strip()
    period_format = payload.period_format.strip()
    emitting_company = payload.emitting_company.strip()
    document_type = payload.document_type.strip()
    if not emitting_company or not document_type:
        raise HTTPException(status_code=422, detail="Company and document type are required")

    if registry.get_template(template_name) is None:
        raise HTTPException(status_code=422, detail=f"Unknown template '{template_name}'")
    period_config = registry.get_period_format_config(period_format)
    if period_config is None:
        raise HTTPException(status_code=422, detail=f"Unknown period format '{period_format}'")

    emission_date = payload.emission_date.isoformat()
    try:
        period = format_period_from_emission_date(
            payload.emission_date,
            period_config.to_dict(),
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=f"Unable to calculate period: {error}") from error

    updated_optional_fields = {
        str(label).strip(): value
        for label, value in (payload.optional_fields or {}).items()
        if str(label).strip()
    }

    key_pattern = registry.get_key_pattern(template_name)
    if not key_pattern:
        raise HTTPException(status_code=422, detail=f"Template '{template_name}' has no key pattern")
    pattern_values = dict(updated_optional_fields)
    pattern_values.update({
        "company": emitting_company,
        "document_type": document_type,
        "emission_date": emission_date,
        "period": period,
        "period_format": period_format,
        "template_name": template_name,
        "emitting_company": emitting_company,
        "additional_fields": ", ".join(updated_optional_fields.values()),
        "original_filename": record.original_key.rsplit("/", 1)[-1],
    })
    for _ in range(20):
        try:
            formatted_key = key_pattern.format(**pattern_values).strip("/")
            break
        except KeyError as error:
            pattern_values[str(error.args[0])] = "unknown"
    else:
        raise HTTPException(status_code=422, detail="Template key pattern has too many unresolved fields")

    if not formatted_key or any(part in {".", ".."} for part in formatted_key.split("/")):
        raise HTTPException(status_code=422, detail="Template produced an invalid document key")
    new_current_key = formatted_key if formatted_key.startswith("files/") else f"files/{formatted_key}"
    new_pdf_key = new_current_key if new_current_key.lower().endswith(".pdf") else f"{new_current_key}.pdf"
    new_sidecar_key = f"{new_current_key}.meta.json"

    source_sidecar_key = ""
    sidecar_metadata: dict[str, Any] = {}
    known_sidecar_key = f"{record.current_key.split('.')[0]}.meta.json" if record.current_key.endswith(".pdf") else f"{record.current_key}.meta.json"
    if known_sidecar_key:
        candidate = _parse_file_metadata(storage.get_object(known_sidecar_key))
        if candidate and candidate.get("sha256") in {record.sha256, record.id}:
            source_sidecar_key = known_sidecar_key
            sidecar_metadata = candidate
    if not source_sidecar_key:
        for key in storage.list_objects(prefix=""):
            if not key.endswith(".meta.json"):
                continue
            candidate = _parse_file_metadata(storage.get_object(key))
            if candidate and (
                candidate.get("sha256") in {record.sha256, record.id}
                or candidate.get("current_key") == record.current_key
            ):
                source_sidecar_key = key
                sidecar_metadata = candidate
                break
    if not source_sidecar_key:
        raise HTTPException(status_code=404, detail="Document sidecar was not found")

    source_current_key = str(sidecar_metadata.get("current_key") or record.current_key)
    source_pdf_key = ""
    pdf_candidates = [source_current_key]
    if source_current_key and not source_current_key.lower().endswith(".pdf"):
        pdf_candidates.append(f"{source_current_key}.pdf")
    for candidate in pdf_candidates:
        if candidate and storage.object_exists(candidate):
            source_pdf_key = candidate
            break

    if storage.object_exists(new_pdf_key) and new_pdf_key != source_pdf_key:
        raise HTTPException(status_code=409, detail="The recalculated PDF key is already in use")
    destination_sidecar = _parse_file_metadata(storage.get_object(new_sidecar_key))
    if (
        destination_sidecar
        and new_sidecar_key != source_sidecar_key
        and destination_sidecar.get("sha256") not in {record.sha256, record.id}
    ):
        raise HTTPException(status_code=409, detail="The recalculated sidecar key is already in use")

    updated_at = datetime.now(timezone.utc).isoformat()
    old_values = {}
    new_values = {}
    if template_name != record.template_name:
        old_values["template_name"] = record.template_name
        new_values["template_name"] = template_name
    if period_format != record.period_format:
        old_values["period_format"] = record.period_format
        new_values["period_format"] = period_format
    if emitting_company != record.emitting_company:
        old_values["emitting_company"] = record.emitting_company
        new_values["emitting_company"] = emitting_company
    if document_type != record.document_type:
        old_values["document_type"] = record.document_type
        new_values["document_type"] = document_type
    if emission_date != record.emission_date:
        old_values["emission_date"] = record.emission_date
        new_values["emission_date"] = emission_date
    if period != record.period:
        old_values["period"] = record.period
        new_values["period"] = period
    if new_pdf_key != record.current_key:
        old_values["current_key"] = record.current_key
        new_values["current_key"] = new_pdf_key
    if updated_optional_fields != record.optional_fields:
        old_values["optional_fields"] = dict(record.optional_fields)
        new_values["optional_fields"] = dict(updated_optional_fields)
    sidecar_metadata.update(new_values)
    sidecar_metadata["optional_fields"] = updated_optional_fields
    sidecar_metadata["status"] = "validated"
    sidecar_metadata["updated_at"] = updated_at
    events = sidecar_metadata.get("events")
    if not isinstance(events, list):
        events = []
        sidecar_metadata["events"] = events
    events.append({
        "type": "corrected",
        "timestamp": updated_at,
        "payload": {"before": old_values, "after": new_values},
    })

    copied_pdf = bool(source_pdf_key and source_pdf_key != new_pdf_key)
    if copied_pdf:
        storage.copy_object(source_pdf_key, new_pdf_key)
    try:
        storage.put_object(new_sidecar_key, json.dumps(sidecar_metadata, ensure_ascii=False, indent=2))
    except Exception:
        if copied_pdf:
            storage.delete_object(new_pdf_key)
        raise

    record.template_name = template_name
    record.period_format = period_format
    record.emitting_company = emitting_company
    record.document_type = document_type
    record.emission_date = emission_date
    record.period = period
    record.current_key = new_pdf_key
    record.optional_fields = updated_optional_fields
    record.status = "validated"
    record.updated_at = updated_at
    db.upsert(record)

    if source_sidecar_key != new_sidecar_key:
        storage.delete_object(source_sidecar_key)
    if source_pdf_key and source_pdf_key != new_pdf_key:
        storage.delete_object(source_pdf_key)

    return await get_file(request, record.id, current_user=current_user)


@router.post("/files/bulk-edit")
async def bulk_edit_files(
    request: Request,
    payload: BulkEditRequest,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Apply company, document-type, and additional-field edits to selected files."""
    db = _db(request)
    if not payload.changes:
        raise HTTPException(status_code=422, detail="At least one explicit change is required")
    allowed = {"emitting_company", "document_type", "additional_fields"}
    unknown = set(payload.changes) - allowed
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unsupported bulk fields: {', '.join(sorted(unknown))}")
    if payload.all_matching:
        if payload.ids:
            raise HTTPException(status_code=422, detail="Specify ids or all_matching, not both")
        filters = {key: value for key, value in payload.filters.items() if key in {
            "status", "template", "period_format", "q", "current_key", "emitting_company",
            "document_type", "period", "confidence", "updated_at", "emission_date"
        }}
        additional_fields = {
            key.removeprefix("additional_field__"): value
            for key, value in payload.filters.items()
            if key.startswith("additional_field__") and value
        }
        records = db.list_files(**filters, additional_fields=additional_fields, page=1, page_size=100000)
    else:
        if not payload.ids:
            raise HTTPException(status_code=422, detail="Select at least one document")
        records = [record for file_id in dict.fromkeys(payload.ids) if (record := db.get_by_id(file_id)) is not None]
    if not records:
        raise HTTPException(status_code=404, detail="No matching documents found")

    results: list[dict[str, Any]] = []
    for record in records:
        try:
            if not record.template_name or not record.period_format or not record.emission_date:
                raise HTTPException(status_code=422, detail="Document is missing template, period format, or emission date")
            optional_fields = dict(record.optional_fields or {})
            field_changes = payload.changes.get("additional_fields", {})
            if field_changes is not None:
                if not isinstance(field_changes, dict):
                    raise HTTPException(status_code=422, detail="additional_fields must be an object")
                for name, value in field_changes.items():
                    if not str(name).strip():
                        raise HTTPException(status_code=422, detail="Additional-field names cannot be empty")
                    if value is None or value == {"operation": "clear"}:
                        optional_fields.pop(str(name), None)
                    elif isinstance(value, dict) and value.get("operation") == "set":
                        optional_fields[str(name)] = value.get("value", "")
                    else:
                        optional_fields[str(name)] = value
            edit_payload = DocumentEditRequest(
                template_name=record.template_name,
                period_format=record.period_format,
                emitting_company=str(payload.changes.get("emitting_company", record.emitting_company or "")),
                document_type=str(payload.changes.get("document_type", record.document_type or "")),
                emission_date=record.emission_date,
                optional_fields=optional_fields,
            )
            updated = await edit_file(request, record.id, edit_payload, current_user=current_user)
            results.append({"id": record.id, "success": True, "status": updated.status, "current_key": updated.current_key})
        except HTTPException as error:
            results.append({"id": record.id, "success": False, "status_code": error.status_code, "error": error.detail})
        except Exception as error:
            logger.exception("Bulk edit failed for file %s", record.id)
            results.append({"id": record.id, "success": False, "status_code": 500, "error": str(error)})

    succeeded = sum(1 for item in results if item["success"])
    return {"requested": len(results), "succeeded": succeeded, "failed": len(results) - succeeded, "results": results}


@router.post("/files/{file_id}/validate", response_model=ValidationResponse)
async def validate_file(
    request: Request,
    file_id: str,
    payload: ValidationRequest,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> ValidationResponse:
    """Apply a manual validation update and persist it back to the sidecar metadata."""
    storage = _storage(request)
    db = _db(request)
    record = db.get_by_id(file_id)

    if record is None:
        logger.debug(f"File '{file_id}' not found in SQLite, searching storage sidecars")
        keys = storage.list_objects(prefix="")
        for key in keys:
            if key.endswith(".meta.json"):
                metadata = _parse_file_metadata(storage.get_object(key))
                if metadata and (metadata.get("sha256") == file_id or key == file_id):
                    record = FileRecord.from_metadata(metadata)
                    db.upsert(record)
                    break

    if record is None:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found")

    metadata = dict(record.optional_fields)
    if payload.template_id:
        record.template_name = payload.template_id
    if payload.fields:
        record.optional_fields = {**record.optional_fields, **payload.fields}
        metadata = record.optional_fields

    record.status = "validated"
    record.updated_at = "now"
    db.upsert(record)

    sidecar_key = f"{record.current_key}.meta.json" if record.current_key else f"{file_id}.meta.json"
    storage.put_object(sidecar_key, json.dumps({
        "sha256": record.sha256,
        "original_key": record.original_key,
        "current_key": record.current_key,
        "status": record.status,
        "template_name": record.template_name,
        "period_format": record.period_format,
        "document_type": record.document_type,
        "emitting_company": record.emitting_company,
        "period": record.period,
        "emission_date": record.emission_date,
        "confidence": record.confidence,
        "optional_fields": record.optional_fields,
        "extracted_text": record.extracted_text,
        "updated_at": record.updated_at,
    }, ensure_ascii=False, indent=2))

    return ValidationResponse(
        id=str(record.sha256 or file_id),
        status="validated",
        current_key=record.current_key,
        template_name=record.template_name,
        fields=record.optional_fields,
        updated=True,
    )


@router.get("/document-options")
async def document_edit_options(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Return configured templates and existing document values for edit suggestions."""
    logger.debug("API GET /document-options: loading template and field options")
    registry = _registry(request)
    db = _db(request)
    records = db.list_files(page_size=10000)

    companies = {
        str(company).strip()
        for company in registry.get_companies()
        if str(company).strip()
    } | {
        str(record.emitting_company).strip()
        for record in records
        if record.emitting_company and str(record.emitting_company).strip()
    }
    document_types = {
        str(document_type).strip()
        for document_type in registry.get_document_types()
        if str(document_type).strip()
    } | {
        str(record.document_type).strip()
        for record in records
        if record.document_type and str(record.document_type).strip()
    }
    optional_field_values_by_name: dict[str, set[str]] = {}
    for name, values in registry.get_additional_fields().items():
        field_name = str(name).strip()
        if field_name:
            optional_field_values_by_name[field_name] = {
                str(value).strip()
                for value in values
                if value is not None and str(value).strip()
            }
    optional_field_names = set(optional_field_values_by_name)
    for record in records:
        for name, value in (record.optional_fields or {}).items():
            field_name = str(name).strip()
            field_value = str(value).strip() if value is not None else ""
            if field_name:
                optional_field_names.add(field_name)
                if field_value:
                    optional_field_values_by_name.setdefault(field_name, set()).add(field_value)
    optional_field_values = {
        name: sorted(values)
        for name, values in sorted(optional_field_values_by_name.items())
    }

    return {
        "templates": registry.list_all_templates(),
        "period_formats": registry.list_period_formats(),
        "companies": sorted(companies),
        "document_types": sorted(document_types),
        "optional_field_names": sorted(optional_field_names),
        "optional_field_values": optional_field_values,
    }


@router.get("/config")
async def get_config(
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, str]:
    """Return the configured JSONC file for editing in the dashboard."""
    path = _template_config_path()
    try:
        return {"content": path.read_text(encoding="utf-8")}
    except OSError as error:
        raise HTTPException(status_code=500, detail="Unable to read the configured template file.") from error


@router.put("/config")
async def update_config(
    payload: ConfigUpdateRequest,
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, bool]:
    """Validate and save JSONC configuration, then activate it without restarting the process."""
    registry = TemplateRegistry()
    try:
        registry.load_from_content(payload.content)
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError, KeyError, re.error) as error:
        raise HTTPException(status_code=422, detail=f"Invalid configuration: {error}") from error

    path = _template_config_path()
    try:
        with path.open("w", encoding="utf-8") as config_file:
            config_file.write(payload.content)
            config_file.flush()
            os.fsync(config_file.fileno())
        request.app.state.template_registry = registry
    except OSError as error:
        raise HTTPException(status_code=500, detail="Unable to save the configured template file.") from error

    return {"saved": True, "reloaded": True}


@router.get("/templates")
async def list_templates(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List all templates configured for the service."""
    registry = _registry(request)
    items: list[dict[str, Any]] = []
    for template_name in registry.list_all_templates():
        template = registry.get_template(template_name)
        if template is None:
            continue
        items.append({
            "name": template.name,
            "key_pattern": template.key_pattern,
        })
    return items


@router.get("/models")
async def get_models(
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Report whether each model in the current complete set is available."""
    result = list_models()
    logger.debug("API GET /models: complete=%s", result.get("complete"))
    return result


@router.post("/models/train", status_code=202)
async def train_models(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Start one background job that replaces the complete model set."""
    jobs = getattr(request.app.state, "model_training_jobs", None)
    if jobs is None:
        jobs = {}
        request.app.state.model_training_jobs = jobs
    if any(job.get("status") == "running" for job in jobs.values()):
        raise HTTPException(status_code=409, detail="A full model retraining job is already running.")

    records = _db(request).list_files(status="validated", page=1, page_size=100000)
    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "progress": 0,
        "stage": "Starting model training",
        "message": "Preparing validated documents",
    }

    def update_progress(progress: int, message: str) -> None:
        jobs[job_id].update({
            "progress": max(0, min(100, int(progress))),
            "stage": message,
            "message": message,
        })

    def run_training() -> None:
        try:
            result = train_all_models(records, progress_callback=update_progress)
            jobs[job_id].update({
                "status": "completed",
                "progress": 100,
                "stage": "Completed",
                "message": "All models have been retrained and replaced.",
                "result": result,
            })
        except Exception as error:
            logger.exception("Full model retraining job %s failed", job_id)
            jobs[job_id].update({
                "status": "failed",
                "stage": "Failed",
                "message": str(error),
            })

    threading.Thread(target=run_training, name=f"model-training-{job_id[:8]}", daemon=True).start()
    logger.debug("API POST /models/train: started full retraining job %s", job_id)
    return {"job_id": job_id, "status": "running"}


@router.get("/jobs/{job_id}")
async def get_job_status(
    request: Request,
    job_id: str,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Return current progress for a background model-training job."""
    jobs = getattr(request.app.state, "model_training_jobs", {})
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return dict(job)


@router.get("/consistency")
async def list_consistency_flags(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return consistency anomalies. In the current implementation, this is a lightweight placeholder."""
    logger.debug("API GET /consistency: scanning storage and database consistency")
    storage = _storage(request)
    db = _db(request)
    anomalies: list[dict[str, Any]] = []
    for key in storage.list_objects(prefix=""):
        if key.endswith(".meta.json"):
            metadata = _parse_file_metadata(storage.get_object(key))
            if metadata and metadata.get("status") in {"processed", "validated"}:
                current_key = metadata.get("current_key", key.replace(".meta.json", ""))
                if not storage.object_exists(current_key):
                    anomalies.append({
                        "kind": "missing_object",
                        "key": key,
                        "message": "Sidecar points to an object that is no longer present.",
                    })
    for record in db.list_files():
        if record.status in {"processed", "validated"} and not storage.object_exists(record.current_key):
            anomalies.append({
                "kind": "missing_object",
                "key": record.current_key,
                "message": "SQLite record points to a missing object.",
            })
    return anomalies


@router.post("/consistency/rebuild")
async def rebuild_consistency(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Rebuild SQLite from sidecar metadata and return the completed result."""
    storage = _storage(request)
    db = _db(request)
    rebuilt = db.rebuild_from_sidecars(storage)
    request.app.state.last_rebuild = {
        "status": "completed",
        "message": "SQLite rebuilt from sidecar metadata.",
        "count": len(rebuilt),
    }
    return request.app.state.last_rebuild
