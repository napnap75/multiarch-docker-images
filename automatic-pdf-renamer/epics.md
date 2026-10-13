---
project_name: "Automatic PDF Renamer"
document_type: "epics-and-stories"
version: "1.0"
status: "draft"
date: "2026-10-13"
inputDocuments:
  - "brief.md"
  - "prd.md"
  - "arch.md"
---

# Automatic PDF Renamer — Epics and Stories

## 1. Overview

### Product vision

Build a personal, fully offline application that watches a self-hosted Garage S3 bucket for new French text-based PDFs, extracts document data, classifies documents, applies configurable naming rules, and moves files into an organized bucket structure. This bucket is accessible through Nextcloud when the user wants to consult one file. A local dashboard lets the owner review and correct uncertain results. JSON sidecars are the source of truth; SQLite is a rebuildable query index.

### Product principles

- **Offline by design:** no cloud APIs, telemetry, or runtime downloads. Documents remain on self-hosted infrastructure.
- **No silent wrong renames:** ambiguous, incomplete, or invalid results must be routed for human review.
- **Recoverability:** renames are traceable and reversible; sidecars are authoritative and SQLite can be rebuilt from them.
- **Incremental delivery:** first deliver deterministic extraction with regular expressions and configured lookup lists; add ML as a separate capability without replacing the stable ingestion pipeline.
- **Configuration over code:** adding a document template should not require changing application code.
- **Single-user v1:** Nextcloud handles user-facing file access and sharing. Dashboard authentication and multi-user management are outside the v1 scope unless explicitly re-approved.

### Delivery strategy

1. Deliver the technical foundation and durable metadata model.
2. Deliver a complete, useful ingestion flow using deterministic rules only.
3. Add ML classification and ML-assisted extraction, training and model lifecycle management.
4. Deliver the dashboard for search, review, correction, validation, and template visibility.
5. Complete consistency checks, recovery, deployment hardening, and operational verification.

## 2. Requirements Inventory

### 2.1 Functional requirements

The following inventory consolidates the requirements from the project brief and PRD. The original FR identifiers are retained where practical for traceability.

