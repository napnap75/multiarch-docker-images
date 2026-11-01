# Project Brief

## 1. Vision

A personal, 100% offline tool that watches the `inbox/` prefix of a self-hosted S3 bucket, identifies each incoming PDF (invoice, purchase order, contract, etc.) from the text content of its pages, applies the corresponding naming template by extracting fields, and files the object into an organized prefix structure. Nextcloud, which mounts the bucket as external storage, is the user-facing interface for uploading, downloading, and sharing. Files that cannot be classified with certainty go to `pending/` for review in a local web dashboard.

## 2. Problem and Context

PDFs arrive continuously in the inbox prefix via Nextcloud or upload automations. Current naming is manual or inconsistent, making documents hard to find and archive. Around 2000 existing files are already correctly named.

## 3. Objectives

- Extract the emitting company, document type, additional fields, and emission date from the document's text content (including headers and footers), never from the current object name.
- Deterministic processing: unambiguous rules and valid fields → automatic filing. Anything uncertain or incomplete → status `pending` for human review in the dashboard.
- Fully offline on self-hosted infrastructure: no cloud APIs, no data ever leaves the server.

## 4. Scope

- Ingestion: polling the `inbox/` prefix for new objects. OCR for scanned/image-only documents is out of scope.
- Fields, rule-based extraction, and naming templates declared in configuration (JSONC), not in code; constant reference lists live inline in rule expressions.
- Storage: single versioned self-hosted S3 bucket; objects addressed by S3 keys, no local filesystem for documents.
- Dashboard: file list with status, PDF preview streamed from S3, field and template correction, validation, history, and full-text search across the entire document in addition to metadata/field search.
- The ~2000 pre-existing files are out of scope: no initial import or indexing.

## 5. User and Usage

Single user (personal use), technically proficient. Nominal flow:

1. A PDF is dropped into `inbox/` via Nextcloud (or any S3 client).
2. The tool classifies it: a valid result is filed automatically (`processed`); otherwise the file lands in `pending/` in the review queue.
3. From the dashboard, the user corrects fields (a correctable file is re-filed and becomes `validated`) and validates `processed` files.
