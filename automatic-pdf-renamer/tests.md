# End-to-End Testing Guide

## Purpose

The end-to-end (E2E) test harness is intended to verify the Automatic PDF Renamer as a running service, through its HTTP API and its persisted state. It should catch regressions that unit tests can miss, such as a successful API response that leaves a stale sidecar, an unexpected file move, or a SQLite index that no longer matches the authoritative metadata.

The harness is designed to test the backend without depending on the web dashboard or a real S3 service. It uses a local filesystem-backed test storage area and disposable test data.

> **Implementation status:** A first Docker-based pytest harness is implemented under `tests/e2e/`. It reuses the existing filesystem storage backend and includes representative rebuild, read-only, missing-document, and PDF-streaming scenarios. The broader scenarios below remain future coverage.

## Test design at a glance

Each test run should follow this sequence:

1. Create a baseline fixture directory containing sample PDF files and their `.meta.json` sidecars.
2. Keep that baseline inside a unique temporary workspace, never in a developer or production data directory.
3. Start a fresh instance of the application configured to use that workspace as its local storage root and a new SQLite database.
4. Call the API's consistency rebuild endpoint and wait for its asynchronous job to finish. This initializes the database from the sidecars rather than from pre-populated database state.
5. Execute the test's ordered list of HTTP actions.
6. After each action, check the HTTP response and assert the expected database and filesystem state.
7. Stop the application and remove the temporary workspace, including the database and any job state.

A test must not depend on state left behind by another test. Each test should be reproducible when run alone or as part of the full suite.

## Fixture directory

The fixture directory is the known starting point for each test. It should contain realistic directory layouts and sidecars, but no SQLite database that the test relies on.

For example, the fixture layout could look like this:

```text
tests/
  fixtures/
    baseline/
      inbox/
        sample-a.pdf
        sample-a.pdf.json
      pending/
        sample-b.pdf
        sample-b.pdf.json
      files/
        sample-c.pdf
        sample-c.pdf.json
```

The exact sidecar filename convention and directory layout must match the application's storage implementation. The names above are illustrative, not a declaration of the current format.

PDF fixtures may be minimal or empty when a scenario only needs to test file identity, moves, sidecar updates, or indexing. Scenarios that test PDF parsing or text extraction must use valid PDF fixtures with known contents. Sidecars should be small, explicit, and deterministic, with stable metadata and extracted text.

Keep the baseline fixtures immutable during a test. Tests should assert against a copy in the temporary workspace, not against files under version control.

## Local storage and service isolation

The test instance should use a filesystem-backed storage implementation, or a test adapter that exposes the same storage operations as the production adapter. It must not require S3 credentials, a live Garage endpoint, or network access to an object-storage service.

The local storage adapter should preserve the production semantics relevant to tests, including:

- Listing and reading managed PDF objects.
- Reading and writing authoritative sidecars.
- Copying or moving a PDF safely when a document is renamed.
- Deleting a source only after the destination is safely written.
- Reporting missing objects and sidecars for consistency checks.

The test harness should configure a fresh SQLite database for each isolated run. SQLite is a rebuildable index; sidecars remain the source of truth for document metadata and extracted text. Rebuilding the index must not rewrite or delete fixture PDFs or sidecars merely to make them match the database.

The service should run with background polling and other non-deterministic workers disabled unless a test explicitly covers them. Tests should use a configurable local base URL and a bounded timeout for API calls and asynchronous jobs.

## Test cases as ordered API actions

A test scenario should be a readable sequence of actions. Each action should define:

- An HTTP method, path, and optional query parameters or request body.
- The expected HTTP status and, where useful, response fields.
- Assertions to run after the action against the local filesystem and SQLite database.

Actions run in order. Assertions run after each action so the test can identify the first operation that caused state to diverge.

A scenario might conceptually look like this:

```yaml
name: "Preview does not mutate a document"
setup:
  fixture: "baseline"
actions:
  - request:
      method: GET
      path: "/api/v1/files/<sha256>"
    expect:
      status: 200
    assert:
      sidecar_unchanged: true
      pdf_locations_unchanged: true
      database_matches_sidecar: true

  - request:
      method: POST
      path: "/api/v1/files/<sha256>/preview"
      json:
        template_name: "invoice"
        emission_date: "2026-09-20"
        emitting_company: "Example Company"
        document_type: "invoice"
        optional_fields: {}
    expect:
      status: 200
      json:
        valid: true
    assert:
      sidecar_unchanged: true
      pdf_locations_unchanged: true
      database_matches_sidecar: true
```

This is an illustrative format, not a currently supported test-file schema. The eventual harness may use YAML, JSON, or ordinary Python test functions. It should prioritize readability and useful failure messages over introducing a large custom test language.

## What to assert

Every action should check the relevant layers rather than relying only on the HTTP response.

### HTTP API

- Status code and response content type.
- Required response fields and important values.
- Error messages for invalid requests.
- Job completion and job result for asynchronous operations.

### Filesystem

- Expected PDFs and sidecars exist.
- Renames and moves affect the correct paths.
- PDF bytes are preserved by a metadata-only rename or move.
- Sidecars contain the expected metadata, extracted text, and event history.
- Preview and rejected validation requests do not mutate files.
- No duplicate, orphaned, or unexpectedly deleted files are introduced.

### SQLite database

- Indexed rows reflect the authoritative sidecars after synchronization.
- Searchable fields and extracted text are indexed as expected.
- Rebuilding the database from sidecars reproduces the expected index.
- Database state is not treated as authoritative when it disagrees with a sidecar.

Tests should avoid asserting implementation details that are irrelevant to the contract. Where possible, compare normalized records and meaningful fields rather than timestamps or generated job IDs that naturally vary between runs.

## Recommended initial scenarios

Start with a small set of high-value scenarios:

1. **Fresh rebuild:** the database is populated from fixture sidecars, and the filesystem is unchanged.
2. **Read-only requests:** list and detail requests return expected metadata and do not mutate files.
3. **Preview:** a valid preview returns the computed destination without changing the PDF, sidecar, or database.
4. **Invalid validation:** invalid fields return the documented error and leave all persisted state unchanged.
5. **Successful validation:** a valid update safely moves or renames the PDF, updates the sidecar, and synchronizes SQLite.
6. **Idempotency:** repeating a supported operation does not create duplicate records or lose the document.
7. **Consistency anomalies:** missing PDFs, missing sidecars, or database drift are detected and reported.
8. **Rebuild after drift:** after deliberately changing or removing database state, rebuilding restores the index from sidecars without modifying the PDFs or sidecars.
9. **Collision handling:** a rename to an occupied destination follows the documented collision policy without overwriting another document.
10. **Interrupted operations:** simulated failures do not delete the only recoverable copy of a document.

Not every scenario needs to be implemented in the first iteration. The initial goal is a reliable isolation mechanism, a deterministic local storage adapter, and a few representative end-to-end scenarios.

## Running and troubleshooting

Once implemented, the repository should provide a single documented command to run the E2E suite. The command should report which action and which state assertion failed, preserve or print the temporary workspace location on failure when useful, and clean up temporary data after successful runs.

The harness should never point at a developer's normal data directory or a production storage endpoint. It should fail early if its configuration appears to target a non-test database or external object storage.

Until the harness is implemented, this document is a design guide rather than a runnable command reference. See [API.md](API.md) for the API contract and [arch.md](arch.md) for the architecture overview.
