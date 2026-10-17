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
    assert {row["sha256"] for row in rows} == ({"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "b6c81b0f56ce2237b8d121f1066ed699fc32cd18a587e337cb7273d63e862fa4"} if expected_count else set())
    assert _sidecar(service["storage"], "inbox/empty.pdf")["sha256"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert _sidecar(service["storage"], "pending/valid.pdf")["sha256"] == "b6c81b0f56ce2237b8d121f1066ed699fc32cd18a587e337cb7273d63e862fa4"


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
    assert {key.lower(): value for key, value in headers.items()}.get("x-total-count") == "2"
    _assert_baseline(service, expected_count=2)
    assert _filesystem_snapshot(storage) == original_files

    # Action 3: read document detail, including extracted text.
    status, detail, _ = request_json(base, "GET", f"/files/b6c81b0f56ce2237b8d121f1066ed699fc32cd18a587e337cb7273d63e862fa4")
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
        service["base_url"] + f"/files/b6c81b0f56ce2237b8d121f1066ed699fc32cd18a587e337cb7273d63e862fa4/pdf", timeout=10
    ) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "application/pdf"
        assert response.read() == original_pdf

    _assert_baseline(service, expected_count=2)
    assert _filesystem_snapshot(service["storage"]) == before

def test_file_list_filters_and_search(e2e_service: dict[str, Any]) -> None:
    service = e2e_service
    base = service["base_url"]
    status_code, rebuilt, _ = request_json(base, "POST", "/consistency/rebuild")
    assert status_code == 200, rebuilt

    status_code, processed, headers = request_json(base, "GET", "/files?status=processed")
    assert status_code == 200, processed
    assert len(processed) == 1
    assert processed[0]["id"] == "b6c81b0f56ce2237b8d121f1066ed699fc32cd18a587e337cb7273d63e862fa4"
    assert {key.lower(): value for key, value in headers.items()}.get("x-total-count") == "1"

    status_code, by_company, _ = request_json(base, "GET", "/files?emitting_company=EDF")
    assert status_code == 200, by_company
    assert len(by_company) == 1
    assert by_company[0]["emitting_company"] == "EDF"

    status_code, by_text, _ = request_json(base, "GET", "/files?q=E2E%20sample%20document")
    assert status_code == 200, by_text
    assert any(item["id"] == "b6c81b0f56ce2237b8d121f1066ed699fc32cd18a587e337cb7273d63e862fa4" for item in by_text)


def test_document_edit_updates_metadata_and_moves_pdf_safely(e2e_service: dict[str, Any]) -> None:
    service = e2e_service
    base = service["base_url"]
    file_id = "b6c81b0f56ce2237b8d121f1066ed699fc32cd18a587e337cb7273d63e862fa4"
    status_code, rebuilt, _ = request_json(base, "POST", "/consistency/rebuild")
    assert status_code == 200, rebuilt

    source_pdf = service["storage"] / "pending/valid.pdf"
    original_bytes = source_pdf.read_bytes()
    payload = {
        "template_name": "default",
        "period_format": "monthly",
        "emitting_company": "EDF",
        "document_type": "facture",
        "emission_date": "2026-09-20",
        "optional_fields": {"customer_number": "CUST-12345678"},
    }
    status_code, detail, _ = request_json(base, "POST", f"/files/{file_id}/edit", payload)
    assert status_code == 200, detail
    assert detail["status"] == "validated"
    assert detail["emission_date"] == "2026-09-20"
    assert detail["period"] == "Septembre 2026"
    assert detail["current_key"].startswith("files/EDF/Septembre 2026/facture/2026-09-20_valid.pdf")
    assert detail["optional_fields"] == {"customer_number": "CUST-12345678"}

    destination = service["storage"] / detail["current_key"]
    assert destination.read_bytes() == original_bytes
    assert not source_pdf.exists()
    assert not (service["storage"] / "pending/valid.meta.json").exists()

    rows = _database_rows(service["db_path"])
    matching = [row for row in rows if row["sha256"] == file_id]
    assert len(matching) == 1
    assert matching[0]["status"] == "validated"
    assert matching[0]["current_key"] == detail["current_key"]
    assert matching[0]["optional_fields"] == json.dumps({"customer_number": "CUST-12345678"}, separators=(",", ":")) or json.loads(matching[0]["optional_fields"]) == {"customer_number": "CUST-12345678"}

    sidecar_path = service["storage"] / f"{detail['current_key']}.meta.json"
    assert sidecar_path.exists()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["status"] == "validated"
    assert sidecar["optional_fields"] == {"customer_number": "CUST-12345678"}
    assert any(event.get("type") == "corrected" for event in sidecar.get("events", []))


def test_configuration_can_be_read_and_invalid_update_is_rejected(e2e_service: dict[str, Any]) -> None:
    base = e2e_service["base_url"]
    status_code, original, _ = request_json(base, "GET", "/api/config")
    assert status_code == 200, original
    assert isinstance(original.get("content"), str)
    assert '"templates"' in original["content"]

    status_code, error, _ = request_json(base, "PUT", "/api/config", {"content": '{"templates":'})
    assert status_code == 422, error

    status_code, after, _ = request_json(base, "GET", "/api/config")
    assert status_code == 200, after
    assert after["content"] == original["content"]


def test_configuration_valid_update_reloads_without_changing_content(e2e_service: dict[str, Any]) -> None:
    base = e2e_service["base_url"]
    status_code, original, _ = request_json(base, "GET", "/api/config")
    assert status_code == 200, original

    status_code, saved, _ = request_json(base, "PUT", "/api/config", {"content": original["content"]})
    assert status_code == 200, saved
    assert saved == {"saved": True, "reloaded": True}

    status_code, current, _ = request_json(base, "GET", "/api/config")
    assert status_code == 200, current
    assert current["content"] == original["content"]


def test_template_options_are_available_for_dashboard(e2e_service: dict[str, Any]) -> None:
    base = e2e_service["base_url"]
    status_code, templates, _ = request_json(base, "GET", "/templates")
    assert status_code == 200, templates
    assert {template["name"] for template in templates} >= {"default", "edf_invoice", "bank_statement"}

    status_code, options, _ = request_json(base, "GET", "/api/document-options")
    assert status_code == 200, options
    assert "templates" in options
    assert "period_formats" in options
    assert "companies" in options
    assert "document_types" in options

