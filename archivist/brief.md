# Project Brief

## 1. Vision

A personal, 100% offline tool that watches an S3 bucket hosted on a self-hosted storage cluster, automatically identifies the type of each incoming PDF (invoice, purchase order, contract, etc.) from its content, applies the corresponding naming template by extracting the fields (date, type of document, references), and moves the object into an organized S3 prefix structure. Users upload, download, and share files through Nextcloud, which mounts the bucket as external storage. Uncertain or incomplete results go through human review in a local web dashboard.

## 2. Problem and Context

PDFs arrive continuously in an inbox S3 prefix (dropped via Nextcloud by the user or by upload automations). Current naming is manual or inconsistent, making documents hard to find and archive. The user already owns around 2000 files already correctly named.

## 3. Objectives

- Extract the emitting company, document type, additional fields, and emission date from the text content of the first page of the document (including headers and footers), not from the current object name.
- Rename and move the object within the bucket into an organized prefix structure depending on the extracted data.
- Unambiguous deterministic rules and valid fields: automatic processing. If rules cannot determine a valid result or extraction is incomplete, queue the file with status `pending` for human review in the dashboard.
- Run fully offline on self-hosted infrastructure: no cloud APIs, no data ever leaves the server.

## 4. Scope

- Ingestion by polling the `inbox/` prefix of a S3 bucket for new objects only. Nextcloud (External Storage app) is the user-facing interface to upload, download, and share files.
- Extract the text from the first page of each PDF. All files are text-based; OCR for scanned/image-only documents is out of scope.
- Deterministic template classification using configured rules, extracted text, and reference data; uncertain or insufficient results are routed to `pending/` for manual correction.
- Fields to extract, extractors and template for renaming the file declared in configuration (JSONC format), not in code.
- Storage: self-hosted S3-compatible with a single versioned bucket; objects addressed by S3 keys, no local filesystem for documents.
- Local web dashboard: all processed files with status (pending / processed / validated), file detail page with PDF preview streamed from S3, template and field correction, validation, history, and full-text search across the entire extracted document in addition to metadata/field search.

## 5. User and Usage

Single user (personal use), technically proficient. Nominal flow:

1. A PDF is uploaded into the `inbox/` prefix via Nextcloud (or any S3 client).
2. The poller detects the new object, waits for write completion, downloads it, extracts text from document, applies deterministic classification and field-extraction rules, then validates.
3. A deterministic result with valid fields: the object is copied to its renamed key in the target prefix and the inbox copy is deleted only after the file is properly processed. The status `processed` is applied to this file.
4. If deterministic rules cannot produce a valid result: the file is moved to `pending/`, the status `pending` is applied and the file appears in the dashboard review queue; the user will have to correct and may validate it.
5. In the dashboard, the user may review the already `processed` or `validated` files to correct the fields and validate them.
