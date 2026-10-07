from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.config import load_templates
from app.db import FileRecord, SQLiteFileRepository
from app.period import format_period_from_emission_date
from app.storage import StorageBackend

from .deps import get_current_user
from .models import (
    FileDetail,
    FileListItem,
    DocumentEditRequest,
    HealthResponse,
    ValidationRequest,
    ValidationResponse,
)

router = APIRouter(tags=["files"])


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
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Service health checkpoint."""
    return HealthResponse(status="ok", app="automatic-pdf-renamer")


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
    sort_by: str = "updated_at",
    sort_order: str = "desc",
    page: int = 1,
) -> list[FileListItem]:
    """List documents and report filtered and global counts in response headers."""
    storage = _storage(request)
    db = _db(request)
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
    }

    if db.count_files() == 0:
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
    storage = _storage(request)
    db = _db(request)
    record = db.get_by_id(file_id)

    if record is None:
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
        sidecar_metadata = _parse_file_metadata(
            storage.get_object(f"{record.current_key}.meta.json")
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
    return FileDetail(
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


@router.get("/files/{file_id}/pdf")
async def get_file_pdf(
    request: Request,
    file_id: str,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> Response:
    """Stream a PDF by key from storage when present."""
    storage = _storage(request)
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
    known_sidecar_key = f"{record.current_key}.meta.json" if record.current_key else ""
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


@router.get("/api/document-options")
async def document_edit_options(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, list[str]]:
    """Return configured templates and existing document values for edit suggestions."""
    registry = _registry(request)
    db = _db(request)
    records = db.list_files(page_size=10000)

    companies = sorted({
        str(record.emitting_company).strip()
        for record in records
        if record.emitting_company and str(record.emitting_company).strip()
    })
    document_types = sorted({
        str(record.document_type).strip()
        for record in records
        if record.document_type and str(record.document_type).strip()
    })
    optional_field_names = sorted({
        str(name).strip()
        for record in records
        for name in (record.optional_fields or {}).keys()
        if str(name).strip()
    })
    optional_field_values = sorted({
        str(value).strip()
        for record in records
        for value in (record.optional_fields or {}).values()
        if value is not None and str(value).strip()
    })

    return {
        "templates": registry.list_all_templates(),
        "period_formats": registry.list_period_formats(),
        "companies": companies,
        "document_types": document_types,
        "optional_field_names": optional_field_names,
        "optional_field_values": optional_field_values,
    }


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


@router.post("/retrain")
async def start_retrain(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Start a retraining job. The current implementation records a job stub and returns its status."""
    request.app.state.last_retrain_job = {
        "job_id": "retrain-1",
        "status": "queued",
        "message": "Retrain job accepted. Replace with a subprocess-backed job in production.",
    }
    return request.app.state.last_retrain_job


@router.get("/retrain/status")
async def get_retrain_status(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Read the active retraining status."""
    job = getattr(request.app.state, "last_retrain_job", {"job_id": "retrain-1", "status": "idle"})
    return job


@router.post("/retrain/{job_id}/activate")
async def activate_model(
    job_id: str,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Activate a completed training model."""
    return {"job_id": job_id, "status": "activated", "message": "Model activation accepted."}


@router.get("/consistency")
async def list_consistency_flags(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return consistency anomalies. In the current implementation, this is a lightweight placeholder."""
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


@router.get("/api/health")
async def api_health() -> HealthResponse:
    """Alias for /health."""
    return await health()


@router.get("/api/files", response_model=list[FileListItem])
async def list_files_api(
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
    sort_by: str = "updated_at",
    sort_order: str = "desc",
    page: int = 1,
) -> list[FileListItem]:
    """Compatibility alias for /files."""
    return await list_files(
        request=request,
        response=response,
        current_user=current_user,
        status=status,
        template=template,
        period_format=period_format,
        q=q,
        current_key=current_key,
        emitting_company=emitting_company,
        document_type=document_type,
        period=period,
        confidence=confidence,
        updated_at=updated_at,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
    )


@router.get("/api/files/{file_id}", response_model=FileDetail)
async def get_file_api(
    request: Request,
    file_id: str,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> FileDetail:
    """Compatibility alias for /files/{file_id}."""
    return await get_file(request=request, file_id=file_id, current_user=current_user)


@router.post("/api/files/{file_id}/edit", response_model=FileDetail)
async def edit_file_api(
    request: Request,
    file_id: str,
    payload: DocumentEditRequest,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> FileDetail:
    """Compatibility alias for POST /files/{file_id}/edit."""
    return await edit_file(
        request=request,
        file_id=file_id,
        payload=payload,
        current_user=current_user,
    )


@router.get("/api/templates")
async def list_templates_api(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Compatibility alias for /templates."""
    return await list_templates(request=request, current_user=current_user)


@router.post("/api/retrain")
async def start_retrain_api(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Compatibility alias for /retrain."""
    return await start_retrain(request=request, current_user=current_user)


@router.get("/api/retrain/status")
async def get_retrain_status_api(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Compatibility alias for /retrain/status."""
    return await get_retrain_status(request=request, current_user=current_user)


@router.get("/api/consistency")
async def list_consistency_flags_api(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Compatibility alias for /consistency."""
    return await list_consistency_flags(request=request, current_user=current_user)


@router.post("/api/consistency/rebuild")
async def rebuild_consistency_api(
    request: Request,
    current_user: dict[str, str] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Compatibility alias for /consistency/rebuild."""
    return await rebuild_consistency(request=request, current_user=current_user)
