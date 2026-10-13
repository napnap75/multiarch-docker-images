# Architecture — Automatic PDF Renamer

Architecture document derived from the PRD, following the BMAD method. Personal tool, single user, 100% offline, self-hosted.

## 1. Architecture Overview

A single Python service (FastAPI) hosts the API, the inbox poller, and the static React dashboard. It talks to a Garage S3 bucket (source of truth for objects and JSON sidecars) and mirrors metadata into a local SQLite replica.

Everything runs in one container, deployed next to the existing Garage and Nextcloud deployments. There is no message broker, no external database, and no runtime network access beyond Garage and local dashboard serving.

### 1.1 High-level view

```mermaid
flowchart TD
  nc["Nextcloud (External Storage S3)"]
  user["Owner (browser)"]
  dash["React dashboard (static build)"]
  api["FastAPI service (single container)"]
  poll["Inbox poller (asyncio task)"]
  s3["Garage S3 bucket"]
  db[("SQLite replica (WAL)")]
  worker["Subprocess jobs: retrain / consistency / rebuild"]
  model[("Model registry on disk")]

  user --> dash
  dash --> api
  api --> db
  api --> s3
  api --> worker
  worker --> db
  worker --> model
  worker --> s3
  poll --> s3
  poll --> db
  nc --> s3
  user --> nc
```

## 2. Architecture Goals and Constraints

- Offline-first: no runtime downloads, no external services (NFR1, AC10).
- Zero silent wrong renames: every ambiguous path routes to not processed (NFR3).
- Crash safety: copy-then-delete renames, atomic sidecar writes, SQLite repairable from sidecars alone (NFR4, AC8, AC9).
- Minimal operational surface: one container, one JSONC configuration file, env-var credentials (NFR6).
- Single user, local network: performance targets are modest (NFR2) but correctness targets are strict.

## 3. Tech Stack and Key Decisions

| Concern | Decision | Rationale |
|---|---|---|
| Language/runtime | Python 3.11+, single service | NFR7; ecosystem for ML and S3. |
| Web framework | FastAPI + Uvicorn | NFR7; async I/O for S3 polling and PDF streaming. |
| Text extraction | PyMuPDF | Fastest option; AGPL acceptable for a personal offline tool. Wrapped behind a small extractor interface so it remains swappable (PRD open question resolved). |
| S3 client | boto3 | Most complete API coverage (copy-object, versioning, ETag), mature Garage compatibility. Wrapped behind a thin storage interface; v1 ships one implementation. |
| SQLite access | SQLAlchemy ORM, WAL mode | Declared replica schema, typed models, migrations via Alembic. Sidecar remains authoritative; ORM layer only reads/writes the mirror. |
| ML classification | scikit-learn TF-IDF + Logistic Regression | PRD FR3.1; small corpus is sufficient, no deep learning needed. |
| Fuzzy matching | rapidfuzz | PRD FR4.2, counterparty gazetteer. |
| Dates | python-dateutil | PRD FR4.2, emission date parsing and period computation. |
| Frontend | React + Vite, react-pdf viewer | NFR7; built to static assets served by FastAPI. |
| Process model | API, poller, and background jobs in one container | Single-user tool; a process manager is unnecessary complexity. |
| Inbox poller | asyncio background task inside the FastAPI process | Lightweight, no queue infrastructure; restart scan (FR1.3) covers crash recovery. |
| Long jobs (retrain, consistency, rebuild) | Subprocesses launched and monitored by the API | Keeps the event loop responsive; status exposed via the existing endpoints (FR9, FR10). |
| Dashboard auth | Username + password (single account, env-var credentials, session cookie) | Local network but not fully trusted; cheap to implement. |
| Testing | Unit tests only, pytest | Single-user offline tool; the storage layer is behind interfaces so mocks are cheap. Integration/CI coverage deferred. |

## 4. Components

### 4.1 Service process (single container)

One Python process runs:

- FastAPI app: serves the API, the static dashboard, and the PDF streaming proxy.
- Poller task: asyncio loop, configurable interval, lists the `inbox/` prefix.
- Job supervisor: spawns subprocesses for retrain, consistency scan, and SQLite rebuild; tracks their status in memory and in SQLite.

