# PRD — Automatic PDF Renamer

Product Requirements Document. Derived from the project brief (BMAD). Personal tool, single user, 100% offline.

## 1. Product Overview

A server-side service with a local web dashboard that watches a folder for incoming PDFs, identifies the document type from its content, applies the matching naming template, fills the template fields, renames and moves the file into an organized folder tree, and tracks every file through a three-state lifecycle (not processed / processed / validated). Low-confidence or invalid files wait for human correction; validated files feed future model retraining.

## 2. Personas

Single user: the owner. Technically proficient, wants minimal manual work but zero silent errors.

## 3. Document Lifecycle and Statuses

| Status | Meaning |
|---|---|
| not processed | Classification confidence below threshold, or field validation failed. File untouched in the watched folder (or a pending subfolder), waiting for review. |
| processed | Automatically classified, fields validated, renamed and moved. Eligible for undo. |
| validated | The user reviewed the result (confirmed or corrected template/fields). Eligible for model retraining. Terminal state for the review flow; the file was renamed and moved. |

Transitions:

- new file → not processed (low confidence or invalid fields)
- new file → processed (high confidence, valid fields)
- not processed → validated (user corrects and validates in the detail page; rename then applied)
- processed → validated (user opens the detail page and confirms; no changes needed)
- processed or validated → previous state restorable via undo (rollback of the rename)

## 4. Functional Requirements

### Epic 1 — Ingestion and watching

- FR1.1 Watch a configurable folder for new PDF files only. Ignore other extensions and temporary files (e.g. `.tmp`, partial downloads).
- FR1.2 Wait for file stability (size unchanged over a configurable interval) before processing.
- FR1.3 On service restart, detect PDFs that arrived while the service was down but were never processed (no record in the database).
- FR1.4 Configurable poll interval or OS-level file system events (watchdog).

### Epic 2 — Text extraction

- FR2.1 Extract text with pdfplumber or PyMuPDF (choice fixed at implementation, documented in config).
- FR2.2 Extract the full first page plus header and footer zones of all pages.
- FR2.3 If extracted text is empty or below a minimum length, route the file to not processed with reason "empty text".
- FR2.4 Store the extracted text in the database for the file's lifetime (enables detail page display and retraining without re-parsing).

### Epic 3 — Template classification

- FR3.1 Classify the document into one of the configured templates using TF-IDF + Logistic Regression on the extracted text.
- FR3.2 Output a confidence score per template; the top score below a configurable threshold (default 0.8) yields "unknown" and routes the file to not processed with reason "low confidence".
- FR3.3 Display the top-N template candidates with scores on the detail page.
- FR3.4 Retrain the classifier on demand from: the initial corpus + all files with validated status. Retraining is a manual action from the dashboard, with the training date and resulting evaluation metrics stored and displayed.

### Epic 4 — Document data model, slot filling and validation

#### 4.1 Common fields (mandatory for every document)

Every document, regardless of template, carries at least these four common fields:

- Common field 1 — Template: the selected template (i.e. the document family: invoice, purchase order, contract, bank statement, rent receipt, etc.).
- Common field 2 — Emission date: the date the document was emitted, extracted from its content (not the file modification date).
- Common field 3 — Emitting company: the company or organization that emitted the document, matched against the counterparty gazetteer.
- Common field 4 — Document type: the fine-grained type of document within the family (e.g. debit advice vs monthly statement for bank statements; quotes vs order confirmations for purchase documents).

The filename pattern of each template is composed from these common fields, plus the template's optional fields.

#### 4.2 Optional fields (template-specific)

- FR4.1 Each template declares in YAML its optional fields beyond the common four: name, type, format, extraction rules, validation rules, and whether the field is rendered in the filename, the target folder path, or stored only. Examples: bank statements carry account owner and statement type; rent receipts carry the concerned premises; contracts carry the counterparty signatory.
- FR4.2 Fill common and optional fields using per-template regexes, dateutil for dates, and a counterparty gazetteer with rapidfuzz matching for the emitting company (similarity threshold configurable; below threshold the company is "unrecognized").
- FR4.3 Validate fields before renaming: emission date parses and is within a plausible range, emitting company recognized, document type among the template's allowed values, each optional field matches its declared format (empty optional fields are allowed). Any failure routes to not processed with the failing fields identified.
- FR4.4 Collisions in the target folder are resolved with an incremental suffix; the final name is always the one logged.

### Epic 5 — Renaming, moving, logging, undo

- FR5.1 Rename and move the file to the template's target folder according to its filename pattern.
- FR5.2 Every event is logged in SQLite: file hash, old path, new path, template, extracted fields, confidence, decision, status, timestamps.
- FR5.3 Undo restores the previous path and sets the status back to its prior value, keeping the full audit trail.
- FR5.4 Deleting or moving a file on disk that is not in a terminal state is detected and the record is flagged as orphaned.

### Epic 6 — Dashboard: file list

- FR6.1 A single list shows every file the system has seen, with columns: emission date, current filename, status, template (document family), document type, emitting company, confidence.
- FR6.2 Filter by status, template, document type, emitting company, and date range; sort by any column; free-text search on filename and emitting company.
- FR6.3 Status is visually distinguishable (color or badge): not processed, processed, validated.
- FR6.4 The list shows counters: files per status, automation rate (processed / total), validation rate.

