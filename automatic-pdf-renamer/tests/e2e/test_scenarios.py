from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from conftest import request_json


def _sidecar(storage: Path, key: str) -> dict[str, Any]:
    return json.loads((storage / key).with_suffix(".meta.json").read_text(encoding="utf-8"))


def _database_rows(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM files ORDER BY sha256")]


def _filesystem_snapshot(storage: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(storage)): path.read_bytes()
        for path in storage.rglob("*") if path.is_file()
    }


def _assert_baseline(service: dict[str, Any], *, expected_count: int) -> None:
    rows = _database_rows(service["db_path"])
    assert len(rows) == expected_count
    assert {row["sha256"] for row in rows} == ({"e" * 64, "a" * 64} if expected_count else set())
    assert _sidecar(service["storage"], "inbox/empty.pdf")["sha256"] == "e" * 64
    assert _sidecar(service["storage"], "pending/valid.pdf")["sha256"] == "a" * 64


def test_rebuild_and_read_only_requests(e2e_service: dict[str, Any]) -> None:
    """Run an ordered action list and check persisted state after each action."""
    service = e2e_service
    base = service["base_url"]
    storage = service["storage"]
    original_files = _filesystem_snapshot(storage)

    # Action 1: rebuild SQLite from authoritative sidecars.
    status, result, _ = request_json(base, "POST", "/consistency/rebuild")
    assert status == 200, result
    assert result["status"] == "completed"
    assert result["count"] == 2
    _assert_baseline(service, expected_count=2)
    assert _filesystem_snapshot(storage) == original_files

    # Action 2: list documents.
    status, items, headers = request_json(base, "GET", "/files")
    assert status == 200, items
    assert len(items) == 2
    assert headers.get("X-Total-Count") == "2"
    _assert_baseline(service, expected_count=2)
    assert _filesystem_snapshot(storage) == original_files

    # Action 3: read document detail, including extracted text.
    status, detail, _ = request_json(base, "GET", f"/files/{'a' * 64}")
    assert status == 200, detail
    assert detail["current_key"] == "pending/valid.pdf"
    assert detail["extracted_text"] == "E2E sample document 2026-09-20"
    _assert_baseline(service, expected_count=2)
    assert _filesystem_snapshot(storage) == original_files


def test_missing_document_returns_404_without_mutation(e2e_service: dict[str, Any]) -> None:
    service = e2e_service
    request_status, result, _ = request_json(service["base_url"], "POST", "/consistency/rebuild")
    assert request_status == 200, result
    before = _filesystem_snapshot(service["storage"])

    status, body, _ = request_json(service["base_url"], "GET", f"/files/{'f' * 64}")
    assert status == 404, body
    _assert_baseline(service, expected_count=2)
    assert _filesystem_snapshot(service["storage"]) == before


def test_pdf_endpoint_streams_original_bytes(e2e_service: dict[str, Any]) -> None:
    import urllib.request

    service = e2e_service
    request_status, result, _ = request_json(service["base_url"], "POST", "/consistency/rebuild")
    assert request_status == 200, result
    original_pdf = (service["storage"] / "pending/valid.pdf").read_bytes()
    before = _filesystem_snapshot(service["storage"])

    with urllib.request.urlopen(
        service["base_url"] + f"/files/{'a' * 64}/pdf", timeout=10
    ) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "application/pdf"
        assert response.read() == original_pdf

    _assert_baseline(service, expected_count=2)
    assert _filesystem_snapshot(service["storage"]) == before
