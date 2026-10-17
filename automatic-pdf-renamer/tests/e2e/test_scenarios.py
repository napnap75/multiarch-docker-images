from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from conftest import request_json


def _sidecar(storage: Path, key: str) -> dict[str, Any]:
    path = storage / key
    sidecar_path = path.with_suffix(".meta.json")
    return json.loads(sidecar_path.read_text(encoding="utf-8"))


def _database_rows(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM files ORDER BY sha256")]


def _assert_rebuild_and_read_scenario(service: dict[str, Any]) -> None:
    """Ordered actions: rebuild, list, detail, then assert all persisted layers."""
    base = service["base_url"]
    storage = service["storage"]
    db_path = service["db_path"]
    before = {
        str(path.relative_to(storage)): path.read_bytes()
        for path in storage.rglob("*") if path.is_file()
    }

    status, result, _ = request_json(base, "POST", "/consistency/rebuild")
    assert status == 200, result
    assert result["status"] == "completed"
    assert result["count"] == 2

    rows = _database_rows(db_path)
    assert len(rows) == 2
    assert {row["sha256"] for row in rows} == {"e" * 64, "a" * 64}
    assert {row["current_key"] for row in rows} == {"inbox/empty.pdf", "pending/valid.pdf"}

    status, items, headers = request_json(base, "GET", "/files")
    assert status == 200, items
    assert len(items) == 2
    assert headers.get("X-Total-Count") == "2"

    status, detail, _ = request_json(base, "GET", f"/files/{'a' * 64}")
    assert status == 200, detail
    assert detail["current_key"] == "pending/valid.pdf"
    assert detail["extracted_text"] == "E2E sample document 2026-09-20"

    # Every action is followed by persisted-state assertions.
    assert _sidecar(storage, "inbox/empty.pdf")["sha256"] == "e" * 64
    assert _sidecar(storage, "pending/valid.pdf")["sha256"] == "a" * 64
    after = {
        str(path.relative_to(storage)): path.read_bytes()
        for path in storage.rglob("*") if path.is_file()
    }
    assert after == before


def test_rebuild_and_read_only_requests(e2e_service: dict[str, Any]) -> None:
    _assert_rebuild_and_read_scenario(e2e_service)


def test_missing_document_returns_404_without_mutation(e2e_service: dict[str, Any]) -> None:
    service = e2e_service
    before = {
        str(path.relative_to(service["storage"])): path.read_bytes()
        for path in service["storage"].rglob("*") if path.is_file()
    }
    status, body, _ = request_json(service["base_url"], "GET", f"/files/{'f' * 64}")
    assert status == 404, body
    after = {
        str(path.relative_to(service["storage"])): path.read_bytes()
        for path in service["storage"].rglob("*") if path.is_file()
    }
    assert after == before


def test_pdf_endpoint_streams_original_bytes(e2e_service: dict[str, Any]) -> None:
    import urllib.request

    service = e2e_service
    with urllib.request.urlopen(
        service["base_url"] + f"/files/{'a' * 64}/pdf", timeout=10
    ) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "application/pdf"
        assert response.read() == (service["storage"] / "pending/valid.pdf").read_bytes()