On startup, the process runs the recovery scan (FR1.3, FR10.3): objects in `inbox/` and `pending/` without sidecar records are enqueued for processing.

### 4.2 Module layout

```
app/
  main.py            # FastAPI app, lifespan: recovery scan + poller start
  config.py          # env vars + JSONC configuration loading
  storage/           # S3 interface (boto3), key helpers, collision suffix
  sidecar.py         # atomic sidecar write (temp key + copy), schema
  replica/           # SQLAlchemy models, sync, rebuild
  pipeline/          # extract (PyMuPDF), classify, fill fields, validate
  period.py          # period computation from emission date + period format config
  templates.py       # JSONC template registry (slots, period rules, key patterns)
  rename.py          # copy-then-delete move + event logging
  jobs/              # poller, consistency scan, retrain subprocess, rebuild
  api/               # routes: files, templates, retrain, consistency, auth
web/                 # React + Vite app (built to static assets)
models/              # versioned model registry (initial + trained)
```

### 4.3 Processing pipeline

One ingestion run, fully synchronous within the poller task:

```mermaid
flowchart TD
  in["Stable object in inbox/"] --> dl["Download PDF"]
  dl --> tx["Extract first-page text (PyMuPDF)"]
  tx --> empty{"Text empty?"}
  empty -- yes --> pending["Move to pending/ (reason: empty text)"]
  empty -- no --> cls["Classify (TF-IDF + LogReg)"]
  cls --> conf{"Confidence >= threshold?"}
  conf -- no --> pending
  conf -- yes --> fill["Fill fields: regexes, companies list, additional fields from groups, period from emission date"]
  fill --> valid{"Fields valid?"}
  valid -- no --> pending
  valid -- yes --> rename["Compute key, copy-then-delete, write sidecar + SQLite"]
  rename --> done["Status: processed"]
  pending --> wait["Status: not processed (sidecar with failing fields)"]
```

Every terminal step writes a sidecar event (classified, renamed, corrected, validated) per FR5.2. Sidecar first, SQLite second (FR5.1); the consistency job repairs any drift after a crash.

### 4.4 Storage layer

- boto3 client pinned to the Garage endpoint, credentials from env vars.
- Interface operations: list prefix, head object, get, put, copy, delete. Rename = copy + delete, collisions resolved with an incremental suffix before the copy.
- Sidecar write: put JSON to a temp key, then copy over the final sidecar key, then delete the temp key. Never an in-place overwrite.

### 4.5 Metadata and replica

- Sidecar JSON is the sole source of truth; schema per PRD section 7 (now including the period field).
- SQLAlchemy models `files` and `consistency_flags` mirror it. `extracted_text` is stored in SQLite for search; it is a replica, and the rebuild command repopulates it from sidecars.
- `POST /consistency/rebuild` and the CLI rebuild command both clear and repopulate SQLite from all sidecars (AC9).
- Divergence resolution is always sidecar-wins (PRD 3.2); the consistency job flags drift rather than guessing.

#

### 4.5.1 Sidecar file format and schema

The sidecar is the authoritative metadata record for one PDF. Its schema and serialization behavior are implemented in `app/sidecar.py`; this section documents that implementation. The current schema version is `"1.0"`.

#### File naming and location

A sidecar key is derived from the PDF object key by replacing a terminal, lowercase `.pdf` extension with `.meta.json`. If the key does not end in `.pdf`, `.meta.json` is appended. For example:

- `files/invoice.pdf` → `files/invoice.meta.json`
- `inbox/document.pdf` → `inbox/document.meta.json`

Although the project configuration and this specification use JSONC, the current `app/sidecar.py` serializer uses Python `json.dumps` and therefore writes strict JSON without comments or trailing commas. Strict JSON is a valid subset of JSONC. Consumers must not require comments to be present in a sidecar; JSONC comments are for human-authored configuration/documentation, not generated metadata.

#### Canonical schema example