- **FR-ING-01 — Polling and discovery:** poll a configurable Garage S3 `inbox/` prefix at a configurable interval; consider only PDFs and ignore temporary, hidden, and lock files.
- **FR-ING-02 — Upload stability:** process a PDF only after its size and ETag remain stable across consecutive listings.
- **FR-ING-03 — Restart recovery:** on startup, discover eligible inbox or pending objects that have not completed processing.
- **FR-TXT-01 — Text extraction:** extract all available text from every page of a PDF using a local library; no OCR in v1.
- **FR-TXT-02 — Empty text handling:** route PDFs with empty or insufficient extracted text to human review with an explicit reason.
- **FR-TXT-03 — Text persistence:** persist extracted text in the sidecar and SQLite replica so normal review and training workflows do not need to re-parse the PDF.
- **FR-CLS-01 — Classification:** classify documents against configured templates. The initial light release uses deterministic rules; the ML epic adds TF-IDF and Logistic Regression classification.
- **FR-CLS-02 — Confidence and unknown class (post-v1):** ML classification exposes confidence and routes results below a configurable threshold to review. The deterministic initial release uses explicit rule outcomes and does not generate artificial confidence scores.
- **FR-FLD-01 — Common fields:** every document has template, emission date, emitting company, document type, and period.
- **FR-FLD-02 — Optional fields:** templates define optional fields and reusable field groups with formats, allowed values, and extraction rules.
- **FR-FLD-03 — Deterministic extraction:** the light release uses configured regular expressions, date parsing, company gazetteers, and fuzzy matching where useful.
- **FR-FLD-04 — ML-assisted extraction:** the ML release can use trained models to help identify configured values where deterministic rules are insufficient.
- **FR-FLD-05 — Period computation:** derive period from emission date and template/company/document-type rules, including granularity, formatting, and offset.
- **FR-FLD-06 — Validation:** validate dates, companies, document types, periods, and optional field formats before applying a rename. Invalid or missing required values must be reviewed.
- **FR-STO-01 — Garage storage:** use a versioned Garage S3 bucket and the `inbox/`, `pending/`, and `files/` key areas.
- **FR-STO-02 — Sidecar source of truth:** store a JSON sidecar for each PDF with identifiers, current and original keys, status, extracted fields/text, confidence where applicable, timestamps, and event history.
- **FR-STO-03 — SQLite replica:** mirror sidecar data into SQLite for fast listing, filtering, search, and statistics.
- **FR-STO-04 — Safe moves:** move objects using copy-before-delete, resolve destination collisions with an incremental suffix, and record the actual final key.
- **FR-STO-05 — Reversible operations:** preserve sufficient history and key information to trace and reverse a rename without silently overwriting another object.
- **FR-DASH-01 — File list:** list all known files and show emission date, period, current key/name, status, template, document type, company, and confidence where available.
- **FR-DASH-02 — Search and filters:** filter by status, template, document type, company, period, and date range; sort columns and search object names and metadata, and full extracted text across every PDF page.
- **FR-DASH-03 — Statistics:** show counts by status, automation and validation rates, and consistency indicators.
- **FR-DASH-04 — PDF detail and preview:** stream the PDF from Garage through the backend and show it alongside extracted metadata.
- **FR-DASH-05 — Correction and validation:** allow the owner to change the template and field values, preview the resulting target key, and validate the final result.
- **FR-DASH-06 — Lifecycle:** support the business statuses `pending`, `processed`, and `validated`; validated records are eligible for training.
- **FR-TPL-01 — Template configuration:** define templates, fields, extraction and validation rules, period rules, destination prefixes, and key patterns in configuration.
- **FR-TPL-02 — Template discoverability:** show configured templates, their fields/rules, and corpus counts in the dashboard.
- **FR-ML-01 — Training corpus:** train from the initial labeled corpus and files validated by the owner.
- **FR-ML-02 — Evaluation:** show evaluation metrics, including accuracy and confusion matrix, before model activation.
- **FR-ML-03 — Model lifecycle:** version model artifacts, allow activation or discard, keep one active model, and activate without restarting the service.
- **FR-REC-01 — Consistency scan:** detect missing objects, missing sidecars, orphaned records, and sidecar/SQLite drift.
- **FR-REC-02 — Rebuild:** reconstruct SQLite from sidecars through both a CLI command and a dashboard/API action.
- **FR-REC-03 — Repair and resume:** expose anomalies and safe repair actions; re-scan pending work after restart.
- **FR-OPS-01 — Local deployment:** run the API, poller, dashboard, and background jobs in a container next to Garage and Nextcloud.
- **FR-OPS-02 — Nextcloud compatibility:** renamed/moved files remain visible at their new keys without reconfiguring the Nextcloud integration.

### 2.2 Non-functional requirements

- **NFR-01 Privacy:** no network calls except the local Garage bucket, local dashboard, and required local infrastructure. No runtime downloads or telemetry.
- **NFR-02 Performance:** process a standard PDF in under 5 seconds on the target server, including the S3 download. Dashboard list loads in under 1 second for 10,000 records; the PRD also calls out a 1,000-file acceptance check.
- **NFR-03 Reliability:** no silent invalid renames; ambiguous or invalid results are routed to review.
- **NFR-04 Durability:** bucket versioning is enabled; SQLite uses WAL; sidecar writes are atomic; crash recovery repairs replica drift.
- **NFR-05 Language:** French documents in v1; the UI may use one locale, English or French.
- **NFR-06 Deployment/configuration:** containerized deployment, environment variables for secrets and endpoints, and a single documented template configuration format.
- **NFR-07 Maintainability:** Python/FastAPI, Garage-compatible S3 client, SQLite, React dashboard, local PDF text extraction, and scikit-learn for ML.
- **NFR-08 Testing:** unit tests plus targeted integration/end-to-end tests for Garage behavior, recovery, offline operation, and critical user journeys.
- **NFR-09 Encryption:** at-rest encryption is explicitly out of scope for v1; document the decision and revisit if the data sensitivity changes.

