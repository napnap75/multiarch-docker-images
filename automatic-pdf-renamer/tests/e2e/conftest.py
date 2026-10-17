from __future__ import annotations

import json
import os
import subprocess
import shutil
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


def _write_baseline(workspace: Path) -> None:
    """Copy checked-in PDF and sidecar fixtures into an isolated test workspace."""
    storage = workspace / "storage"
    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "baseline"
    if not fixture_dir.is_dir():
        raise FileNotFoundError(f"E2E fixture directory does not exist: {fixture_dir}")
    shutil.copytree(fixture_dir, storage)


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
