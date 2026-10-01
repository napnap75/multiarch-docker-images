# Project Brief — Automatic PDF Renamer

## 1. Vision

A personal, 100% offline tool that watches an S3 bucket hosted on a self-hosted Garage storage cluster, automatically identifies the type of each incoming PDF (invoice, purchase order, contract, etc.) from its content, applies the corresponding naming template by extracting the fields (date, counterparty, reference, amount), and moves the object into an organized S3 prefix structure. Users upload, download, and share files through Nextcloud, which mounts the bucket as external storage. Low-confidence decisions go through human review in a local web dashboard. Every operation is logged in JSON sidecar files (source of truth) and replicated to SQLite, and every rename is reversible.

## 2. Problem and Context

PDFs arrive continuously in an inbox S3 prefix (dropped via Nextcloud by the user or by upload automations). Current naming is manual or inconsistent, making documents hard to find and archive. The user owns 50 to 500 files already correctly named according to 1 to 5 existing templates, which will serve as training and reference corpus.

## 3. Objectives

- Classify each incoming PDF against a known naming template, based on the file content (first-page text, header/footer), not the current object name.
- Fill the fields of the selected template (date, counterparty, reference, amount) from the content.
- Rename and move the object within the bucket into an organized prefix structure depending on the document type.
- High confidence: automatic processing. Low confidence or incomplete extraction: queued for human review in the dashboard.
- Write metadata as JSON sidecar objects in the bucket (source of truth) and replicate every decision into SQLite for fast queries, with rollback capability and retraining from corrections.
- Run fully offline on self-hosted infrastructure: no cloud APIs, no data ever leaves the server.

## 4. Scope

### In scope (v1)