### 2.3 Additional requirements and planning decisions

- Existing correctly named documents provide the initial labeled corpus; v1 processes new incoming files only, not the historical backlog.
- OCR, scanned PDFs, multilingual documents, cloud services, and automatic periodic retraining are out of scope for v1.
- The three business statuses must not be overloaded with transient technical states. Processing failures and recovery steps need separate technical flags/reasons.
- S3 object operations, sidecar updates, and SQLite writes do not form one distributed transaction. Each state transition must be recoverable and verifiable rather than relying on a claim of cross-system atomicity.

## 3. Functional Requirements Coverage Map

| Requirement group | Covered by epic(s) |
|---|---|
| FR-ING-01 to FR-ING-03: discovery, stability, restart scan | Epic 2; recovery aspects in Epic 5 |
| FR-TXT-01 to FR-TXT-03: text extraction and persistence | Epic 2 |
| FR-CLS-01 to FR-CLS-02: rules-first classification and ML confidence | Epics 2 and 3 |
| FR-FLD-01 to FR-FLD-06: fields, deterministic/ML extraction, periods, validation | Epics 2 and 3; review overrides in Epic 4 |
| FR-STO-01 to FR-STO-05: S3, sidecars, SQLite, safe and reversible moves | Epics 1 and 2; recovery in Epic 5 |
| FR-DASH-01 to FR-DASH-06: list, search, preview, correction, validation, lifecycle | Epic 4 |
| FR-TPL-01 to FR-TPL-02: templates and template visibility | Epics 2 and 4 |
| FR-ML-01 to FR-ML-03: training, evaluation, model lifecycle | Epic 3 |
| FR-REC-01 to FR-REC-03: consistency, rebuild, repair, resume | Epic 5 |
| FR-OPS-01 to FR-OPS-02: deployment and Nextcloud compatibility | Epics 1 and 5 |
| NFR-01, NFR-05 to NFR-09: privacy, language, configuration, maintainability, testing, encryption decision | All epics; verification primarily in Epic 5 |
| NFR-02 to NFR-04: performance, correctness, durability | Epics 1, 2, 4, and 5 |

## 4. Epic List

1. **Technical Foundation, Storage, and Metadata** — establish the offline service, Garage access, sidecar schema, and rebuildable SQLite index.
2. **Rules-Based PDF Ingestion and Classification (Light Release)** — deliver a complete deterministic pipeline using text extraction, regular expressions, configured lookup data, validation, and safe renaming; no ML.
3. **Machine Learning for PDF Ingestion** — add local model training and inference for document classification and configured-value extraction, integrated with the existing pipeline.
4. **Dashboard, Human Review, and Template Management** — make results searchable and correctable, support PDF preview and validation, and expose configured templates.
5. **Consistency, Recovery, and Operational Readiness** — detect and repair drift, recover safely from interruptions, verify offline behavior and performance, and document deployment/operations.

---

## Epic 1: Technical Foundation, Storage, and Metadata

**Goal:** Provide the reliable offline foundation required by all user-facing workflows. PDF objects and sidecars live in Garage; SQLite is a rebuildable replica, never the authority.

**Requirements covered:** FR-STO-01 to FR-STO-03, FR-OPS-01, NFR-01, NFR-04, NFR-06 to NFR-09.

### Story 1.1: Run the service in a local container

As the owner,
I want the application to run as a container alongside Garage and Nextcloud,
So that I can deploy and operate the tool on my own infrastructure without cloud services.

**Acceptance Criteria:**