### Epic 7 — Dashboard: file detail page

- FR7.1 Clicking a file in the list opens a detail page.
- FR7.2 The detail page displays the PDF (embedded viewer) alongside the extracted data.
- FR7.3 The user can change the template (dropdown of configured templates, showing the classifier's top candidates and scores).
- FR7.4 The detail page always shows the four common fields (template, emission date, emitting company, document type) plus the current template's optional fields; the user can correct each value inline; changing the template reloads the optional fields with the new template's schema and best-effort re-extraction, keeping the common field values.
- FR7.5 A preview shows the target filename and destination folder resulting from the current values, updating live as fields change.
- FR7.6 A validate action applies the rename/move (if not already done), sets the status to validated, and records the corrected values as training data.
- FR7.7 An undo action (for processed or validated files) rolls back the rename.

### Epic 8 — Template management

- FR8.1 Templates are defined in YAML configuration files, hot-reloaded without service restart.
- FR8.2 Adding a template = YAML entry + example files; no code change.
- FR8.3 The dashboard lists configured templates with their slot definitions and the count of corpus files per template.

### Epic 9 — Retraining

- FR9.1 Manual "Retrain" action in the dashboard: trains on initial corpus + validated files, shows evaluation metrics (accuracy, confusion matrix) before activation.
- FR9.2 The user can activate or discard the new model. Only one model is active at a time; model files are versioned on disk.

## 5. Non-Functional Requirements

- NFR1 Privacy: fully offline. No network calls except serving the local dashboard. Dependencies must not require downloads at runtime.
- NFR2 Performance: end-to-end processing of a standard PDF under 5 seconds on the target server; dashboard file list loads under 1 s for 10,000 records.
- NFR3 Reliability: zero silent wrong renames. Every ambiguous case routes to not processed.
- NFR4 Durability: SQLite with WAL; a crash at any point leaves no half-renamed state (rename is atomic on the same filesystem; cross-filesystem moves use copy+verify+delete).
- NFR5 Reversibility: every rename has an undo.
- NFR6 Language: documents in French; UI in English or French (single locale acceptable in v1).
- NFR7 Deployment: runs as a systemd service or container; configuration via a single YAML file and environment variables.
- NFR8 Stack: Python (FastAPI, watchdog, scikit-learn, pdfplumber/PyMuPDF, rapidfuzz, spaCy fr model optional), React (Vite, a lightweight PDF viewer such as react-pdf), SQLite.

## 6. Data Model (indicative)

- files: id, sha256, original_path, current_path, status, template_id, confidence, emission_date, emitting_company, document_type, created_at, updated_at, extracted_text.
- fields: id, file_id, slot_name, value, is_common (bool), was_corrected (bool), validated_by.
- events: id, file_id, type (classified, renamed, validated, undone), payload_json, created_at.
- templates: mirrored from YAML at load: id, name, document_types, pattern, target_folder, common_fields_json, optional_fields_json.
- models: id, trained_at, metrics_json, active (bool).

## 7. API Surface (indicative)

- GET /files?status=&template=&q=&page= — list with filters.
- GET /files/{id} — detail: text, fields, candidates, current path.
- GET /files/{id}/pdf — stream the PDF for the viewer.
- POST /files/{id}/validate — body: template_id, fields; applies rename if needed, sets validated.
- POST /files/{id}/undo — rolls back the rename.
- GET /templates — configured templates.
- POST /retrain — starts training; GET /retrain/status; POST /retrain/{id}/activate.

## 8. Acceptance Criteria (v1)

- AC1 A new PDF with high-confidence classification and valid fields appears as processed within the poll interval, renamed and moved correctly, and is undoable.
- AC2 A PDF below the confidence threshold appears as not processed; correcting and validating it on the detail page renames it and sets validated.
- AC3 Opening a processed file and validating without changes sets validated without re-renaming.
- AC4 The file list filters by status, template, document type, emitting company, and date; counters match the data.
- AC5 Changing the template on the detail page keeps the common fields, reloads the optional fields per the new template schema, and shows the live target filename preview.
- AC5b Every file in every state carries the four common fields (template, emission date, emitting company, document type); a document whose emitting company cannot be recognized is routed to not processed rather than renamed.
- AC6 Retraining uses initial corpus + validated files, shows metrics, and activation swaps the model without service restart.
- AC7 Adding a template via YAML (with example files) makes it available for classification and in the detail page dropdown, with no code change and no restart.
- AC8 Killing the service mid-processing leaves no half-renamed file; restart recovers and re-processes pending files.
- AC9 No network traffic other than local dashboard serving (verifiable with a firewall audit).
- AC10 1,000-file list view renders with filters in under 1 second.

## 9. Release Plan

- v1 (this PRD): new files only, text-based French PDFs, three statuses, detail page with correction and validation, manual retrain, undo.
- v2 candidates: OCR for scanned documents, backlog ingestion of historical files, automatic periodic retraining, multilingual documents, automatic counterparty learning from validated files.

## 10. Open Questions

- Choice between pdfplumber and PyMuPDF (license and performance tradeoff) — decide in Week 1 spike.
- Whether not-processed files stay in the watched folder or move to a pending subfolder (recommendation: pending subfolder to avoid re-processing loops).
- Exact confidence threshold and per-template overrides — calibrate on the initial corpus.
