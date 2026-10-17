# Docker-based end-to-end tests

The E2E suite starts the real API in a disposable Docker container using the existing `FileStorage` backend. It does not start the dashboard, connect to S3/Garage, or enable background workers.

## Requirements

- Docker Engine with the `docker` CLI
- Python 3.11+
- `pytest` (install with `python -m pip install -r tests/e2e/requirements.txt`)

Run from `automatic-pdf-renamer/`:

```sh
python -m pip install -r tests/e2e/requirements.txt
pytest -q tests/e2e
```

The suite builds the application image once per pytest session. Each test creates a baseline fixture tree (PDFs plus `.meta.json` sidecars) in its own temporary workspace, starts a new container with a new SQLite database, rebuilds SQLite from sidecars, and executes ordered HTTP actions. After each action, assertions can inspect the API, the host-mounted filesystem, and SQLite.

Set `E2E_DOCKER_IMAGE` to override the temporary image tag. Set `E2E_STARTUP_TIMEOUT` to adjust the bounded startup wait. The suite uses a dynamically allocated localhost port and removes each container and temporary workspace when finished.

Fixtures are immutable inputs. The baseline includes an empty PDF and a generated valid one-page PDF with deterministic sidecars. PDF generation exists only to keep the fixture small and readable; tests exercise the running service, not a test-only in-process app.

## Safety

The suite always overrides storage and SQLite paths with paths inside its temporary workspace, and sets `APP_STORAGE_BACKEND=file`. It does not accept a production base URL or reuse a developer database. Do not modify the fixture source during a run.