**Given** the required local configuration and Garage endpoint are available  
**When** I start the application container  
**Then** the FastAPI service starts and exposes its local API and static dashboard  
**And** startup does not download models, packages, or other assets from the internet  
**And** missing or invalid configuration is reported clearly without exposing secrets.

### Story 1.2: Connect to Garage and define storage key conventions

As the application,
I want a configured S3-compatible storage adapter and consistent key conventions,
So that every ingestion and file operation uses the same Garage interface.

**Acceptance Criteria:**

**Given** valid Garage credentials, endpoint, and bucket configuration  
**When** the application performs a storage health check  
**Then** it can list, inspect, read, write, copy, and delete objects using the supported Garage API  
**And** key helpers consistently recognize the `inbox/`, `pending/`, and `files/` areas  
**And** credentials are supplied through environment variables and are never written into sidecars or logs.

### Story 1.3: Persist authoritative PDF sidecars

As the application,
I want a versioned JSON sidecar for every managed PDF,
So that processing decisions and file history survive restarts and can be audited.

**Acceptance Criteria:**

**Given** a PDF is first accepted for processing  
**When** its metadata record is created  
**Then** the sidecar records schema version, SHA-256, original key, current key, status, template and extracted fields when known, extracted text, timestamps, and event history  
**And** subsequent state changes append a timestamped event and update the current state  
**And** sidecar writes use a temporary key and a safe replacement procedure rather than exposing partially written JSON  
**And** the sidecar remains the authoritative record when SQLite disagrees.

### Story 1.4: Mirror sidecar data into SQLite

As the owner,
I want sidecar metadata indexed in SQLite,
So that dashboard queries and statistics remain fast without making SQLite authoritative.

**Acceptance Criteria:**

**Given** a sidecar is created or updated  
**When** the replica synchronization completes  
**Then** the corresponding SQLite record reflects its queryable metadata  
**And** SQLite uses WAL mode and a versioned schema/migration mechanism  
**And** a replica failure is logged and can be repaired from the sidecar without losing the authoritative record.

### Story 1.5: Rebuild the SQLite index from sidecars

As the owner,
I want to recreate SQLite from the bucket's sidecars,
So that a damaged or deleted local database does not require reprocessing the PDF files.

**Acceptance Criteria:**

**Given** the SQLite database is missing or unusable and sidecars remain available in Garage  
**When** the rebuild operation is run  
**Then** SQLite is recreated from the sidecars, including statuses, fields, extracted text, and event-derived metadata available in the schema  
**And** the rebuilt index supports the same core list and detail queries as before  
**And** malformed or unreadable sidecars are reported rather than silently discarded.

---

## Epic 2: Rules-Based PDF Ingestion and Classification (Light Release)

**Goal:** Deliver a complete end-to-end workflow for new PDFs using deterministic rules only. The application polls Garage, extracts text from every PDF page, fills fields with regexes and configured lookup data, validates the result, and safely renames or routes the file for review. This epic must not depend on a machine-learning model.

**Requirements covered:** FR-ING-01 to FR-ING-03, FR-TXT-01 to FR-TXT-03, deterministic part of FR-CLS-01, FR-FLD-01 to FR-FLD-03 and FR-FLD-05 to FR-FLD-06, FR-STO-04 to FR-STO-05, FR-TPL-01, FR-OPS-02, NFR-02 to NFR-04.

### Story 2.1: Discover stable incoming PDF files

As the owner,
I want the application to detect complete PDFs in the configured inbox,
So that newly uploaded documents enter the pipeline automatically.

**Acceptance Criteria:**

**Given** a PDF is uploaded to the configured `inbox/` prefix  
**When** its size and ETag are unchanged across two consecutive listings  
**Then** it becomes eligible for processing  
**And** non-PDF files, hidden files, `.part` files, and lock files are ignored  
**And** a file that is still being uploaded is not eligible for processing prematurely  
**And** a poller error for one object does not stop discovery of other objects.

### Story 2.2: Extract and persist full PDF text

