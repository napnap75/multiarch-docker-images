# API — Automatic PDF Renamer

Version: 1.0 (design specification)  
Base path: `/api/v1`  
Backend: FastAPI; storage: Garage S3-compatible bucket; metadata source of truth: per-file sidecars; query index: SQLite.

This document defines the HTTP API consumed by the local web dashboard and the operational API used to observe ingestion, recover the index, and run maintenance jobs. The API is a local, same-origin interface; it does not expose the S3 credentials or bypass the sidecar-as-source-of-truth rule.

## 1. API principles

- **Nextcloud/S3 remains the upload interface.** A user uploads a PDF to the configured `inbox/` prefix through Nextcloud or an S3 client. The API does not accept PDF uploads in v1.
- **Ingestion is server-side and automatic.** The poller discovers stable PDF objects in `inbox/`, extracts text from every page, applies deterministic rules, validates fields, and either renames the file or moves it to `pending/`.
- **Sidecars are authoritative.** For a mutation, persist the sidecar first, then synchronize SQLite. SQLite is used for list/search and consistency diagnostics, and can be rebuilt from sidecars.
- **Business statuses are exactly** `pending`, `processed`, and `validated`. Technical job state is separate and must not be represented as a business status.
- **The initial release is deterministic.** API responses may include a nullable confidence/model section for forward compatibility, but the rules-only release must not fabricate ML scores.
- **No rename undo endpoint exists.** Renames are recorded for audit, not reversed by a user-facing API.
- The API and processing pipeline remain offline except for the configured local Garage endpoint.

## 2. Conventions

### 2.1 JSON and identifiers

Requests and responses use UTF-8 JSON unless an endpoint streams a PDF or returns an HTTP error. Timestamps are ISO 8601 UTC strings. Dates use `YYYY-MM-DD`. S3 object keys are opaque strings, not filesystem paths.

`sha256` is the canonical unique identifier for a managed PDF, computed from the exact PDF bytes. All document endpoints use this digest instead of a separate `file_id` or database `id`. Renaming/moving a PDF without changing its bytes preserves the identifier. Any byte-level content change creates a new document record with a new SHA-256; references to the previous digest must not be silently rewritten. If identical bytes are imported again, ingestion must detect the existing digest and treat the import as a duplicate rather than create a second document record. Compute and verify the digest during ingestion and consistency checks.

### 2.2 Pagination, sorting, filtering

List endpoints use page-based pagination:

- `page`: integer, default `1`, minimum `1`.
- `page_size`: integer, default `50`, maximum `200`.
- `sort_by`: allowlisted field name; default `updated_at`.
- `sort_order`: `asc` or `desc`; default `desc`.

Unknown filters or unsupported sort fields return `422 Unprocessable Entity`. Search and filters operate on SQLite's replica/index; they must not cause a full S3 listing on every dashboard request.

### 2.3 Standard error response

HTTP errors return a simple human-readable text message in the response body, with `Content-Type: text/plain; charset=utf-8`. Error responses do not use a JSON envelope or include field-level details.

Example response body:

```text
The emission date must use YYYY-MM-DD format.
```

HTTP status codes distinguish error categories:
- `400`: malformed request or invalid operation for the current state.
- `404`: unknown file, template, job, or resource.
- `409`: state conflict, such as attempting to start a job of the same kind while one is running, or validating a stale edit.
- `422`: request schema or field validation failure. Return a concise plain-text explanation.
- `503`: required local dependency unavailable (Garage or SQLite). Return a concise plain-text message without infrastructure details.
- `500`: unexpected server error. Return a generic plain-text message; do not expose credentials, stack traces, or secrets to the browser.

This convention applies to HTTP error responses from every endpoint. Successful responses retain the format specified by each endpoint (JSON or PDF bytes).

