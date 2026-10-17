# PRD — Automatic PDF Renamer

Product Requirements Document. Derived from the project brief (BMAD). Personal tool, single user, 100% offline. Storage is a self-hosted Garage (S3-compatible) bucket; Nextcloud is the user-facing file interface; JSON sidecars are the source of truth; SQLite is a replicated index.

## 1. Product Overview

A server-side service with a local web dashboard that polls an `inbox/` prefix of a Garage S3 bucket for incoming PDFs, identifies the document type from its content, applies the matching naming template, fills the template fields, moves the object to an organized prefix tree within the bucket, and tracks every file through a three-state lifecycle (`pending` / `processed` / `validated`). Metadata lives in per-file JSON sidecars (source of truth), replicated to SQLite for queries. Low-confidence or invalid files wait for human correction; validated files feed future model retraining. Users upload, download, and share documents through Nextcloud, which mounts the bucket as external storage.

## 2. Personas

Single user: the owner. Technically proficient, wants minimal manual work but zero silent errors.

## 3. Storage and Metadata Architecture

### 3.1 Object storage

- Garage ([https://garagehq.deuxfleurs.fr/](https://garagehq.deuxfleurs.fr/)), self-hosted, S3 API. One versioned bucket, e.g. `pdf-renamer`.
- Key layout: `inbox/` (entry point), `pending/` (files awaiting manual correction, moved out of inbox to avoid re-processing), and `files/` for the processes files (validated or not).
- Sidecars: `{object-key}.meta.json` next to each PDF.
- No encryption at rest in v1 (documents not sensitive); bucket served on the local network only.
- Renames are copy-object + delete within the bucket; collisions resolved with an incremental suffix.

### 3.2 Metadata: sidecars as source of truth, SQLite as replica

- Each PDF has a sidecar JSON object holding: sha256, original key, current key, status, template, document type, emission date, period, emitting company, all optional fields, confidence, and the full event history (classified, renamed, validated, corrected).
- Sidecar is updated atomically on every state change: write the new sidecar to a temp key, copy over the final sidecar key.
- SQLite mirrors the sidecar content in queryable tables for the dashboard (lists, filters, search, and consistency diagnostics). It is a cache/replica, never authoritative. Any sidecar↔SQLite divergence is resolved in favor of the sidecar.
- A rebuild command reconstructs the full SQLite database by scanning all sidecars; a consistency job detects PDFs without sidecars (orphaned) and sidecars without PDFs (missing object) and flags them in the dashboard.

### 3.3 Nextcloud integration

- Nextcloud connects to the bucket via the External Storage (S3/Swift) app, mounted read-write on `inbox/` and read-only on other prefixes, giving upload, download, preview, and sharing without touching the dashboard.
- The renamer service uses its own S3 credentials distinct from Nextcloud's; Nextcloud users never need renamer credentials.
- The renamer never touches Nextcloud's own storage or database; the only shared surface is the bucket.

## 4. Document Lifecycle and Statuses


| Status        | Meaning                                                                                                                                                                     |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| pending | Deterministic rules cannot determine a valid classification or required field, or validation fails. Object moved to the `pending/` prefix, waiting for review.                                           |
| processed     | Automatically classified, fields validated, renamed and moved.                                                                                                              |
| validated     | The user reviewed the result (confirmed or corrected template/fields). Eligible for model retraining. Terminal state for the review flow; the object was renamed and moved. |


Transitions:

- new object in `inbox/` → pending (deterministic rules cannot determine a valid result; object copied to `pending/`)
- new object in `inbox/` → processed (deterministic rules yield an unambiguous result and all required fields are valid)
- pending → validated (user corrects and validates in the detail page; rename then applied)
- processed → validated (user confirms or corrects and validates in the detail page; rename only if the user maid some changes)

## 5. Functional Requirements

### Epic 1 — Ingestion

- FR1.1 Poll the configurable `inbox/` prefix at a configurable interval; process only keys ending in `.pdf`. Ignore other extensions and temporary files (Nextcloud `.part`, `.~lock`, hidden files).
- FR1.2 Wait for object stability: process a key only after its size and ETag are unchanged across two consecutive listings (handles chunked uploads and partial writes).
- FR1.3 On service restart, recover eligible objects in `inbox/` and resume interrupted technical operations. Do not automatically reprocess a `pending` document that is awaiting manual correction unless the user requests processing after correction.
- FR1.4 Optionally reduce latency with bucket notification hooks if the Garage deployment supports them; polling is the v1 baseline.
- FR1.5 After successful processing, the inbox copy is deleted. Documents requiring manual correction are copied to `pending/` and removed from `inbox/`. Transient technical failures remain eligible for retry/recovery and must not be mislabeled as `pending`.

### Epic 2 — Text extraction

- FR2.1 Download the object from S3 and extract text with pdfplumber or PyMuPDF (choice fixed at implementation, documented in config).
- FR2.2 Extract all available text from every page of each PDF; extraction must not stop after the first page. OCR of scanned/image-only documents remains out of scope for v1.
- FR2.3 If extracted text is empty or below a minimum length, route the file to `pending` with reason "empty text".
- FR2.4 Store the complete extracted text from all pages in the sidecar and replicate it to SQLite for the file's lifetime, so search, the detail page, and retraining do not need to re-download and re-parse the object.

### Epic 3 — ML-assisted field extraction and classification

- FR3.1 Process the full extracted document text in ordered stages. Stage 1 extracts `emitting_company`, `document_type`, `additional_fields`, and `emission_date`; emission date extraction runs in the same stage as the other text-derived fields.
- FR3.2 Use local ML to predict applicable additional-field groups from `(emitting_company, document_type)`, then use local ML to extract each selected value from document text. No predefined mapping selects these groups. Do not select or consume the template and period format before this extraction stage completes.
- FR3.3 Stage 2 resolves `template_name` and `period_format` from the configured mapping using the first-stage fields. Stage 3 calculates `period` from `emission_date` and `period_format`, then generates and validates the target storage key.
- FR3.4 Ambiguous or missing indispensable values, unknown values, unresolved mappings, and invalid calculations must route to `pending`; the pipeline must never substitute a default company or document type or fabricate a candidate.
- FR3.5 ML is an assistive candidate-proposal/ranking layer, not an authority that bypasses configuration. Deterministic rules and configured allow-lists remain available as fallback and validation boundaries. ML must be disabled or unavailable without breaking the rules-only path. Exact model strategy and confidence thresholds remain an explicit calibration decision.
- FR3.6 Retrain on demand from the initial labeled corpus plus eligible validated files. Record the training date and evaluation metrics in the local model registry; require evaluation before activation.

### Epic 4 — Document data model, slot filling and validation

#### 4.1 Common fields (mandatory for every document)

Every document, regardless of template, carries at least these five common fields:

- Common field 1 — Template: the selected template (i.e. the document family: invoice, purchase order, contract, bank statement, rent receipt, etc.).
- Common field 2 — Emission date: the date the document was emitted, extracted from its content (not the object modification date).
- Common field 3 — Emitting company: the company or organization that emitted the document, matched against the companies list in configuration.
- Common field 4 — Document type: the fine-grained type of document within the family (e.g. debit advice vs monthly statement for bank statements; quotes vs order confirmations for purchase documents).
- Common field 5 u2014 Period: the period the document refers to, in human-readable form, derived from the emission date according to the period format configuration. The period format is selected per (company, document_type) mapping and includes:
  - Granularity: `day`, `week`, `month`, `quarter`, `semester`, or `year`
  - Format: custom template string with placeholders (e.g., `{month_name} {year}`, `Q{quarter} {year}`)
  - Offset: `current` (date in middle of period), `previous` (arrives after period), `following` (arrives before period)

The object key pattern of each template is composed from these common fields, plus the template's optional fields.

#### 4.2 Optional fields (template-specific)\n\n**Required processing order:** (1) extract emitting company, document type, additional-field values, and emission date from text; (2) resolve template and period format using the first-stage fields and configured mapping; (3) calculate period and storage key. The first stage must not select a default class. If an indispensable field is missing or ambiguous, route the document to `pending`.

- FR4.1 Each template declares in JSONC its optional fields beyond the common five. Additional fields are organized in predefined field groups (e.g., `bank_accounts`, `properties`, `contract_references`, `customer_numbers`) with their possible values. The (company, document_type) mapping specifies which field groups to extract for each document. Examples: bank statements extract from `bank_accounts` group; rent receipts extract from `properties` group.
- FR4.2 Extract `emitting_company`, `document_type`, `additional_fields`, and `emission_date` from the document text in the first processing stage. Predict additional-field groups with a local ML model from `(emitting_company, document_type)`, then extract their values with local ML from document text. No predefined mapping or allow-list determines these decisions. Only after this stage resolves required fields may the system select `template_name` and `period_format`. Compute `period` server-side from `emission_date` and the selected period format, then generate the storage key from the validated fields and template pattern. Recompute period and key whenever a relevant source field or mapping changes; period is not client-editable.
- FR4.3 Validate fields before renaming: emission date parses and is within a plausible range, period is non-empty and consistent with the emission date and the template's granularity/offset, emitting company recognized, document type among the template's allowed values, each optional field matches its declared format (empty optional fields are allowed). Any failure routes to `pending` with the failing fields identified.
- FR4.4 Collisions in the target prefix are resolved with an incremental suffix; the final key is always the one logged.

### Epic 5 — Renaming, moving, logging

- FR5.1 Rename and move the object within the bucket using a recoverable sequence: copy to the target key, persist the sidecar with the new current key and event, delete the source key only after the sidecar is durable, then synchronize SQLite. These stores do not share a transaction. A crash or replica failure is recovered by the consistency job. If a transient technical operation fails, retry it safely for up to 3 times after the initial attempt, with delays of 5 seconds, 30 seconds, and 2 minutes. Do not assign business status `pending` solely because of technical failure.
- FR5.2 Every event is logged in the sidecar history: file hash, old key, new key, template, extracted fields, confidence, decision, status, timestamps.
- FR5.3 Objects missing from the bucket while not in a terminal state are detected by the consistency job and the record is flagged as orphaned.

### Epic 6 — Dashboard: file list

- FR6.1 A single list shows every file the system has seen, with columns: emission date, period, current object name, status, template (document family), document type, emitting company, and confidence where applicable. Served from SQLite.
- FR6.2 Filter by status, template, document type, emitting company, period, and date range; sort by any column; free-text search on object name, metadata fields, and the complete extracted text from every page of the PDF.
- FR6.3 Status is visually distinguishable (badge): `pending`, `processed`, `validated`.
- FR6.4 The list exposes a consistency indicator for known anomalies (orphaned objects, missing sidecars), without a separate statistics dashboard or aggregate KPI counters.

### Epic 7 — Dashboard: file detail page

- FR7.1 Clicking a file in the list opens a detail page.
- FR7.2 The detail page displays the PDF (embedded viewer), streamed from the bucket by the backend, alongside the extracted data.
- FR7.3 The user can change the template (dropdown of configured templates, showing the classifier's top candidates and scores).
- FR7.4 The detail page always shows the five common fields (template, emission date, emitting company, document type, period) plus the current template's optional fields; the user can correct editable values inline. Changing the template reloads the optional fields with the new template's schema and best-effort re-extraction, keeps the other common field values, and recomputes the period from the emission date and the selected rules. The period is read-only, calculated by the server, and never accepted as user input.
- FR7.5 A preview shows the target object key and destination prefix resulting from the current values, updating live as fields change.
- FR7.6 A validate action applies the rename/move (if not already done), sets the status to validated, and records the corrected values as training data.

### Epic 8 — Configuration file management

- FR8.1 Templates and extraction rules are defined in a JSONC configuration file; there is no separate template-management API or template registry endpoint.
- FR8.2 Adding or changing a template means editing the JSONC configuration file (including period granularity and offset) plus example files; no code change is required.
- FR8.3 The dashboard configuration editor retrieves the current JSONC file and uploads a modified version through dedicated configuration endpoints. The server validates the file before replacing the active configuration and reloading the in-memory template registry.

### Epic 9 — Retraining

- FR9.1 Manual "Retrain" action in the dashboard: trains on initial corpus + validated files, shows evaluation metrics (accuracy, confusion matrix) before activation.
- FR9.2 The user can activate or discard the new model. Only one model is active at a time; model files are versioned on disk.

### Epic 10 — Consistency and recovery

- FR10.1 A periodic consistency job verifies, for every sidecar: object exists at current key, sidecar in sync with SQLite; and for every PDF object: sidecar exists. Anomalies are listed in the dashboard with repair actions.
- FR10.2 Full SQLite rebuild from sidecars is available as a dashboard action and a CLI command.
- FR10.3 At startup, recover interrupted technical operations from recorded state. Re-scan `pending/` to reconcile metadata and detect anomalies, but do not automatically repeat classification or rename for documents awaiting manual correction.

## 6. Non-Functional Requirements

- NFR1 Privacy: fully offline on self-hosted infrastructure. No network calls except the local Garage bucket, and serving the local dashboard. Dependencies must not require downloads at runtime.
- NFR2 Performance: end-to-end processing of a standard PDF under 5 seconds on the target server, including the S3 download; dashboard file list loads under 1 s for 10,000 records (served from SQLite).
- NFR3 Reliability: zero silent wrong renames. Every ambiguous case routes to `pending`.
- NFR4 Durability: bucket versioning enabled; SQLite with WAL. Operations must be recoverable after crashes using copy-then-delete, atomic sidecar writes, idempotent retries, and consistency repair for SQLite-sidecar drift. For transient technical failures, perform at most 3 retries after the initial attempt, with delays of 5 seconds, 30 seconds, and 2 minutes. After the 3 retries are exhausted, stop processing that item; do not launch another retry or manual-review workflow, and do not change its business status to `pending` solely because of the technical failure.
- NFR5 Language: documents in French; UI in English or French (single locale acceptable in v1).
- NFR6 Deployment: runs as a container next to the Garage deployment and the Nextcloud server; configuration via a single JSONC file (templates) and environment variables (S3 endpoint, credentials, bucket).
- NFR7 Stack: Python (FastAPI, boto3 or minio S3 client, scikit-learn, pdfplumber/PyMuPDF, rapidfuzz, spaCy fr model optional), React (Vite, a lightweight PDF viewer such as react-pdf), SQLite, Garage for S3 storage, Nextcloud with External Storage (S3) as the user-facing file interface.
- NFR9 No encryption at rest in v1: documents are not sensitive; this decision is recorded explicitly and can be revisited.

## 7. Data Model

Sidecar JSON (source of truth), one per PDF at `{key}.meta.json`. The `sha256` field is the unique document identifier and must match the digest of the exact PDF bytes. Any byte-level change creates a new document record; a rename/move without content changes preserves the identifier. Identical PDF bytes map to the same record, so ingestion must detect an existing SHA-256 and handle it as a duplicate rather than create a second row.

- schema_version, sha256, original_key, current_key, status, template_name, confidence, emission_date, period, emitting_company, document_type, optional_fields (dict of additional field values), extracted_text (full text from all pages), created_at, updated_at, events[] (type: classified / renamed / validated / corrected, payload, timestamp).

SQLite replica (rebuilt from sidecars):

- files: sha256 (primary key), original_key, current_key, status, template_id, confidence, emission_date, period, emitting_company, document_type, optional_fields (JSON), created_at, updated_at, extracted_text.
- consistency_flags: id, sha256 (nullable reference to the affected PDF digest), kind (orphaned_object, missing_sidecar, drift).

## 8. API Surface (indicative)

- GET /files?status=&template=&q=&page= — list with filters (from SQLite).
- GET /files/{sha256} — detail: text, fields, candidates, current key.
- GET /files/{sha256}/pdf — stream the PDF from the bucket for the viewer.
- POST /files/{sha256}/validate — body: template_id, fields; applies rename if needed, sets validated.
- GET /config — retrieve the current JSONC configuration file.
- PUT /config — validate and save a replacement JSONC configuration file, then reload the configuration.
- POST /retrain — starts training and returns a generic job ID; GET /jobs/{job_id} reports status and result.
- GET /consistency — list anomalies; POST /consistency/rebuild — rebuild SQLite from sidecars.

## 9. Acceptance Criteria (v1)

- AC1 A new PDF dropped in `inbox/` via Nextcloud whose deterministic rules yield an unambiguous template and valid fields appears as processed within the poll interval, renamed and moved in the bucket.
- AC2 A PDF that cannot be classified unambiguously by deterministic rules, or has missing/invalid required fields, appears as `pending` and is moved to `pending/`; correcting and validating it on the detail page renames it and sets validated.
- AC3 Opening a processed file and validating without changes sets validated without re-renaming.
- AC4 The file list filters by status, template, document type, emitting company, period, and date; the consistency indicator matches the known anomalies.
- AC5 Changing the template on the detail page keeps the other common fields, reloads the optional fields per the new template schema, recomputes the read-only period per the new template's granularity and offset, and shows the live target key preview.
- AC5b Every file in every state carries the five common fields (template, emission date, emitting company, document type, period); a document whose fields cannot be recognized is routed to `pending` rather than renamed.
- AC5c Period computation per template rules: a document dated 2026-09-20 under a monthly template with offset current yields "September 2026"; under a quarterly template with offset next it yields "Q4 2026"; under a yearly template it yields "2026".
- AC6 Retraining uses the initial corpus + validated files, runs as a generic asynchronous job queried through `GET /jobs/{job_id}`, shows metrics when complete, and activation swaps the model without service restart.
- AC7 Editing and uploading the JSONC configuration (with example files) makes a new template available for classification and correction; the server validates the replacement before activating it, with no code change.
- AC8 Killing the service mid-processing leaves a recoverable state: startup recovery and the consistency job reconcile object keys, sidecars, and SQLite; retries do not duplicate renames or delete the only recoverable copy.
- AC9 Deleting the SQLite database entirely and running the rebuild command restores a fully functional dashboard from the sidecars alone.
- AC10 No network traffic other than the local Garage bucket, local Nextcloud, and local dashboard serving (verifiable with a firewall audit).
- AC11 1,000-file list view renders with filters in under 1 second.
- AC12 Nextcloud sees renamed and moved files at their new location without reconfiguration.

## 10. Release Plan

- v1 (this PRD): new files only, text-based French PDFs, three statuses, Garage S3 storage, sidecar source of truth with SQLite replica, Nextcloud as file interface, detail page with correction and validation, manual retrain, consistency job.
- v2 candidates: OCR for scanned documents, backlog ingestion of historical files, automatic periodic retraining, multilingual documents, automatic counterparty learning from validated files, bucket notification hooks to replace polling, at-rest encryption if the document mix becomes sensitive.

## 11. Open Questions

- Choice between pdfplumber and PyMuPDF (license and performance tradeoff) — decide in Week 1 spike.
- S3 client library: boto3 vs minio-py against Garage — covered by the Week 1 Garage compatibility spike (listing, copy-object, multipart, versioning behavior).
- Post-v1 ML decision: exact model confidence threshold and per-template overrides, to be calibrated on the initial corpus before enabling ML.
- Poll interval tradeoff between responsiveness and Garage listing load.
- Exact human-readable period formats (French month names, quarter notation) and their rendering in the object key — to be fixed in the template JSONC spec.


### ML-only inference contract

The runtime uses local model artifacts for emitting-company, document-type and emission-date extraction; additional-field-group selection from emitting company and document type; additional-field value extraction; and independent `template_name` and `period_format` prediction from emitting company and document type. Configuration may define template key patterns and period arithmetic parameters, but it must not select classification outcomes. Missing models, invalid predictions, or confidence below `CONFIDENCE_THRESHOLD` route the document to `pending`. The `period` and final storage key remain deterministic calculations after model inference.