As the application,
I want to extract and persist all available text from every page of each eligible PDF,
So that downstream classification and field extraction use the document's content.

**Acceptance Criteria:**

**Given** an eligible PDF contains extractable text  
**When** the extraction step runs  
**Then** the configured local PDF library extracts text from every page of the PDF  
**And** the text is saved in the sidecar and mirrored to SQLite  
**And** the application does not call an external OCR or document-processing service  
**And** if the text is empty or below the configured minimum length, the file is routed to review with reason `empty text`.

### Story 2.3: Define templates and deterministic document rules

As the owner,
I want each template to declare its fields, extraction rules, validation rules, period rules, and destination key pattern,
So that I can adapt document handling without changing application code.

**Acceptance Criteria:**

**Given** a valid template configuration is loaded  
**When** the template registry starts or reloads  
**Then** each template exposes its required common fields, optional fields, regex rules, company/document-type mappings, period rules, target prefix, and key pattern  
**And** invalid configuration is rejected with a useful error and does not partially replace the active configuration  
**And** template configuration contains no credentials or secrets  
**And** the configuration format is the project-wide format selected in the Additional Requirements section.

### Story 2.4: Extract common and optional fields using rules

As the application,
I want to fill document fields using configured regular expressions and reference data,
So that the light release can classify and name common document types without ML.

**Acceptance Criteria:**

**Given** full-document extracted text and a configured set of templates are available  
**When** deterministic extraction runs  
**Then** it extracts candidate values for emission date, emitting company, document type, and configured optional fields using the applicable regexes and lookup lists  
**And** date parsing uses a local date parser and company matching uses the configured gazetteer, with fuzzy matching only where configured  
**And** the selected template and all extracted candidates are recorded in metadata  
**And** fields that cannot be extracted are left explicitly missing rather than guessed.

### Story 2.5: Compute periods and validate the extracted data

As the application,
I want to compute the configured period and validate all required fields before renaming,
So that only internally consistent metadata can drive an automatic move.

**Acceptance Criteria:**

**Given** an emission date and the applicable company/document-type period rules are available  
**When** period computation runs  
**Then** the period is derived using the configured granularity, format, and offset  
**And** the implementation supports the PRD granularities day, week, month, quarter, semester, and year, and offsets current, previous, and following  
**And** validation checks date parseability and plausible range, recognized company, allowed document type, period consistency, and configured optional-field formats  
**And** any missing or invalid required value routes the document to review with the failing fields and reasons recorded  
**And** no invalid result is automatically renamed.

### Story 2.6: Route files and apply safe, traceable renames

As the owner,
I want valid documents to be renamed and moved automatically while uncertain documents wait for review,
So that routine files are organized without risking silent data loss.

**Acceptance Criteria:**

**Given** all required fields are valid and the deterministic rules produce a target key  
**When** the pipeline completes  
**Then** the object is copied to the target key before the source is deleted  
**And** destination collisions are resolved with an incremental suffix and the final key is recorded  
**And** the sidecar records the original key, current key, extracted values, decision, status, and rename events  
**And** the file receives status `processed` only after the required state updates have been persisted or recoverably recorded  
**And** when confidence is not applicable in this rules-only release, the decision is based on explicit rule outcomes and field validation rather than a fabricated ML confidence score  
**And** ambiguous, incomplete, or invalid files are moved to `pending/` and receive status `pending` with a review reason  
**And** the operation can be audited and safely reversed without overwriting another object.

### Story 2.7: Make ingestion idempotent and restart-safe

As the owner,
I want ingestion to resume safely after interruptions,
So that retries do not create duplicate processing or lose track of a file.

**Acceptance Criteria:**

**Given** the service restarts while files exist in `inbox/` or `pending/`  
**When** the recovery scan runs  
**Then** objects without a completed sidecar state are discovered and resumed or flagged for review  
**And** objects already completed are not processed again in a way that creates duplicate renames or events  
**And** if a crash occurs between copy and delete, the recovery logic detects both keys and resolves the state from the sidecar and object hashes  
**And** transient technical failures are distinguishable from the three business statuses.

