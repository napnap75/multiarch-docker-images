# Technical Architecture

## 1. Overview

Single docker container Go application built on PocketBase as an embedded framework (https://pocketbase.io/). The binary contains:

- PocketBase's HTTP/API server and admin dashboard;
- the custom review dashboard;
- the S3 poller and the PDF processing pipeline, executed through PocketBase's standard job scheduler (`app.Cron()`);
- the declarative rule engine (JSONC config + `expr-lang/expr` expressions);
- access to the self-hosted S3 storage via the MinIO Go SDK.

Everything runs offline on the server infrastructure: no cloud APIs, no outbound calls other than to the S3 cluster.

## 2. Stack

| Component | Choice |
|---|---|
| Framework / runtime | PocketBase (Go framework, single binary) |
| Language | Go |
| Scheduling | `app.Cron()` (S3 poller, periodic tasks) |
| S3 | MinIO Go SDK (`github.com/minio/minio-go/v7`) |
| PDF text extraction | `github.com/ledongthuc/pdf` (native text, no OCR) |
| Rule expressions | `github.com/expr-lang/expr` |
| Regex | stdlib `regexp` (RE2); `regexp2` only if a lookaround proves indispensable |
| Config validation | `santhosh-tekuri/jsonschema` (schema generated from structs via `invopop/jsonschema`) |
| Database | SQLite (embedded in PocketBase) |
| Full-text search | SQLite FTS5 index synchronized via hooks |

## 3. S3 Layout

A single versioned bucket:

```
inbox/       # drop zone for new PDFs (via Nextcloud / S3 client)
pending/     # files awaiting human review
files/       # renamed and filed files (configured prefix structure)
```

Nextcloud mounts the bucket as external storage: it is the user-facing interface for uploading, downloading, and sharing. The application only manipulates S3 keys, never a local filesystem for documents.

## 4. Data Model (PocketBase Collections)

### `documents`
One row per processed PDF file:

- `sha256`: the sha256 is a primary key as the system will reject as duplicate any file with the same hash
- `original_filename`: original file name in `inbox/`
- `file_key`: current S3 key of the file
- `extracted_text`: full text extracted from all pages (base of the FTS5 index)
- `first_page_text`: first-page text (extraction base)
- `size`: file size
- `status`: `pending` | `processed` | `validated`
+ all the fields declared by the user in the configuration file

The append-only `document_events` collection is the single source of truth for a document's history; no history is stored on the `documents` record itself.

### `document_events`
Append-only history: `document`, `event_type` (`classified`, `moved`, `corrected`, `validated`), `payload` as JSON, `timestamp`.


## 5. Configuration File (JSONC)

A `config.jsonc` file kept alongside the binary, reloaded on restart. Three sections:

1. `fields`: declaration of fields to extract from the files:
   - `name`: name of the field
   - `type`: `string` | `number` | `boolean`
   - `mandatory`: true | false

2. `rules`: ordered list of rules that will be evaluated one after the other:
   - `when`: an `expr-lang/expr` expression evaluated over the document text and the current field values; rules run in order and each `when` may reference fields set by earlier rules (e.g. `firstPageText contains "Relevé de compte"`, or `emittingCompany in ["BRED", "Fortunéo"] && documentType == "Relevé de compte"`)
   - `actions`: 
       - `set`: set the value of one field; fields accumulate as rules are applied in order
	   - `stop`: stop rule evaluation; the fields set so far are kept and the usual validations still apply

3. `templates`: named naming templates (e.g. `bank_statement: {sender}/{date}_{type}_{reference}.pdf`), with required-field validation — a template with a missing mandatory field renders the result invalid and routes the file to `pending/`. The template is chosen by the rules themselves: `template` is a field declared in the `fields` section like any other and set with `set` (e.g. `set template = "bank_statement"` when `emittingCompany in ["BRED", "Fortunéo"]` and `documentType == "Relevé de compte"`). If no rule sets `template`, the special `default` template is used. A `template` value with no matching entry renders the result invalid and routes the file to `pending/`.

Validation on load: JSON schema, regex compilation, `expr` expression compilation against a typed context (`Document{FirstPageText, Fields}`). Full rejection if invalid, no partial processing.

## 6. Processing Pipeline

Executed by the PocketBase cron job (configurable interval, e.g. every minute):

1. **Poll**: list objects in `inbox/`; an object is a candidate only if no `documents` record references its key yet, and its size and ETag are stable across two consecutive polls (write-completion wait).
2. **Download**: fetch the object into memory (streamed), no disk writes. The sha256 is computed during download; if a `documents` record with the same hash already exists, the object is a duplicate: the `inbox/` key is deleted (delete marker, data recoverable), a message is logged in the application logs, and processing stops there.
3. **Extraction**: first-page text (for rule evaluation) + full text (for FTS) via `ledongthuc/pdf`, including headers and footers
4. **Rule evaluation**: rules evaluated in order against the first-page text and the fields set so far; full evaluation log written to the application logs (built-in dry-run mode for debugging from the dashboard).
5. **Validation**: a result is valid if and only if: all rules have been processed with no error, all mandatory fields are identified, and the naming template evaluated with no error. Otherwise: route directly to `pending/`.
6. **Record**: create the `documents` record (sha256, original filename, current inbox key, extracted texts, fields, status) and add a `classified` event including the original filename. The record exists before any physical move, so an interrupted filing leaves a recoverable state.
7. **Filing**:
   - valid → resolve the target name first: if a file with the same name already exists in the target prefix, append a number to the filename; then copy to the new key (prefix + templated name), verify the target object exists, and delete the `inbox/` key only after the copy is confirmed; finally update the `documents` record with the final, conflict-resolved filename (`file_key`, status `processed`, `moved` event).
   - invalid → move to `pending/` (incremental suffix on name conflict); update `file_key`; status `pending`.

The bucket is versioned: any `inbox/` object deletion creates a delete marker, the data remains recoverable.

## 7. Review Dashboard

Custom pages on the PocketBase instance:

- File list: all files with status, filters, combined search over full text (FTS5) + metadata/fields.
- Detail: PDF preview streamed from S3 through a dedicated route, along with extracted fields; debug mode adds detailed log lines explaining every rule evaluation and action performed.
- Correction: forms to edit the extracted fields; correcting a `processed` file re-files it (copy to the new key, delete the old one, history preserved). A corrected `pending` file follows the same path: once validated, it is copied to its templated key in `files/` (incremental suffix on conflict) and removed from `pending/`.
- Validation: transition to `validated`; `processed` files can also be validated. Validation is what takes a corrected `pending` file out of the review queue.
- Rule management: config display and dry-run on an existing document. The configuration is reloaded only on restart; there is no runtime reload.

Authentication: single account via PocketBase's standard auth.

## 8. Error Handling

- Transient S3 error: retry with backoff, the object stays in `inbox/` and will be polled again.
- Unreadable PDF: `pending` status, reason recorded in the payload of the `classified` event.
- Target key conflict (two files renamed identically, whether filing to `files/` or moving to `pending/`): automatic incremental suffix, event logged.
- Invalid config: error message displayed and the application does not start.
