from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

APP_DIR = Path(__file__).resolve().parents[2]

def _run(command: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(
        command,
        cwd=APP_DIR,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _pdf_with_text(text: str) -> bytes:
    """Create a small, valid one-page PDF without third-party Python packages."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode("ascii"))
        data.extend(body)
        data.extend(b"\nendobj\n")
    xref_offset = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(data)


def _write_baseline(workspace: Path) -> None:
    """Materialize immutable logical fixtures into the disposable workspace."""
    storage = workspace / "storage"
    storage.mkdir(parents=True)
    fixtures = [
        {
            "filename": "inbox/empty.pdf",
            "sha256": "e" * 64,
            "pdf": b"",
            "text": "",
            "status": "not processed",
        },
        {
            "filename": "pending/valid.pdf",
            "sha256": "a" * 64,
            "pdf": _pdf_with_text("E2E sample document 2026-09-20"),
            "text": "E2E sample document 2026-09-20",
            "status": "processed",
        },
    ]
    for item in fixtures:
        pdf_path = storage / item["filename"]
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(item["pdf"])
        sidecar = {
            "schema_version": "1.0",
            "sha256": item["sha256"],
            "original_key": item["filename"],
            "current_key": item["filename"],
            "status": item["status"],
            "template_name": "default",
            "period_format": "monthly",
            "confidence": 1.0 if item["text"] else 0.0,
            "emission_date": "2026-09-20" if item["text"] else "",
            "period": "September 2026" if item["text"] else "",
            "emitting_company": "EDF" if item["text"] else "",
            "document_type": "facture" if item["text"] else "",
            "optional_fields": {},
            "extracted_text": item["text"],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "events": [],
        }
        (pdf_path.with_suffix(".meta.json")).write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


@pytest.fixture(scope="session")
def docker_image() -> Iterator[str]:
    image = os.environ.get("E2E_DOCKER_IMAGE", f"automatic-pdf-renamer-e2e:{uuid.uuid4().hex[:10]}")
    _run(["docker", "build", "-t", image, "-f", "Dockerfile", "."], timeout=600)
    try:
        yield image
    finally:
        if not os.environ.get("E2E_DOCKER_IMAGE"):
            subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True, text=True)


@pytest.fixture
def e2e_service(tmp_path: Path, docker_image: str) -> Iterator[dict[str, Any]]:
    workspace = tmp_path / "workspace"
    _write_baseline(workspace)
    db_path = workspace / "renamer.db"
    name = f"pdf-renamer-e2e-{uuid.uuid4().hex[:12]}"
    _run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-p", "127.0.0.1::8000",
            "-e", "APP_STORAGE_BACKEND=file",
            "-e", "LOCAL_STORAGE_DIR=/e2e/storage",
            "-e", "SQLITE_PATH=/e2e/renamer.db",
            "-e", "DEBUG=false",
            "-v", f"{workspace.resolve()}:/e2e",
            docker_image,
        ],
        timeout=60,
    )
    try:
        port = _run(["docker", "port", name, "8000/tcp"]).splitlines()[0].rsplit(":", 1)[1]
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + float(os.environ.get("E2E_STARTUP_TIMEOUT", "45"))
        import urllib.error
        import urllib.request

        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout
            raise RuntimeError(f"Application did not become healthy. Container logs:\n{logs}")

        yield {"base_url": base_url, "workspace": workspace, "storage": workspace / "storage", "db_path": db_path}
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True, text=True, timeout=30)


def request_json(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    import urllib.error
    import urllib.request

    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            try:
                content: Any = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                content = raw.decode("utf-8", errors="replace")
            return response.status, content, dict(response.headers.items())
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            content = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            content = raw.decode("utf-8", errors="replace")
        return error.code, content, dict(error.headers.items())