---

## Epic 3: Machine Learning for PDF Ingestion

**Goal:** Improve document classification and configured-value extraction with locally trained models, while retaining the deterministic pipeline and human-review safety net. ML must be an optional capability layered behind stable classification and extraction interfaces.

**Requirements covered:** ML extension of FR-CLS-01 and FR-CLS-02, FR-FLD-04, FR-ML-01 to FR-ML-03, FR-DASH-05 training feedback, NFR-01 to NFR-04, NFR-07 to NFR-08.

### Story 3.1: Build a labeled training corpus from validated documents

As the owner,
I want initial labeled examples and validated documents to form the training corpus,
So that the model learns from known document types and my corrections.

**Acceptance Criteria:**

**Given** the initial labeled corpus and sidecars for validated files are available  
**When** the training dataset is assembled  
**Then** eligible samples are mapped to their validated template/document labels and relevant field labels  
**And** unvalidated or inconsistent records are excluded unless explicitly selected through a documented policy  
**And** duplicate content is detected by hash to avoid unintended train/test leakage  
**And** the dataset build is reproducible and records which samples were used.

### Story 3.2: Train and evaluate a local document classifier

As the owner,
I want to train a local classifier and review its quality before using it,
So that ML-based classification is measurable rather than blindly trusted.

**Acceptance Criteria:**

**Given** a valid training corpus contains enough labeled examples  
**When** I trigger model training  
**Then** a TF-IDF plus Logistic Regression classifier is trained locally  
**And** evaluation metrics include accuracy and a confusion matrix using a held-out evaluation set where the dataset permits it  
**And** the training run records timestamp, dataset/model version, metrics, and warnings about insufficient or imbalanced data  
**And** training failure leaves the currently active model unchanged  
**And** no runtime or training data is sent to external services.

### Story 3.3: Use ML predictions during ingestion

As the owner,
I want the ingestion pipeline to use the active classifier to propose a document template,
So that documents whose layouts or wording vary can be recognized more reliably.

**Acceptance Criteria:**

**Given** an active model is available and text extraction succeeds  
**When** a PDF enters classification  
**Then** the classifier returns ranked template candidates and scores  
**And** a configurable threshold determines whether the top prediction is eligible for automatic processing  
**And** a low-confidence or unknown prediction is routed to human review with the candidates and scores stored in metadata  
**And** the pipeline can fall back to deterministic rules when the model is unavailable or disabled  
**And** downstream field validation and safe rename rules remain mandatory regardless of classifier confidence.

### Story 3.4: Add ML-assisted extraction for configured field groups

As the owner,
I want ML to help select values from configured field groups when regex extraction is insufficient,
So that the application can resolve more document-specific values without hard-coded per-document code.

**Acceptance Criteria:**

**Given** a template maps a period format/emitting company/document type/additional fields to a predefined field group with allowed values  
**When** ML-assisted extraction is enabled for that field  
**Then** the extractor proposes a value from the configured candidate set and records the method and score where available  
**And** regex and deterministic lookup results remain available as inputs or fallback paths  
**And** values outside the configured schema are never accepted automatically  
**And** low-confidence, contradictory, or invalid results are routed to review  
**And** the selected value and its provenance are persisted in the sidecar.

### Story 3.5: Version, activate, discard, and roll back models

As the owner,
I want to control which trained model is active,
So that model improvements can be adopted safely and reversed if quality regresses.

**Acceptance Criteria:**

**Given** a completed training run has produced a candidate model and metrics  
**When** I activate the candidate  
**Then** the model artifacts are stored in a versioned local registry and the active-model pointer is switched atomically without restarting the service  
**And** only one model version is active at a time  
**And** I can discard a candidate without affecting the active model  
**And** I can restore a previous version  
**And** each file's metadata records the model version used for its prediction when ML was enabled.

---