The following is a JSONC-readable example. Comments explain the fields and are not emitted by the current serializer.

```jsonc
{
  "schema_version": "1.0", // Required. Sidecar schema version.
  "sha256": "0123456789abcdef...", // Required. SHA-256 hex digest of the PDF bytes.
  "original_key": "inbox/invoice.pdf", // Required. Original S3 object key before renames.
  "current_key": "files/acme/invoice-2026-01.pdf", // Required. Current S3 key for the PDF.
  "status": "processed", // Required. One of the three business statuses below.
  "template_name": "invoice", // Required. Template/document family selected for this PDF.
  "period_format": "YYYY-MM", // Required. Format/rule identifier used for the period string.
  "confidence": 0.97, // Serialized by the model; numeric classification confidence (0.0–1.0).
  "emission_date": "2026-01-15", // Required. Emission date; expected ISO YYYY-MM-DD.
  "period": "2026-01", // Required. Computed or validated period string.
  "emitting_company": "ACME", // Required. Normalized name of the issuing company.
  "document_type": "invoice", // Required. Fine-grained document type.
  "optional_fields": { // Serialized by the model; template-specific extracted values.
    "invoice_number": "INV-2026-001",
    "amount": 125.5,
    "currency": "EUR"
  },
  "extracted_text": "Invoice ACME ...", // Serialized by the model; text extracted from the first page.
  "created_at": "2026-01-15T10:20:30+00:00", // Required. Creation timestamp in ISO 8601.
  "updated_at": "2026-01-15T10:21:05+00:00", // Required. Last sidecar mutation timestamp in ISO 8601.
  "events": [ // Required array; may be empty.
    {
      "type": "classified", // Event name; see supported event names below.
      "payload": {
        "template_name": "invoice",
        "confidence": 0.97
      }, // Required object; event-specific data, intentionally extensible.
      "timestamp": "2026-01-15T10:20:45+00:00" // Required ISO 8601 timestamp.
    }
  ]
}
```

#### Field definitions

| Field | Type | Presence and meaning |
|---|---|---|
| `schema_version` | string | Required. Must equal `"1.0"` in the current implementation. |
| `sha256` | string | Required. SHA-256 digest of the PDF content; used to identify content and support safe resume checks. |
| `original_key` | string | Required. S3 key where the PDF was first registered, retained across renames. |
| `current_key` | string | Required. S3 key currently associated with the PDF. |
| `status` | string | Required. One of `not processed`, `processed`, or `validated`. |
| `template_name` | string | Required. Name of the configured template/document family. |
| `period_format` | string | Required. Period format/rule label, e.g. `YYYY-MM` or `YYYY-MM-DD`. |
| `confidence` | number | Emitted by the dataclass serializer, default `0.0`. Intended for a classification score between 0 and 1; the current validator checks only that it is numeric, not that it is within that range. |
| `emission_date` | string | Required. Document emission date; `build_sidecar` documents ISO `YYYY-MM-DD`. |
| `period` | string | Required. Period derived from the emission date and applicable rules. |
| `emitting_company` | string | Required. Issuing company name. |
| `document_type` | string | Required. More specific document type within the template. |
| `optional_fields` | object | Emitted by the dataclass serializer, default `{}`. Template-specific values; values may be strings, numbers, booleans, arrays, objects, or null as allowed by the application schema. |
| `extracted_text` | string | Emitted by the dataclass serializer, default empty string. Extracted first-page text. |
| `created_at` | string | Required. ISO 8601 timestamp set on object construction if not supplied. |
| `updated_at` | string | Required. ISO 8601 timestamp set on construction and refreshed by `add_event` and `set_status`. |
| `events` | array of objects | Required. Chronological event history; each event has `type`, `payload`, and `timestamp`. |

The dataclass serializer emits all fields listed above, including `confidence`, `optional_fields`, and `extracted_text`, even though those three are not in the implementation's required-field set. `Sidecar.from_dict` supplies defaults for omitted fields, but `validate_sidecar` still requires the core required-field set described below.

#### Business statuses and event types

The only business statuses accepted by `validate_sidecar` are:

- `not processed` — not successfully processed for automatic filing, including items awaiting review.
- `processed` — processed by the pipeline.
- `validated` — reviewed/confirmed by the owner.

Technical progress and error details must not be invented as additional business statuses. Store detailed failure or review reasons in event payloads and/or the appropriate processing metadata, while keeping these three statuses stable.

The event names declared by `EventType` are:

- `classified`
- `renamed`
- `validated`
- `corrected`
- `imported`
- `reprocessed`

Each event is an object with exactly the following required structure:

- `type`: string event name.
- `payload`: object containing event-specific details; the schema intentionally allows different payloads for different event types.
- `timestamp`: ISO 8601 timestamp generated in UTC when `Sidecar.add_event` is called.

The current validator checks that these three event properties have the correct broad types, but it does not enforce the event-name enum, timestamp parseability, or a per-event payload schema.

#### Validation rules implemented by `app/sidecar.py`

`validate_sidecar(data)` returns a pair `(is_valid, errors)`. It checks:

1. Presence of the required keys: `schema_version`, `sha256`, `original_key`, `current_key`, `status`, `template_name`, `period_format`, `emission_date`, `emitting_company`, `document_type`, `period`, `created_at`, `updated_at`, and `events`.
2. Presence of the six common fields: `template_name`, `period_format`, `emission_date`, `emitting_company`, `document_type`, and `period`. These are also included in the required-key set.
3. `schema_version` and `sha256` are strings.
4. `confidence`, if present, is numeric; `optional_fields`, if present, is an object/dictionary; and `events`, if present, is an array/list.
5. Every event is an object with string `type`, string `timestamp`, and object `payload`.
6. `schema_version` equals the supported version `"1.0"`.
7. `status` is one of the three business status values.

The current validator does **not** yet enforce the types of every required string field, validate the SHA-256 hexadecimal shape, parse date/timestamp strings, constrain confidence to 0–1, or validate `optional_fields` against a template-specific schema. These are potential hardening tasks rather than guarantees of the current implementation.

#### Serialization, reads, and atomic writes

- `serialize(sidecar, indent=2)` serializes all dataclass fields as UTF-8-friendly JSON (`ensure_ascii=False`), with sorted keys and two-space indentation by default.
- `deserialize(json_str)` parses strict JSON and constructs a `Sidecar`; despite the JSONC terminology used for configuration, comments and trailing commas are not accepted by Python's standard `json.loads`.
- `get_sidecar_key(pdf_key)` derives the sidecar key using the naming rule above.
- `write_atomic(storage_client, sidecar, target_key)` writes serialized content to a unique temporary key under `tmp/`, copies it to the target key, and deletes the temporary object. On failure, it attempts best-effort cleanup and raises `RuntimeError`.
- `read_sidecar(storage_client, pdf_key)` returns the deserialized sidecar, or `None` if the sidecar is missing or JSON/required-value parsing fails.
- `matching_sha256(storage_client, pdf_key, expected_sha)` returns true only when a readable sidecar exists and its `sha256` matches the expected digest.

The copy operation used to replace the final sidecar is atomic at the individual object operation level, but the complete sequence involving the temporary key, PDF rename, sidecar, and SQLite replica is not a cross-object transaction. Recovery and consistency checks must therefore handle intermediate states.

## 4.6 Period computation

A pure function of (emission date, template period rules): granularity in {day, month, quarter, year} and offset in {current, next}. Produces the human-readable period string (e.g. "Q3 2026", "September 2026", "2026"). Recomputed whenever the emission date or template changes on the detail page; the user can override the suggested value, and validation checks consistency between period, date, and template rules (FR4.2, FR4.3, AC5c).

### 4.7 Frontend

The dashboard is served as static assets by FastAPI. Views: file list (filters, counters, badges), detail page (PDF preview and editable metadata), and a Config page for editing JSONC configuration. Saving validates the content before updating the configured file and reloading the in-memory template registry. Auth: API routes use the configured dashboard credentials.

## 5. Data Flow: Renaming and Validation