## 3. Endpoint overview

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/ready` | Dependency/readiness check |
| GET | `/files` | Browse, filter, sort, and search documents |
| GET | `/files/{sha256}` | Document detail and metadata |
| GET | `/files/{sha256}/pdf` | Stream the current PDF for the embedded viewer |
| POST | `/files/{sha256}/preview` | Validate proposed fields and calculate the target key without mutation |
| POST | `/files/{sha256}/validate` | Validate metadata and apply the final move/rename if required |
| GET | `/consistency` | List known storage/index anomalies |
| POST | `/consistency/scan` | Start a consistency scan |
| POST | `/consistency/rebuild` | Rebuild SQLite from authoritative sidecars |
| GET | `/jobs/{job_id}` | Get asynchronous job status and result (maintenance or retraining) |
| GET | `/ingestion/status` | Inspect poller status and recent activity |
| GET | `/config` | Retrieve the current JSONC configuration file |
| PUT | `/config` | Validate and save a modified JSONC configuration file |
| GET | `/models` | Post-v1: list model versions |
| POST | `/retrain` | Post-v1: start local model training |
| POST | `/retrain/{job_id}/activate` | Post-v1: activate a trained model |
| POST | `/retrain/{job_id}/discard` | Post-v1: discard a candidate model |

The endpoints in the models/retraining section are post-v1 capabilities. They may be omitted from the first deterministic release without affecting the core API.

## 4. Configuration file

### GET `/config`

Returns the complete current JSONC configuration as UTF-8 text. Use `Content-Type: application/jsonc; charset=utf-8` (or `text/plain; charset=utf-8`). This endpoint retrieves the configuration file itself; it does not expose a separate template-list or template-editing API.

### PUT `/config`

Accepts the complete replacement JSONC configuration as a UTF-8 text body. Validate its syntax and schema before writing. If invalid, return `422` with a concise plain-text error and leave the active file unchanged. If valid, atomically replace the configured file, reload the in-memory template registry, and return `200` JSON:

```json
{"saved": true, "reloaded": true}
```

The implementation must avoid exposing secrets in the configuration file. Only one complete configuration replacement is supported; individual templates are changed by editing this file.

## 5. Health and ingestion workflow

### GET `/health`

Liveness only. Returns `200` when the API process is running.

Example response:

```json
{"status": "ok"}
```

### GET `/ready`

Checks that the service has loaded its configuration and can access the dependencies required to serve requests. Do not expose endpoint URLs or credentials.

Example response:

```json
{
  "status": "ready",
  "checks": {
    "configuration": "ok",
    "garage": "ok",
    "sqlite": "ok",
    "templates": "ok"
  }
}
```

Return `503` with a plain-text error message if a required dependency is unavailable.

### GET `/ingestion/status`

Returns poller state and a concise operational summary. It does not expose secrets or the contents of arbitrary S3 objects.

Example response:

```json
{
  "poller": {
    "enabled": true,
    "running": true,
    "poll_interval_seconds": 10,
    "last_poll_started_at": "2026-10-14T07:00:00Z",
    "last_poll_completed_at": "2026-10-14T07:00:01Z"
  },
  "counts": {
    "discovered": 12,
    "processed": 8,
    "pending": 3,
    "technical_failures": 1
  },
  "last_success_at": "2026-10-14T06:59:50Z"
}
```

The values above are illustrative. The configured polling interval is implementation configuration. This endpoint is observational; the dashboard does not need to start the poller or submit files to it.

### Import workflow (internal, not an HTTP upload endpoint)

1. The poller lists `inbox/` and ignores non-PDF keys, hidden files, and known temporary upload keys.
2. A candidate is eligible only after its size and ETag remain unchanged across two consecutive listings.
3. The service downloads the PDF, computes its content hash, and extracts all available text from every page. OCR is out of scope for the initial release.
4. Empty/insufficient text, ambiguous deterministic classification, or invalid required fields routes the document to `pending/` with a review reason and the business status `pending`.
5. An unambiguous classification with valid required fields produces a target key. The service resolves collisions with an incremental suffix, copies the object, records the sidecar, synchronizes SQLite, and deletes the source only according to the safe copy-before-delete procedure.
6. A successful automatic move receives `processed`. A user-confirmed or corrected document receives `validated`.
7. Sidecars contain the complete extracted text and event history. SQLite indexes that text for full-text search across all pages.
8. Interrupted work is reconciled at startup and by the consistency job. The importer must be idempotent and must not delete the only recoverable copy.

### Technical failures

Technical failures are not business statuses and must not automatically set a document to `pending`. For a transient technical failure, the service makes the initial attempt and up to three retries after 5 seconds, 30 seconds, and 2 minutes. Once these retries are exhausted, processing of that item stops; no additional retry or manual-review workflow is launched. Other objects continue to be discovered and processed. The API may expose the last technical failure in operational diagnostics, separate from the document's business status.

## 6. File list and search

### GET `/files`

Query parameters:

| Parameter | Type | Description |
|---|---|---|
| `page` | integer | Page number, default 1 |
| `page_size` | integer | Page size, default 50, max 200 |
| `q` | string | Search current/original key, common fields, optional fields, and complete extracted text from all pages |
| `status` | string | Repeatable or comma-separated values: `pending`, `processed`, `validated` |
| `template` | string | Exact configured template name |
| `document_type` | string | Exact document type |
| `emitting_company` | string | Normalized company name |
| `period` | string | Exact period string |
| `date_from` | date | Inclusive emission-date lower bound |
| `date_to` | date | Inclusive emission-date upper bound |
| `sort_by` | string | Allowlisted: `emission_date`, `period`, `current_key`, `status`, `template_name`, `document_type`, `emitting_company`, `created_at`, `updated_at` |
| `sort_order` | string | `asc` or `desc` |

Example: `GET /api/v1/files?status=pending&q=EDF&page=1&page_size=25`

Response:

```json
{
  "items": [
    {
      "sha256": "0123456789abcdef...",
      "current_key": "pending/edf/document.pdf",
      "original_key": "inbox/document.pdf",
      "status": "pending",
      "template_name": "invoice",
      "emission_date": "2026-09-20",
      "period": "September 2026",
      "emitting_company": "EDF",
      "document_type": "invoice",
      "confidence": null,
      "updated_at": "2026-10-14T07:00:00Z"
    }
  ],
  "page": 1,
  "page_size": 25,
  "total_items": 1,
  "total_pages": 1
}
```

The response must not claim ML confidence for a rules-only decision. `confidence` is nullable and may be omitted if not applicable. List items use `sha256` as the canonical identifier and return the computed `period`. Search must include the full extracted text from every page, not only the first page. Matching semantics (substring/token behavior) should be documented by the implementation and tested; use SQLite indexes/FTS rather than loading every record into application memory.

## 7. Document detail and PDF content

### GET `/files/{sha256}`

Returns the complete document detail in a single request, including metadata and the full extracted text from the PDF.

Example response:

```json
{
  "sha256": "0123456789abcdef...",
  "original_key": "inbox/document.pdf",
  "current_key": "pending/document.pdf",
  "status": "pending",
  "template_name": "invoice",
  "emission_date": "2026-09-20",
  "period": "September 2026",
  "emitting_company": "EDF",
  "document_type": "invoice",
  "optional_fields": {"invoice_number": null, "amount": 125.5},
  "extracted_text": "Complete text from page 1...\\n\\nComplete text from page 2...",
  "target_key_preview": null,
  "confidence": null,
  "events": [
    {
      "type": "imported",
      "timestamp": "2026-10-14T07:00:00Z",
      "payload": {"source_key": "inbox/document.pdf"}
    }
  ],
  "created_at": "2026-10-14T07:00:00Z",
  "updated_at": "2026-10-14T07:00:00Z"
}
```

The event history is read-only through this endpoint. Do not expose internal credentials or unnecessary infrastructure details.

### GET `/files/{sha256}/pdf`

Streams the current PDF from Garage through the backend for the embedded viewer. It must not return a public or signed URL that bypasses the local service.

Requirements:
- Return `Content-Type: application/pdf` and a safe inline `Content-Disposition`.
- Support HTTP range requests (`206 Partial Content`) if supported by the chosen PDF viewer and storage client; otherwise document the limitation.
- Return `404` if the object is genuinely missing and create/retain a consistency anomaly for diagnosis.
- Never return the extracted text in place of the PDF bytes.

## 8. Preview and validation

All mutation endpoints must validate the requested state, recompute the period from `emission_date` and the applicable template period rules, calculate the destination key from the template's key pattern, and prevent an invalid document from being renamed. Filename parsing must never be used as a source for the period.

### Shared request fields

```json
{
  "template_name": "invoice",
  "emission_date": "2026-09-20",
  "emitting_company": "EDF",
  "document_type": "invoice",
  "optional_fields": {
    "invoice_number": "INV-2026-001",
    "amount": 125.5
  }
}
```

- `template_name`, `emission_date`, `emitting_company`, and `document_type` must conform to the selected template's schema.
- The server selects the applicable period rules from the configured mapping for the company and document type, calculates `period` from `emission_date`, and never accepts a client-supplied period or period format.
- `optional_fields` may contain only fields declared by the selected template. Empty optional fields are allowed where the schema permits them.
- The server must never trust a target key supplied by the browser; it calculates the target key itself.

### POST `/files/{sha256}/preview`

Validates a proposed set of values and returns the computed period, target key, and a simple error message if validation fails without changing the sidecar, status, or S3 objects. The UI calls this as values change (debounced by the frontend).

Request: shared request fields above.

Response when valid:

```json
{
  "valid": true,
  "computed_period": "September 2026",
  "target_key": "files/edf/2026/09/invoice-INV-2026-001.pdf",
  "error": null
}
```

Response when invalid is still a normal validation result (`200`), not an infrastructure error:

```json
{
  "valid": false,
  "computed_period": "September 2026",
  "target_key": null,
  "error": "Select a configured company."
}
```

The preview response contains the computed target key.

### POST `/files/{sha256}/validate`

Finalizes a document after the user confirms or corrects its metadata.

Request:

```json
{
  "template_name": "invoice",
  "emission_date": "2026-09-20",
  "emitting_company": "EDF",
  "document_type": "invoice",
  "optional_fields": {"invoice_number": "INV-2026-001", "amount": 125.5}
}
```

Success response:

```json
{
  "sha256": "0123456789abcdef...",
  "status": "validated",
  "previous_key": "pending/document.pdf",
  "current_key": "files/edf/2026/09/invoice-INV-2026-001.pdf",
  "renamed": true,
  "sidecar_updated": true,
  "sqlite_synced": true,
  "updated_at": "2026-10-14T07:05:00Z"
}
```

Behavior:
1. Validate the requested template and every required field.
2. Recompute and validate the period from emission date and template rules.
3. Compute the target key on the server and resolve collisions with an incremental suffix.
4. If the target key differs from the current key, copy to the target before deleting the source. If the key is unchanged, do not perform an unnecessary rename.
5. Persist the authoritative sidecar, including corrected values and a `validated` event, then synchronize SQLite.
6. Return the final key and status.

If required values are invalid, return `422` with a concise plain-text error message and do not rename. If a storage operation is interrupted, leave a recoverable state and reconcile from the sidecar; never delete the only recoverable copy. The operation must be idempotent.

## 9. Consistency and maintenance jobs

### GET `/consistency`

Returns known anomalies, optionally filtered by `kind`, `page`, and `page_size`.

Anomaly kinds include:
- `orphaned_object`: a PDF object exists without a sidecar.
- `missing_object`: a sidecar refers to a PDF key that is not present.
- `missing_sidecar`: an indexed/managed PDF has no authoritative sidecar.
- `sqlite_drift`: SQLite differs from the sidecar.
- `interrupted_operation`: state suggests a copy/delete operation was interrupted.

Response items should include `id`, `kind`, `sha256` when known, affected key, detection timestamp, concise details, and whether a safe repair is available. Listing anomalies must not mutate storage.

### POST `/consistency/scan`

Starts a consistency scan. Returns `202 Accepted`:

```json
{"job_id": "job_abc", "kind": "consistency_scan", "status": "queued"}
```

Only one scan of the same kind may run at a time; a duplicate start returns `409`.

### POST `/consistency/rebuild`

Rebuilds the SQLite index from authoritative sidecars. Returns `202 Accepted` with a job ID. This operation must not rewrite or delete PDF objects and sidecars just to match SQLite. It replaces/recreates the replica and records a job result.

### GET `/jobs/{job_id}`

Returns job status for consistency scan, SQLite rebuild, or other asynchronous maintenance tasks.

```json
{
  "job_id": "job_abc",
  "kind": "sqlite_rebuild",
  "status": "running",
  "started_at": "2026-10-14T07:00:00Z",
  "finished_at": null,
  "progress": {"completed": 125, "total": 500},
  "result": null,
  "error": null
}
```

Allowed job statuses are technical job states such as `queued`, `running`, `succeeded`, and `failed`; these are not document business statuses. This generic endpoint reports maintenance and retraining jobs. If the process restarts and in-memory job state is lost, the API should report the job as interrupted/unknown and use persisted status files or database state where available.

## 10. Local model maintenance endpoints

The Maintenance page manages local model versions using these endpoints:

- `GET /api/models`: list supported model types, stored versions, and the active version for each type.
- `POST /api/models/train`: train a new version from indexed documents with business status `validated`. JSON body: `{"model_name":"additional_field_values"}`. Returns the model name, version, training sample count, and status. Training errors return a validation error when the labeled dataset is insufficient.
- `POST /api/models/{model_name}/{version}/activate`: select an existing version for runtime inference. The selection is written atomically to `MODEL_DIR/active_models.json`.

Supported model types are `field_emitting_company`, `field_document_type`, `field_emission_date`, `additional_field_groups`, `additional_field_values`, `template_name`, and `period_format`. The shared additional-field model is trained and activated once for all eligible field names. All additional-field values are extracted by the shared `additional_field_values` model artifact. It uses one shared token-level classifier to label document-text spans by field, allowing values not seen during training when their text appears in the document. Each extracted span is confidence-gated independently. Training labels are accepted only when the validated field value appears in the saved extracted text.

Training runs locally and stores versioned joblib artifacts under `MODEL_DIR/versions/<model_name>/`. Training does not activate a model automatically. Runtime inference requires an explicitly active model version; missing or uncertain model predictions are routed to `pending`. The model artifacts must be trusted because loading joblib files can execute code.

The first implementation trains synchronously in the API request and reports training errors directly. A future implementation may move long-running training to a persistent background-job queue and add held-out evaluation metrics before activation.

## 11. Consistency and implementation rules

1. Sidecar is the source of truth; SQLite is a rebuildable query replica.
2. Every mutation writes an auditable sidecar event and updates `updated_at`; event types include `imported`, `classified`, `renamed`, `corrected`, `validated`, and `reprocessed` where applicable.
3. Full extracted text from every PDF page is persisted in the sidecar and indexed in SQLite for full-text search.
4. Period is derived only from `emission_date` and the selected template's configured period rules. The filename is never a period input.
5. All final object keys are calculated server-side from the selected template and validated field values.
6. Rename/move uses copy-before-delete, resolves collisions with an incremental suffix, and records the final key.
7. All mutation endpoints are idempotent where feasible and protect against stale edits using `expected_revision` or an equivalent concurrency token.
8. Technical processing failures are not assigned a business status. After the defined retry sequence is exhausted, that item stops processing without another workflow.
9. The API does not expose an endpoint to undo or reverse a file rename.
10. No endpoint uploads a PDF directly; the Nextcloud/S3 inbox remains the ingestion boundary.

## 12. Open implementation choices

The following are implementation details, not reasons to block the API contract:
- Exact stable ID strategy and revision token format.
- Exact SQLite full-text search tokenizer and phrase/prefix matching semantics.
- Whether PDF byte-range requests are supported by the chosen S3 client/viewer.
- Exact representation of technical processing diagnostics after retries are exhausted.