## Epic 4: Dashboard, Human Review, and Template Management

**Goal:** Give the owner a local interface to find documents, understand automated decisions, correct metadata, validate files, and inspect template configuration. The dashboard is the human safety net for both the rules-only and ML-enabled ingestion paths.

**Requirements covered:** FR-DASH-01 to FR-DASH-06, FR-TPL-01 to FR-TPL-02, review aspects of FR-FLD-06, FR-ML-01, NFR-02, NFR-05.

### Story 4.1: Browse and filter all known documents

As the owner,
I want a searchable list of all files the application has seen,
So that I can find documents and understand their processing status.

**Acceptance Criteria:**

**Given** file records are present in SQLite  
**When** I open the dashboard file list  
**Then** each row shows emission date, period, current object name/key, status, template, document type, emitting company, and confidence where available  
**And** I can filter by status, template, document type, company, period, and date range  
**And** I can sort by supported columns and search object names, metadata fields, and the full extracted text across all pages  
**And** status badges distinguish `pending`, `processed`, and `validated`  
**And** the list is served from SQLite rather than performing a full S3 scan for every request.

### Story 4.2: Show useful statistics and consistency indicators

As the owner,
I want summary counts and automation statistics,
So that I can monitor whether the system is working as expected.

**Acceptance Criteria:**

**Given** the indexed file records and known consistency flags are available  
**When** I view the dashboard summary  
**Then** it shows counts by business status, automation rate, validation rate, and known consistency anomalies  
**And** the displayed counts are derived from the same indexed records as the file list  
**And** unavailable or stale statistics are indicated rather than presented as current.

### Story 4.3: Inspect a PDF and its processing decision

As the owner,
I want to open a file's detail page with its PDF and extracted metadata,
So that I can understand and correct the system's decision.

**Acceptance Criteria:**

**Given** a file appears in the list  
**When** I open its detail page  
**Then** the page displays the PDF streamed from Garage through the backend alongside its extracted fields  
**And** it displays the current status, metadata, extracted text, decision/review reason, and ML candidates/scores when available  
**And** a missing or unavailable PDF is reported clearly without breaking the rest of the dashboard.

### Story 4.4: Correct metadata and preview the target key

As the owner,
I want to change a template and edit extracted fields before validation,
So that the final file name and destination reflect the correct document data.

**Acceptance Criteria:**

**Given** I am viewing a document detail page  
**When** I change the template  
**Then** the page keeps the five common fields, reloads the optional fields for the selected template, and performs best-effort re-extraction without silently overwriting user edits  
**And** the period is recomputed from the emission date and the new template rules but remains editable  
**And** the target key and destination prefix update as I edit fields  
**And** invalid or missing required fields are highlighted and prevent final validation until corrected or explicitly handled by a defined rule.

### Story 4.5: Validate a file and preserve the correction history

As the owner,
I want to confirm or correct a file's metadata and validate the result,
So that the file is moved to its final location and my correction can improve future processing.

**Acceptance Criteria:**

**Given** I have reviewed a document and its required fields are valid  
**When** I select Validate  
**Then** the application applies the safe rename/move if the target key changed and sets the business status to `validated`  
**And** if the key did not change, validation does not perform an unnecessary rename  
**And** the corrected values and validation event are written to the sidecar before SQLite is synchronized  
**And** the validated record becomes eligible for the ML training corpus  
**And** the operation is idempotent and its history remains auditable.

### Story 4.6: Inspect configured templates and their corpus coverage

As the owner,
I want to see the configured templates, their rules, and the number of associated examples,
So that I can understand and maintain the document taxonomy.

**Acceptance Criteria:**

**Given** a valid template registry is loaded  
**When** I open the template view  
**Then** I can see each template's fields, period rules, target prefix, key pattern, and extraction/validation configuration  
**And** the view shows the number of corpus files associated with each template  
**And** configuration errors are surfaced with actionable messages  
**And** a new template becomes available to classification and the detail-page selector after valid configuration is loaded, without code changes.