```mermaid
sequenceDiagram
  participant U as Owner
  participant API as FastAPI
  participant S3 as Garage
  participant DB as SQLite
  U->>API: POST /files/{id}/validate (template, fields)
  API->>API: Validate fields, compute target key
  alt key changed
    API->>S3: Copy object to target key
    API->>S3: Delete old key
  end
  API->>S3: Atomic sidecar update (status: validated, event)
  API->>DB: Mirror update
  API-->>U: 200, new key
```

## 6. Jobs and Scheduling

| Job | Trigger | Execution |
|---|---|---|
| Inbox poll | Interval (configurable, default from PRD) | asyncio task, in-process |
| Stability check | Two consecutive listings, same size + ETag | Poller state kept in memory |
| Consistency scan | Periodic (configurable) + on demand | Subprocess |
| Retrain | Dashboard action | Subprocess; writes a new versioned model dir; activation is an atomic pointer swap, no restart (AC6) |
| SQLite rebuild | Dashboard action or CLI | Subprocess |

Subprocesses report progress and metrics by writing a status JSON consumed by the API's status endpoints. Only one job of each kind runs at a time; the API refuses a duplicate start.

## 7. Configuration and Secrets

- Templates: single JSONC file defining: document family, document types, period rules, optional field schema, extraction regexes, validation rules, key pattern, target prefix.
- Environment: S3 endpoint, access key, secret key, bucket name, poll interval, confidence threshold, dashboard credentials, paths (SQLite file, model registry, template YAML).
- No secrets in the YAML; no secrets in sidecars.

## 8. Error Handling and Crash Safety

- Ingestion: any unexpected exception marks the file not processed with the reason in the sidecar; the poller continues.
- Rename: copy strictly before delete; a crash between the two leaves both keys, and the startup scan re-evaluates (the sidecar's current key is authoritative).
- Sidecar/SQLite: sidecar first; drift repaired by the consistency job; rebuild is always possible from sidecars alone.
- Retrain: trained models land in a new versioned directory; activation swaps an active-model pointer; a failed training run leaves the current model untouched.
- All object-not-found conditions during processing are treated as consistency anomalies and flagged (FR5.3).

## 9. Deployment

- One Docker image: Python backend + prebuilt static frontend assets + model registry. Garage and Nextcloud are pre-existing deployments on the same network; the compose file of this project only declares the renamer service.
- SQLite file and model registry on a mounted volume; container restarts are stateless otherwise.
- Offline dependency policy: all Python wheels and the frontend build are baked into the image build; the spaCy French model, if used, is vendored at build time.

## 10. Testing Strategy

- Unit tests only (pytest), per the stack decision.
- Mocked seams: storage interface (fake in-memory S3), extractor interface, classifier (fixture model), clock.
- Priority coverage: period computation (granularity/offset matrix), field validation, collision suffix logic, sidecar atomicity ordering, rebuild determinism, key pattern rendering.
- The pipeline is exercised end-to-end in unit tests with mocked S3 to validate the not-processed routing reasons.

## 11. Architectural Risks and Mitigations

| Risk | Mitigation |
|---|---|
| PyMuPDF AGPL incompatibility if the tool is ever shared | Extractor interface keeps the swap to pdfplumber cheap. |
| Garage quirks in copy-object/versioning | Week 1 spike per PRD; storage interface isolates the client. |
| Corpus too small for reliable classification | Low-confidence routing to pending; retrain on validated files grows the corpus. |
| SQLite drift after crash | Sidecar-first ordering + consistency job + full rebuild path. |
| Single-process blast radius | Long jobs are subprocesses; the poller catches per-file exceptions. |

## 12. Traceability to PRD

Every epic maps to a component: ingestion (jobs/poller), extraction and classification (pipeline), data model and validation (templates, period, gazetteer, pipeline), renaming and logging (rename, sidecar), dashboards (web + api), template management (templates.py + config), retraining (jobs/retrain + models), consistency and recovery (jobs/consistency, replica/rebuild). Acceptance criteria AC1-AC12 are covered by the components above; AC8-AC10 by section 8, AC9 by section 4.5.