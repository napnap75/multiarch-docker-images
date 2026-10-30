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
- `extracted_text`: full extracted text (first page at minimum, stored for FTS)
- `first_page_text`: first-page text (extraction base)
- `size`: file size
- `processed_at`: timestamp of the file initial process
- `status`: `pending` | `processed` | `validated`
- `updated_at`: timestamp of the last update to the file metadata
- `history`: timestamped field/status changes (related `document_events` table)
+ all the fields declared by the user in the configuration file

### `document_events`
Append-only history: `document`, `event_type` (`classified`, `moved`, `corrected`, `validated`), `payload` as JSON, `timestamp`.


## 5. Configuration File (JSONC)

A `config.jsonc` file kept alongside the binary, reloaded on restart. Three sections:

1. `fields`: declaration of fields to extract from the files:
   - `name`: name of the field
   - `type`: `string` | `number` | `boolean`
   - `mandatory`: true | false

2. `rules`: ordered list of rules that will be evaluated one after the other:
   - `when`: an `expr-lang/expr` expression combining the extracted variables (e.g. `siren == "123456789" && has_iban`)
   - `actions`: 
       - `set`: set the value of one field
	   - `stop`: stop the processing after this rule

3. `templates`: naming templates per document type (e.g. `{sender}/{date}_{type}_{reference}.pdf`), with required-field validation — a template with a missing field renders the result invalid and routes the file to `pending/`.

Validation on load: JSON schema, regex compilation, `expr` expression compilation against a typed context (`Document{FirstPageText, Sender, Siren, Iban, Confidence, ...}`). Full rejection if invalid, no partial processing.

## 6. Processing Pipeline

Executed by the PocketBase cron job (configurable interval, e.g. every minute):

1. **Poll**: list objects in `inbox/` not yet processed (detection by key recorded in the database). Write-completion wait: an object is a candidate only if its size and ETag are stable across two consecutive polls.
2. **Download**: fetch the object into memory (streamed), no disk writes.
3. **Extraction**: first-page text (for rule evaluation) + full text (for FTS) via `ledongthuc/pdf`, including headers and footers
4. **Rule evaluation**: rules evaluated one after the other; full log stored in `rule_trace` (built-in dry-run mode for debugging from the dashboard).
5. **Validation**: a result is valid if and only if: all rules have been processed with no error, all mandatory fields are identified, and the naming template evaluated with no error. Otherwise: route directly to `pending/`.
6. **Filing**:
   - valid → copy to the new key (prefix + templated name), verify the target object exists, then delete the `inbox/` key only after the copy is confirmed; status `processed`. If a file with the same name already exists, append a number to the filename to avoid overwriting.
   - invalid → move to `pending/`; status `pending`.
7. **Indexing**: write the `documents` record, add an event to the file history including the original filename.

The bucket is versioned: any `inbox/` object deletion creates a delete marker, the data remains recoverable.

## 7. Review Dashboard

Custom pages on the PocketBase instance:

- File list: all files with status, filters, combined search over full text (FTS5) + metadata/fields.
- Detail: PDF preview streamed from S3 through a dedicated route, along with extracted fields and `rule_trace` visible in debug mode.
- Correction: forms to edit fields, type, and sender; correcting a `processed` file re-files it (copy to the new key, delete the old one, history preserved).
- Validation: transition to `validated`; `processed` files can also be validated.
- Rule management: config display, dry-run on an existing document, reload.

Authentication: single account via PocketBase's standard auth.

## 8. Error Handling

- Transient S3 error: retry with backoff, the object stays in `inbox/` and will be polled again.
- Unreadable PDF: `pending` status, reason recorded.
- Target key conflict (two files renamed identically): automatic incremental suffix, event logged.
- Invalid config: error message displayed and the application does not start.
