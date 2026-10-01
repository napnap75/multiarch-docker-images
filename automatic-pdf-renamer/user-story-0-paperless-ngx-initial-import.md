# User Story 0 — Paperless-ngx Initial Import

BMAD user story. Precedes v1 epics; provides the initial corpus and the pre-existing organized files in the bucket.

## Story

As the owner, I want a standalone Python script that imports my existing paperless-ngx documents into the empty Garage bucket with their metadata as sidecars, so that the renamer service starts with a populated, correctly named file tree and a training corpus, without any document being reclassified by hand.

## Background and Context

- The renamer service assumes an empty v1 scope (new files only). This story backfills the historical corpus from paperless-ngx before the service is ever started.
- The script runs once, offline, from the owner's machine or the server. It is not part of the renamer container and has no dashboard surface.
- Sidecars created here follow the exact same schema as the service's (PRD section 7), with status validated, since the metadata comes from a system the owner already curated.
- After the import, the service's rebuild command (FR10.2) populates SQLite from these sidecars. No code in the service is needed for this story beyond schema compatibility.

## Requirements

### FR0.1 Connection

- Connect to the paperless-ngx REST API (`PAPERLESS_URL` + API token from env vars).
- Connect to the target Garage bucket with boto3 (S3 endpoint, credentials, bucket from env vars). The bucket must be empty on the `files/` prefix; the script refuses to run if any object already exists there (dry-run flag lists what would happen instead).

### FR0.2 Listing and mapping

- List all documents from paperless via the API (paginated `/api/documents/`).
- For each document, fetch the document metadata and map it to the sidecar fields:
  - template ← the first path segment of the paperless storage path (the document family, e.g. `Banque`, `Factures`).
  - emission_date ← paperless document date (`created` field, the content date).
  - emitting_company ← the document's correspondent name.
  - document_type ← the document type name.
  - period ← the last space-separated part(s) of the original filename, parsed as a period: a month-year ("août 2025"), a quarter ("Q3 2026"), a year ("2026"), or the date itself for daily documents. If no period can be parsed, fall back to the period computed from emission_date and the template's granularity/offset rules; if no template rule matches, leave the period equal to the emission date's month-year and log a warning.
  - extracted_text ← the first page text of the downloaded PDF, extracted with the same library as the service (PyMuPDF), stored in the sidecar exactly as the service would (PRD FR2.2, FR2.4). This makes the imported files immediately usable as the classifier corpus without any later re-download or re-parse.
  - optional fields ← sourced from the document's paperless labels. Each label becomes an optional field entry in the sidecar: the label name is the field name, the label value is the field value (boolean labels are stored as true). Labels that collide with a common field name are ignored with a warning. The template YAML's optional field schemas are not enforced at import time; the consistency job or a later review surfaces any mismatch.
- original_key ← the paperless archive filename and path as stored by paperless (`storage_path` + `original filename`).
- status ← validated, confidence ← 1.0, with a single event `imported` in the events array recording the source (paperless document id and URL).
- sha256 computed from the downloaded PDF binary.

### FR0.3 Upload

- Download the PDF through the paperless API (`/api/documents/{id}/download/`).
- Store it in the bucket at `files/{template}/{original filename}` — i.e. reuse the path and filename paperless already uses, so the tree stays familiar and no renames occur during import. Collision: incremental suffix per PRD FR4.4.
- Write the sidecar `{key}.meta.json` with the atomic temp-key + copy pattern (same as the service) so the file tree is consistent if the script is interrupted.

### FR0.4 Resumability and safety

- The script is resumable: a document whose PDF key and sidecar already exist (matching sha256) is skipped.
- A summary is printed at the end: imported, skipped, failed, period fallbacks used, per-template counts.
- Failures (API errors, download errors) are logged with the paperless document id and do not abort the run; the script exits non-zero if any failure occurred.
- The script never writes to `inbox/` or `pending/`.

## Acceptance Criteria

- AC0.1 Running the script against the paperless instance and an empty bucket results in every paperless document present under `files/` with its paperless path and filename, each with a valid sidecar.
- AC0.2 Every sidecar carries the five common fields with correct provenance: template from storage path, emission_date from document date, emitting_company from correspondent, document_type from document type, period parsed from the filename tail (e.g. "août 2025" for "2025-08-31 Relevé de compte Compte LMNP août 2025.pdf"), plus the first-page extracted_text and one optional field per paperless label.
- AC0.3 All imported sidecars have status validated, a computed sha256, and an imported event; running the service's SQLite rebuild afterwards yields a fully populated dashboard with zero not-processed files from the import.
- AC0.4 Interrupting the script and rerunning it completes the import without duplicates (resumability), and the bucket never contains a sidecar without its PDF or vice versa for more than one in-flight file.
- AC0.5 The script performs no network calls other than the paperless API and the Garage endpoint.

## Tasks

- T1 Standalone script skeleton: env-var config (paperless URL, token, S3 endpoint, credentials, bucket), boto3 client, argument parsing (dry-run, resume default, limit for a trial run).
- T2 Paperless client: paginated document listing, metadata fetch (correspondent, document type, storage path, dates), PDF download, sha256 computation.
- T3 Period parser: month-year / quarter / year / date from the filename tail; fallback to template-rule computation from emission_date.
- T3b First-page extractor: PyMuPDF, same extraction parameters as the service; empty text stored as an empty string with a warning (the document stays validated, it is imported metadata, not classified).
- T3c Label mapper: paperless labels → optional fields dictionary; name-collision guard against the common field names.
- T4 Sidecar builder: PRD section 7 schema, imported event, status validated.
- T5 Uploader: key construction from storage path + filename, collision suffix, atomic sidecar write, skip-if-present resumability check.
- T6 Summary reporting and exit codes.
- T7 Unit tests (pytest): period parser matrix, sidecar schema, key construction, collision suffix, resume logic with a mocked S3 and mocked paperless API.

## Interactions and Affected Components

- No changes to the renamer service code. Shared elements: sidecar schema and the atomic write pattern (the script duplicates the small helper, deliberately, to stay standalone).
- Output feeds: the classifier's initial corpus (template + first-page text later re-extracted by the service is not needed here; validated status makes these files eligible for the first retrain) and the dashboard after rebuild.

## Notes and Open Points

- Sidecars now include extracted_text (first page, PyMuPDF, same parameters as the service), so no post-import re-extraction backfill is needed and the corpus is immediately usable for the first retrain.
- Optional fields come solely from paperless labels. Labels are free-form in paperless; the template YAML schemas are not enforced at import, and labels colliding with common field names are dropped with a warning.
- Document subtype mapping (paperless document types to the renamer template YAML document types) is identity on the name; if the template YAML later diverges, the consistency job surfaces mismatches.
- The script lives in the repo as `scripts/import_paperless.py` with its own minimal dependencies (boto3, requests, python-dateutil, PyMuPDF), not in the container image.