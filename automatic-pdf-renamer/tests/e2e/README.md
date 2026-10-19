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

Fixtures are checked-in immutable inputs under `tests/e2e/fixtures/baseline/`. The baseline includes an empty PDF and a valid one-page PDF, each with its matching `.meta.json` sidecar. Each test copies these files into its own temporary workspace; tests do not generate PDF or sidecar contents in Python.

## Safety

The suite always overrides storage and SQLite paths with paths inside its temporary workspace, and sets `APP_STORAGE_BACKEND=file`. It does not accept a production base URL or reuse a developer database. Do not modify the fixture source during a run.

## Additional E2E scenarios: configurable columns and bulk operations

The E2E suite should add API-level scenarios for the bulk-edit contract using isolated local storage and disposable SQLite state. The browser column chooser and row-selection interactions require frontend/browser tests; they are not covered by API-only E2E tests.

Required scenarios:
- List records with last_modified and verify it is distinct from emission_date.
- Bulk-edit company/type and an additional field; inspect sidecars and SQLite after completion.
- Select all matching filters across pagination and prove nonmatching records remain unchanged.
- Verify validation errors and per-document partial outcomes.
- Force a destination collision and a storage failure; verify no source object is lost and sidecar/SQLite state remains recoverable.
- Retry a partially successful request using the same idempotency token and verify duplicate mutations are not introduced.