- Ingestion by polling the `inbox/` prefix of a Garage S3 bucket for new objects only. Nextcloud (External Storage app) is the user-facing interface to upload, download, and share files.
- PDF text extraction (pdfplumber or PyMuPDF). All files are text-based, no OCR in v1.
- Template classification: TF-IDF + Logistic Regression (scikit-learn) on the extracted text, with a configurable confidence threshold.
- Per-template slot filling: regex + dateutil for dates, amounts, references; gazetteer + fuzzy matching (rapidfuzz) for counterparties, with spaCy/CamemBERT NER as fallback if needed.
- Templates and extractors declared in configuration (YAML/JSON), not in code.
- Storage: Garage (self-hosted S3-compatible, https://garagehq.deuxfleurs.fr/) with a single versioned bucket; objects addressed by S3 keys, no local filesystem for documents.
- Metadata: one JSON sidecar object per PDF in the bucket, holding template, fields, confidence, status, and history — the source of truth. SQLite acts as a replicated index for dashboard queries, rebuildable from the sidecars.
- Local web dashboard: all processed files with status (not processed / processed / validated), file detail page with PDF preview streamed from S3, template and field correction, validation, history, statistics.
- On-demand classifier retraining, using the existing corpus plus every file with "validated" status.

### Out of scope (v1)

- OCR of scanned documents (planned for v2 if the mix changes).
- Backlog processing of existing files — new files only.
- Cloud S3 providers, remote language models, cloud APIs, telemetry.
- At-rest encryption: the documents are not sensitive; bucket and volumes are unencrypted in v1.
- Multi-user management, authentication on the dashboard (Nextcloud handles sharing and access control on files).
- Merging, splitting, or modifying PDF content.

## 5. User and Usage

Single user (personal use), technically proficient. Nominal flow:

1. A PDF is uploaded into the `inbox/` prefix via Nextcloud (or any S3 client).
2. The poller detects the new object, waits for write completion (stable ETag/content-length on repeated listing), downloads it, extracts the text, classifies, extracts the fields, validates.
3. High confidence and valid fields: the object is copied to its renamed key in the target prefix and the inbox copy deleted; metadata is written to the sidecar and replicated to SQLite; the event is logged.
4. Otherwise: the file appears in the dashboard review queue; the user corrects or validates; the correction feeds the training corpus.

## 6. Functional Requirements

- FR1: detect new PDFs in a configurable `inbox/` S3 prefix by polling the bucket listing at a configurable interval, ignoring non-PDF keys and temporary uploads (e.g. Nextcloud `.part` files).
- FR2: classify the document into one of the configured templates, with a confidence score and an "unknown" class below the threshold.
- FR3: fill each template field according to its schema (type, format, validity range).
- FR4: validate fields before renaming (parseable date, numeric amount, recognized or flagged counterparty); any failed validation routes to review.
- FR5: rename and move within the bucket via S3 copy + delete to the template's target prefix, resolving key collisions (incremental suffix).
- FR6: write a JSON sidecar per file (old key → new key → decision mapping, per-file status not processed / processed / validated) and replicate to SQLite, enabling rollback and selecting training data.
- FR7: local web dashboard (FastAPI + React): file list, detail page, review, correction, validation, history with undo, retraining trigger. PDFs are streamed from the bucket for preview.
- FR7a: the dashboard shows every processed file with its status: "not processed" (confidence was too low, rename pending), "processed" (automatically renamed and moved), or "validated" (the user confirmed or corrected the result — such files are eligible for future model retraining).
- FR7b: clicking a file opens a detail page displaying the PDF, where the user can change the template if misclassified, correct the extracted field values, and validate the result. Validation sets the status to "validated" and feeds the corrected data into the training corpus.
- FR8: adding a new template is done through a configuration entry (YAML) plus example files, with no code changes.

## 7. Non-Functional Requirements

- Privacy: 100% offline on self-hosted infrastructure (Garage). No data ever leaves the server.
- Encryption: none at rest in v1; documents are not sensitive. Network access to the bucket stays on the local network.
- Performance: a standard-size PDF processed in under a few seconds, including the S3 download.
- Reliability: zero silent invalid renames — every ambiguous case goes to review.
- Reversibility: every rename can be undone from the history (bucket versioning enabled; undo copies the previous version back if needed).
- Document language: French only in v1.
- Stack: Python (FastAPI) backend, boto3-compatible S3 client (Garage), React frontend, SQLite, scikit-learn, spaCy/fr, pdfplumber or PyMuPDF, rapidfuzz. Nextcloud with External Storage (S3) as the user-facing file interface.
- Portability: containerized deployment or systemd service on the user's server, alongside the Garage deployment.

## 8. Success Criteria

- ≥ 95% correct template classification on typical files after initial training on the existing corpus.
- ≥ 80% of files processed without human intervention (the rest in review, never a silent wrong rename).
- Zero non-reversible renames.
- Adding a new template in under 30 minutes without writing code.
- SQLite can be fully rebuilt from the bucket sidecars at any time.
- Working v1 in under one month.

## 9. Risks and Mitigations

- Limited training corpus (50–500 files): start simple (TF-IDF + LogReg), optionally sentence-transformers embeddings if accuracy is insufficient; the review loop continuously enriches the corpus.
- Templates too weakly distinguished in the text: enrich features (header, issuer, business keywords per template).
- Poorly extracted or empty text files: systematic detection and routing to review.
- Partial uploads (Nextcloud chunked uploads): poll-only keys with a stable size across two listing cycles and a `.part`/`.~lock` exclusion list.
- Sidecar/object drift (sidecar missing or out of sync with SQLite): sidecars win; a consistency job rebuilds SQLite and flags orphaned or missing sidecars.
- Garage version of S3 API: pin a Garage release and run a Week 1 compatibility spike (listing, multipart, copy-object, versioning).

## 10. Milestones

1. Week 1: text extraction, Garage bucket provisioning and S3 client spike, parsing the existing corpus into (template, fields), declarative template configuration.
2. Week 2: classifier + extractors + validation + sidecar/SQLite logging + inbox poller.
3. Week 3: React dashboard (file list with statuses, detail page with PDF preview and correction, validation, history, rollback, retraining) + Nextcloud external storage mounting.
4. Week 4: hardening (thresholds, collisions, consistency job, tests), deployment, production go-live on the bucket inbox.