---

## Epic 5: Consistency, Recovery, and Operational Readiness

**Goal:** Make the application diagnosable and safe to operate by detecting storage/index inconsistencies, providing recoverable repair paths, and verifying offline, performance, and deployment requirements.

**Requirements covered:** FR-REC-01 to FR-REC-03, FR-OPS-01 to FR-OPS-02, NFR-01 to NFR-04, NFR-06, NFR-08 to NFR-09.

### Story 5.1: Scan for storage and metadata inconsistencies

As the owner,
I want a consistency job to compare PDF objects, sidecars, and SQLite,
So that missing or divergent records are detected before they cause silent errors.

**Acceptance Criteria:**

**Given** the bucket and SQLite contain managed records  
**When** a consistency scan runs on schedule or on demand  
**Then** it detects PDFs without sidecars, sidecars whose current object is missing, orphaned records, and sidecar/SQLite drift  
**And** each anomaly is recorded with its type, affected key/record, detection time, and a suggested safe action  
**And** the scan does not silently delete or overwrite data to make the stores appear consistent.

### Story 5.2: Repair or rebuild inconsistent state safely

As the owner,
I want safe repair actions for detected anomalies,
So that I can restore a consistent index without risking document loss.

**Acceptance Criteria:**

**Given** consistency anomalies are available  
**When** I request an available repair or a full SQLite rebuild  
**Then** repairs follow the sidecar-as-source-of-truth rule and record an event or diagnostic result  
**And** SQLite can be rebuilt from sidecars using both a CLI command and the dashboard/API  
**And** ambiguous cases that cannot be repaired safely are flagged for manual investigation  
**And** repair actions are repeatable and do not duplicate objects or overwrite unrelated files.

### Story 5.3: Resume interrupted processing after restart

As the owner,
I want the application to recover work interrupted by a crash,
So that files are not stranded in an unknown state.

**Acceptance Criteria:**

**Given** the service starts with objects in `inbox/` or `pending/` and possibly incomplete prior operations  
**When** the startup recovery scan runs  
**Then** it identifies files without a completed processing state and resumes or flags them for review  
**And** it detects copy-before-delete intermediate states using sidecar history and object identity  
**And** it repairs SQLite drift from authoritative sidecars  
**And** it never deletes the only known copy of a PDF as an unverified recovery shortcut.

---

## 5. Cross-Epic Definition of Done

A story is complete only when all applicable items below are satisfied:

- Acceptance criteria are implemented and verified with automated tests where practical.
- Errors and ambiguous outcomes are explicit; no invalid document is silently renamed.
- Metadata changes follow the sidecar-first, SQLite-replica model and can be recovered.
- No external network dependency is introduced.
- Configuration, logs, and documentation are updated where behavior changes.
- The change does not break the rules-only pipeline when ML is unavailable or disabled.
- Any architecture or requirement discrepancy discovered during implementation is documented and resolved before the affected behavior is considered complete.

## 6. Release Boundaries

### Release A — Rules-based light release

Includes Epics 1 and 2 plus the minimum dashboard capabilities from Epic 4 needed to inspect, correct, and validate pending files. It must provide a working end-to-end workflow without ML.

### Release B — ML-assisted ingestion

Adds Epic 3 and its integration with the review workflow. ML is an enhancement, not a hard dependency for storage, review, or safe rename behavior.

### Release C — Operationally complete v1

Completes the remaining Epic 4 capabilities and Epic 5, including consistency repair, rebuild, recovery, offline verification, and deployment documentation.

## 7. Open Decisions to Resolve Before the Affected Stories

1. **Reversibility semantics:** define the user-facing undo operation, collision behavior, and exact state transitions before implementing the final rollback workflow.
2. **ML field extraction:** specify which field groups use ML in v1 and what evaluation threshold is required before enabling each extractor. Classification and deterministic validation must remain usable independently.
