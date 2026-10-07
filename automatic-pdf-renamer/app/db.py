from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")


@dataclass
class FileRecord:
    """SQLite-backed record for a document file."""

    id: str
    sha256: str
    original_key: str
    current_key: str
    status: str = "unknown"
    template_name: str | None = None
    period_format: str | None = None
    document_type: str | None = None
    emitting_company: str | None = None
    period: str | None = None
    emission_date: str | None = None
    confidence: float | None = None
    optional_fields: dict[str, Any] = field(default_factory=dict)
    extracted_text: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> "FileRecord":
        record_id = str(metadata.get("sha256") or metadata.get("id") or metadata.get("current_key") or "unknown")
        created_at = metadata.get("created_at") or metadata.get("created") or _utc_now()
        updated_at = metadata.get("updated_at") or created_at
        return cls(
            id=record_id,
            sha256=str(metadata.get("sha256") or record_id),
            original_key=str(metadata.get("original_key") or metadata.get("source_key") or ""),
            current_key=str(metadata.get("current_key") or metadata.get("key") or ""),
            status=str(metadata.get("status") or "unknown"),
            template_name=metadata.get("template_name"),
            period_format=metadata.get("period_format"),
            document_type=metadata.get("document_type"),
            emitting_company=metadata.get("emitting_company"),
            period=metadata.get("period"),
            emission_date=metadata.get("emission_date"),
            confidence=metadata.get("confidence"),
            optional_fields=dict(metadata.get("optional_fields") or {}),
            extracted_text=metadata.get("extracted_text"),
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.id,
            self.sha256,
            self.original_key,
            self.current_key,
            self.status,
            self.template_name,
            self.period_format,
            self.document_type,
            self.emitting_company,
            self.period,
            self.emission_date,
            self.confidence,
            json.dumps(self.optional_fields, ensure_ascii=False),
            self.extracted_text,
            self.created_at or _utc_now(),
            self.updated_at or self.created_at or _utc_now(),
        )

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> "FileRecord":
        (
            id_,
            sha256,
            original_key,
            current_key,
            status,
            template_name,
            period_format,
            document_type,
            emitting_company,
            period,
            emission_date,
            confidence,
            optional_fields,
            extracted_text,
            created_at,
            updated_at,
        ) = row
        try:
            optional_value = json.loads(optional_fields or "{}") if optional_fields else {}
        except json.JSONDecodeError:
            optional_value = {}

        return cls(
            id=str(id_),
            sha256=str(sha256),
            original_key=str(original_key),
            current_key=str(current_key),
            status=str(status),
            template_name=template_name,
            period_format=period_format,
            document_type=document_type,
            emitting_company=emitting_company,
            period=period,
            emission_date=emission_date,
            confidence=float(confidence) if confidence is not None else None,
            optional_fields=dict(optional_value),
            extracted_text=extracted_text,
            created_at=str(created_at),
            updated_at=str(updated_at),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteFileRepository:
    """Repository for storing and querying file metadata in SQLite."""

    def __init__(self, db_path: str | None = None):
        default_path = db_path or os.environ.get("SQLITE_PATH", "./renamer.db")
        self.db_path = str(Path(default_path).resolve())
        self._ensure_parent_dir()
        self.init_db()

    def _ensure_parent_dir(self) -> None:
        directory = Path(self.db_path).parent
        directory.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    original_key TEXT,
                    current_key TEXT,
                    status TEXT,
                    template_name TEXT,
                    period_format TEXT,
                    document_type TEXT,
                    emitting_company TEXT,
                    period TEXT,
                    emission_date TEXT,
                    confidence REAL,
                    optional_fields TEXT,
                    extracted_text TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
            if "period_format" not in columns:
                conn.execute("ALTER TABLE files ADD COLUMN period_format TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_files_status
                ON files(status)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_files_template
                ON files(template_name)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_files_period_format
                ON files(period_format)
                """
            )
            conn.commit()

    def upsert(self, record: FileRecord) -> FileRecord:
        record.updated_at = record.updated_at or _utc_now()
        record.created_at = record.created_at or record.updated_at

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO files (
                    id, sha256, original_key, current_key, status, template_name, period_format,
                    document_type, emitting_company, period, emission_date,
                    confidence, optional_fields, extracted_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    sha256 = excluded.sha256,
                    original_key = excluded.original_key,
                    current_key = excluded.current_key,
                    status = excluded.status,
                    template_name = excluded.template_name,
                    period_format = excluded.period_format,
                    document_type = excluded.document_type,
                    emitting_company = excluded.emitting_company,
                    period = excluded.period,
                    emission_date = excluded.emission_date,
                    confidence = excluded.confidence,
                    optional_fields = excluded.optional_fields,
                    extracted_text = excluded.extracted_text,
                    updated_at = excluded.updated_at
                """,
                record.to_row(),
            )
            conn.commit()
        return record

    def get_by_id(self, file_id: str) -> FileRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE id = ? OR sha256 = ?",
                (file_id, file_id),
            ).fetchone()
        if row is None:
            return None
        return FileRecord.from_row(tuple(row))

    def _file_conditions(
        self,
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
    ) -> tuple[list[str], list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if template:
            conditions.append("LOWER(template_name) LIKE ?")
            params.append(f"%{template.lower()}%")
        if current_key:
            value = f"%{current_key.lower()}%"
            conditions.append("(LOWER(current_key) LIKE ? OR LOWER(template_name) LIKE ?)")
            params.extend([value, value])
        for column, value in (
            ("emitting_company", emitting_company),
            ("document_type", document_type),
            ("period", period),
            ("updated_at", updated_at),
            ("period_format", period_format),
        ):
            if value:
                conditions.append(f"LOWER({column}) LIKE ?")
                params.append(f"%{value.lower()}%")
        if confidence:
            confidence_filter = confidence.strip().removesuffix("%")
            if confidence_filter:
                conditions.append("CAST(ROUND(confidence * 100, 1) AS TEXT) LIKE ?")
                params.append(f"%{confidence_filter.lower()}%")
        if q:
            conditions.append(
                "(LOWER(current_key) LIKE ? OR LOWER(original_key) LIKE ? OR LOWER(template_name) LIKE ?  OR LOWER(document_type) LIKE ? OR LOWER(emitting_company) LIKE ? OR LOWER(period) LIKE ? OR LOWER(emission_date) LIKE ? OR LOWER(period_format) LIKE ?)"
            )
            like_value = f"%{q.lower()}%"
            params.extend([like_value] * 8)

        return conditions, params

    def list_files(
        self,
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
        page_size: int = 25,
    ) -> list[FileRecord]:
        conditions, params = self._file_conditions(
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
        )

        query = "SELECT * FROM files"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        sortable_columns = {
            "status": "status",
            "current_key": "current_key",
            "template_name": "template_name",
            "period_format": "period_format",
            "emitting_company": "emitting_company",
            "document_type": "document_type",
            "period": "period",
            "confidence": "confidence",
            "updated_at": "updated_at",
        }
        order_column = sortable_columns.get(sort_by, "updated_at")
        order_direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        query += f" ORDER BY {order_column} {order_direction} LIMIT ? OFFSET ?"
        params.extend([page_size, (page - 1) * page_size])

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [FileRecord.from_row(tuple(row)) for row in rows]

    def count_files(
        self,
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
    ) -> int:
        conditions, params = self._file_conditions(
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
        )
        query = "SELECT COUNT(*) FROM files"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row[0])

    def get_status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END) AS processed,
                    SUM(CASE WHEN status = 'validated' THEN 1 ELSE 0 END) AS validated,
                    SUM(CASE WHEN status = 'not processed' THEN 1 ELSE 0 END) AS pending
                FROM files
                """
            ).fetchone()
        return {key: int(row[key] or 0) for key in ("total", "processed", "validated", "pending")}

    def delete_all(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM files")
            conn.commit()

    def rebuild_from_sidecars(self, storage: Any) -> list[FileRecord]:
        """Rebuild the SQLite index from JSON sidecar metadata in storage."""
        self.delete_all()
        records: list[FileRecord] = []

        for key in sorted(storage.list_objects(prefix="")):
            if not key.endswith(".meta.json"):
                continue
            raw = storage.get_object(key)
            if raw is None:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                metadata = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not metadata:
                continue
            record = FileRecord.from_metadata(metadata)
            self.upsert(record)
            records.append(record)

        logger.info(f"Rebuilt SQLite index from {len(records)} sidecar metadata files.")

        return records
