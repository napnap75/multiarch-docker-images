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
| Framework / runtime | PocketBase v0.41.1 (Go framework, single binary; module `ganigeorgiev.com/pocketbase/pocketbase`) |
| Language | Go |
| Scheduling | `app.Cron()` (S3 poller, periodic tasks) |
| S3 | MinIO Go SDK (`github.com/minio/minio-go/v7`) |
| PDF text extraction | `github.com/ledongthuc/pdf` (native text, no OCR) |
| Rule expressions | `github.com/expr-lang/expr` |
| Regex | stdlib `regexp` (RE2); `regexp2` only if a lookaround proves indispensable |
| Config validation | `santhosh-tekuri/jsonschema` (schema generated from structs via `invopop/jsonschema`) |
| Database | SQLite (embedded in PocketBase) |
| Full-text search | SQLite FTS5 index synchronized via hooks |
| Logging | PocketBase `app.Logger()` (Dashboard > Logs); stderr before bootstrap |

## 3. S3 Layout

A single bucket. Versioning is not required, but it is strongly recommended: without it, any deletion performed by the application (or by a bug) is final; with it, delete markers keep deleted objects recoverable.

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
- `file_key`: current S3 key of the file
- `pages`: page-by-page extracted text as a JSON array (0-based, `pages[0]` = page 1); the FTS5 full-text index is built from the concatenation of all pages
- `status`: `pending` | `processed` | `validated`
+ all the fields declared by the user in the configuration file

The append-only `document_events` collection is the single source of truth for a document's history; no history is stored on the `documents` record itself.

### `document_events`
Append-only history: `document`, `event_type` (`imported`, `corrected`, `validated`), `payload` as JSON, `timestamp`.

The payload is self-contained: each event carries its own type, its own timestamp and the document state before and after, so a document's full history is reconstructed from this collection alone. All dates are RFC 3339; all S3 keys are complete, prefix included.

Common payload keys:

- `event_type`: `imported` | `corrected` | `validated`
- `timestamp`: RFC 3339 date of the event
- `before` / `after`: snapshots of the document state; `file_key` (the S3 key, unified with the `documents` record field) is always present; `status` appears only when the event changes it — at import the status does not exist before, so `before` carries no `status`; declared extraction fields appear only when they have a recognized value — a missing key means the value is unknown, there is no `null`

Per event type:

| Key | imported | corrected | validated |
|---|---|---|---|
| `event_type` | always | always | always |
| `timestamp` | always | always | always |
| `errors` | on failure | — | — |
| `before` | always | always | always |
| `after` | always | always | always |

`imported` — created after filing (valid or not), one event for the whole import + classification:

```json
{
  "event_type": "imported",
  "timestamp": "2026-01-15T14:23:07Z",
  "before": {
    "file_key": "inbox/scan_2026-01-15.pdf"
  },
  "after": {
    "file_key": "files/BRED/Decembre 2025_Relevé de compte_Compte Thomas.pdf",
    "status": "processed",
    "emittingCompany": "BRED",
    "documentType": "Relevé de compte",
    "emissionDate": "2026-01-15T00:00:00Z",
    "template": "bank_statement",
    "bankAccount": "Compte Thomas"
  }
}
```

On classification failure, the document is routed to `pending/` and the payload adds `errors` (one entry per cause: missing mandatory field, rule error, missing template, naming-template error) while `after` still lists the fields recognized before the failure — this is what the correction form pre-fills:

```json
{
  "event_type": "imported",
  "timestamp": "2026-01-15T14:23:07Z",
  "errors": ["mandatory field missing: emittingCompany"],
  "before": {
    "file_key": "inbox/scan_2026-01-15.pdf"
  },
  "after": {
    "file_key": "pending/scan_2026-01-15.pdf",
    "status": "pending",
    "documentType": "Relevé de compte",
    "emissionDate": "2026-01-15T00:00:00Z",
    "template": "bank_statement"
  }
}
```

`corrected` — created after a dashboard correction is saved and the file re-filed; a correction always moves the document to `validated` whatever its previous status, so the status change is part of the snapshot diff:

```json
{
  "event_type": "corrected",
  "timestamp": "2026-01-18T09:41:52Z",
  "before": {
    "file_key": "pending/scan_2026-01-15.pdf",
    "status": "pending",
    "emittingCompany": "BRED",
    "emissionDate": "2026-01-15T00:00:00Z",
    "bankAccount": "Compte Thomas"
  },
  "after": {
    "file_key": "files/Fortunéo/Janvier 2026_Relevé de compte_Compte Thomas.pdf",
    "status": "validated",
    "emittingCompany": "Fortunéo",
    "emissionDate": "2026-02-15T00:00:00Z",
    "template": "bank_statement"
  }
}
```

The diff is derived by comparing the two snapshots: a field present in `before` and absent from `after` was deleted (case of `bankAccount` above); the reverse means it was added (`template` above).

`validated` — transition from `processed` to `validated` from the dashboard, no re-filing, no field change; only the status changes, so the snapshots carry it alone:

```json
{
  "event_type": "validated",
  "timestamp": "2026-01-20T16:05:11Z",
  "before": {
    "status": "processed"
  },
  "after": {
    "status": "validated"
  }
}
```


## 5. Configuration File (JSONC)

A `config.jsonc` file kept alongside the binary, reloaded on restart. Three sections:

1. `fields`: declaration of fields to extract from the files:
   - `name`: name of the field
   - `type`: `text` | `number` | `date` | `boolean` | `enum`
   - `mandatory`: true | false

   Type semantics in rule expressions: `number` and `boolean` expose native values, `date` exposes a native Go `time.Time` (comparable, arithmetic via `duration()`, usable with the `date()`/`now()` builtins), `text` and `enum` expose strings. `enum` does not declare its values: the application builds the field's vocabulary from the constants the rules assign to it; the dashboard correction form offers this vocabulary as a dropdown, with free entry allowed. Assigning a value that does not match the field type (e.g. a raw string to a `date` field, a non-numeric value to a `number` field) is a rule error and routes the document to `pending/`.

2. `rules`: an ordered tree of rules. Each rule contains four keys:
   - `when` (required): an `expr-lang/expr` expression evaluated against the document pages (`pages`) and the current field values; the rest of the rule runs only if it returns true (e.g. `pages[0] contains `Relevé de compte``, or `emittingCompany in [`BRED`, `Fortunéo`] && documentType == `Relevé de compte``)
   - `set`: list of assignments, evaluated in order — `{ "field": ..., "value": ... }` for a constant, `{ "field": ..., "eval": ... }` for an expression evaluated in the same context
   - `delete`: list of field names to unset (they become undefined again for later rules and template rendering)
   - `rules`: child rules, evaluated in order only if the parent `when` matched; nesting is recursive

   The application registers two built-in helpers in the expression environment. `firstMatch(regex, texts?)` returns the first match of the regex in the document text (first capture group when present), or an empty string when no match is found (parsing an empty string with `date()` is then a rule error and routes the document to `pending/`); the optional second argument is an expression evaluated to the text(s) to search — a single page (e.g. `pages[0]`, the first page), a slice (e.g. `pages[0:3]`, pages 1 to 3, expr's standard slice syntax, end exclusive), or any list of strings — and when omitted the whole document is searched (e.g. `firstMatch(`[0-9]{2}/[0-9]{2}/[0-9]{4}`, pages[0])`). `period(value, unit, shift, layout?)` renders a period instead of a raw date: `value` is a native date (e.g. `emissionDate`), `unit` is `month` | `quarter` | `half` | `year`, `shift` is `-1` (previous period), `0` (period containing the date) or `+1` (next period) — e.g. a rent receipt issued mid-month uses the containing month, a service-charge call issued at the end of a quarter uses the next quarter, a bank statement uses the previous month. It returns a French string: `Janvier 2026`, `T2 2026`, `S1 2026` or `2026`. The optional `layout` argument is a standard Go reference-date layout applied to the period's first day, with month names rendered in French by the application (default layouts: `January 2006` for `month`, `2006` for `year`); it is ignored for `quarter` and `half`, whose formats (`T<quarter> <year>`, `S<half> <year>`) are fixed. A missing or invalid date is a rule error and routes the document to `pending/`. Parsing strings into native dates uses expr's standard `date(value, layout)` builtin (Go date layout, e.g. `date(`15/01/2026`, `02/01/2006`)`) — the format is carried by the parsing rule, so it can differ per document type.

`frDate(text)` extracts and parses a French natural-language date — the layout Go cannot express, since `date(value, layout)` only knows English month names. It matches `le 19 janvier 2026` (day and leading `le` optional: `Janvier 2026` gives the first of the month; `1er` and abbreviated month names like `févr.` are accepted; accents are optional) and returns a native date — e.g. `frDate(firstMatch(`(?:le )?\d{1,2} \S+ \d{4}`, pages[0]))`, or `frDate(pages[0])` when the first French-looking date of the page is the right one. A text containing no French date, or an impossible date (31 février), is a rule error and routes the document to pending/.

3. `templates`: named naming templates (e.g. `bank_statement: {emittingCompany}/{emissionDate.Format(`2006-01-02`)}_{documentType}_{bankAccount}.pdf`). A template is a literal string containing `{...}` placeholders: the inside of each placeholder is an `expr-lang/expr` expression evaluated in the document context (fields, helpers, builtins — e.g. `{emissionDate.Format(`2006-01-02`)}` or `{upper(emittingCompany)}`) and replaced by its result; the static text between placeholders stays literal. Unset optional fields evaluate to an empty string; a missing mandatory field or an evaluation error renders the result invalid and routes the file to `pending/`. All placeholder expressions are compiled at config load; "/" must not appear in the result (it is the S3 key delimiter). The template is chosen by the rules themselves: `template` is a field declared in the `fields` section like any other and set with `set` (e.g. `set template = `bank_statement`` when `emittingCompany in [`BRED`, `Fortunéo`] and documentType == `Relevé de compte``). If no rule sets `template`, the special `default` template is used. A `template` value with no matching entry renders the result invalid and routes the file to `pending/`.

Validation on load: JSON schema, regex compilation, `expr` expression compilation (`when`, `eval` and template placeholder expressions) against a typed context (`Document{Pages, Fields}`) with the `firstMatch` and `period` helpers, regex precompilation and caching. Full rejection if invalid, no partial processing.

## 6. Processing Pipeline

Executed by the PocketBase cron job (configurable interval, e.g. every minute):

1. **Poll**: list objects in `inbox/`; an object is a candidate only if no `documents` record references its key yet, and its size and ETag are stable across two consecutive polls (write-completion wait). The same pass also picks up interrupted filings: any `documents` record whose `file_key` is still under `inbox/` (a filing that was interrupted between record creation and the physical move) is completed as described below.
2. **Download**: fetch the object into memory (streamed), no disk writes. The sha256 is computed during download; if a `documents` record with the same hash already exists, the object is a duplicate: the `inbox/` key is deleted (a no-data-loss case — the original with the same content stays filed), a message is logged via the PocketBase logger (section 9), and processing stops there. By design, no `document_events` entry is recorded: the duplicate is not a document of its own, and the trace lives in the PocketBase logs only.
3. **Extraction**: page-by-page text (one entry per page, for rule evaluation and the FTS index) via `ledongthuc/pdf`, including headers and footers
4. **Rule evaluation**: rules evaluated in order against the document pages and the fields set so far; full evaluation log written to the PocketBase logger at Debug level (section 9; the same trace feeds the built-in dry-run mode for debugging from the dashboard).
5. **Validation**: a result is valid if and only if: all rules have been processed with no error, all mandatory fields are identified, the selected template exists in the configuration, and the naming template evaluated with no error. Otherwise: route directly to `pending/`.
6. **Record**: create the `documents` record (sha256, current inbox key, pages, fields, status) and add an `imported` event including the original key (`before.file_key`). The record exists before any physical move, so an interrupted filing leaves a state the poller recovers automatically on its next pass.
7. **Filing**:
   - valid → resolve the target name first: if a file with the same name already exists in the target prefix, append a number to the filename; then copy to the new key (`files/` + rendered template — the template renders the full object path, there is no separate prefix), verify the target object exists, and delete the `inbox/` key only after the copy is confirmed; finally update the `documents` record with the final, conflict-resolved filename (`file_key`, status `processed`); the whole import is recorded as the single `imported` event of section 4.
   - invalid → move to `pending/` (incremental suffix on name conflict); update `file_key`; status `pending`.

When the bucket is versioned, any deletion creates a delete marker and the data remains recoverable; when it is not, deletions are final. The application logs a warning at startup when versioning is disabled, recommending the user to enable it to reduce the risk of losing a file to a bug.

## 7. Review Dashboard

Custom pages on the PocketBase instance:

- File list: all files with status, filters, combined search over full text (FTS5) + metadata/fields.
- Detail: PDF preview streamed from S3 through a dedicated route, along with extracted fields; debug mode adds detailed log lines explaining every rule evaluation and action performed.
- Correction: performed directly from the standard PocketBase collections UI (or any PocketBase client) by editing the extracted fields. The application's `OnRecordUpdateRequest` hook on `documents` intercepts the save: it evaluates the selected template with the new values and refuses the save if the template cannot be evaluated without error; otherwise it re-files the file (copy to the new key, delete the old one, incremental suffix on conflict, move skipped when the rendered name is unchanged), updates `file_key`, sets the status to `validated` whatever the previous status, and appends the `corrected` event. A corrected `pending` file is thereby removed from `pending/`. On a mid-re-filing failure the save is rejected and the S3 state is restored. History is preserved.
- Validation: performed from the same interface by changing the status to `validated`; the hook appends the `validated` event, and any other status change is refused. A `pending` file can only leave the review queue through a correction that can be saved, which itself sets the status to `validated`.
- Rule management: config display and dry-run on an existing document. The configuration is reloaded only on restart; there is no runtime reload.

Authentication: single account via PocketBase's standard auth.

## 8. Error Handling

- Transient S3 error: retry with backoff, the object stays in `inbox/` and will be polled again.
- Unreadable PDF: `pending` status, reason recorded in the `errors` key of the `imported` event payload.
- Target key conflict (two files renamed identically, whether filing to `files/` or moving to `pending/`): automatic incremental suffix, event logged.
- Invalid config: error message displayed and the application does not start.
## 9. Logging

Two channels, split by lifecycle:

- Before the PocketBase application is bootstrapped (fatal startup errors: missing environment variable, invalid `config.jsonc`, unreachable S3; pre-bootstrap startup traces): stderr, so `docker logs` shows the cause under the `--restart unless-stopped` restart loop (US-05).
- After the bootstrap: PocketBase's `app.Logger()` — structured records (message plus key/value attributes) stored in the PocketBase logs database, separate from the application data, browsable and filterable from the admin dashboard (Dashboard > Logs). Retention and minimum level are configured from the standard PocketBase logs settings panel.

Levels used:

- `Error`: a failed operation (S3 error, event append failure).
- `Warn`: a degraded but non-fatal state (versioning disabled, schema column removed, stuck record whose inbox object disappeared).
- `Info`: pipeline outcomes (document imported, duplicate removed, interrupted filing recovered).
- `Debug`: the per-rule evaluation trace of section 6, step 4.
## 10. Testing

A single E2E regression gate, run after building the image and before publishing it:

```
cd archivist
E2E_IMAGE=<image-under-test> go test ./e2e -v -count=1
```

The whole harness lives in one file, `e2e/e2e_test.go`. TestMain orchestrates the containers itself over `docker` (no compose, no fixtures):

- starts a disposable Garage (`dxflrs/garage` by default, `GARAGE_IMAGE` overrides) on a private docker network, bootstraps its layout, bucket and key, and publishes its S3 API on 127.0.0.1:3900;
- starts the image under test with a minimal `config.jsonc` embedded in the test file and the cron disabled;
- drives everything through the PocketBase HTTP API and the S3 API; test PDFs are generated in memory from a minimal one-page template, so the classification outcome is the proof that extraction works, without any fixture.

The scenarios form one narrative; each subtest names the US it proves:

1. Startup — health, collections migrated, versioning warning in the PocketBase logs (Garage has no versioning: the US-01 warning path is exercised for free), poll route refused without superuser auth (US-01, US-03, US-04).
2. Import — a valid simulated statement is classified and filed to `files/` with its `imported` event (US-10 to US-17).
3. Duplicate — the same content re-uploaded is deleted from `inbox/`, no record, no event (US-12).
4. Pending — a missing mandatory date routes the document to `pending/` with `errors` in the imported event and the recognized fields kept (US-13 to US-15).
5. Correction — a field PATCH through the API re-files and validates, `corrected` event appended (US-22).
6. Validation — a status-only PATCH appends the `validated` event, `file_key` untouched (US-23).
7. Append-only — PATCH and DELETE on `document_events` are refused (US-04).
8. Recovery — a stuck record (`file_key` still under `inbox/`, created directly through the API) is completed by the next pass (US-11).
9. Restart — the container restarts with all state intact.

`POST /api/archivist/poll` (superuser only) runs one poll pass immediately. It exists so the gate and the operator trigger passes on demand instead of waiting for the cron; a fresh upload needs two calls because the poller waits for size/ETag stability across two passes (US-10).

The gate is a regression detector for dependency upgrades (PocketBase, minio-go, ledongthuc/pdf, expr, Garage), not an acceptance proof: acceptance criteria are reviewed on the stories themselves. `E2E_KEEP=1` leaves the containers running after the run for debugging.

## 11. Packaging

`Dockerfile`, two-stage build:

- Builder: `golang:1.27-alpine` (matches the `go 1.27` directive of PocketBase v0.41.1), CGO disabled, static binary, `TARGETARCH` honored for multi-platform builds under buildx. `go mod tidy` runs there because `go.sum` is not committed; once it is committed, the line becomes `go mod download` for fully reproducible builds.
- Runtime: `scratch` with only the `/archivist` binary, the CA bundle (the S3 endpoint is HTTPS by default), the timezone database (the cron and date rendering honor `TZ`), and `/app/pb_data` + `/tmp`, the only writable directories, owned by the unprivileged runtime user 65532. `config.jsonc` is never baked in: it is mounted at runtime, `ARCHIVIST_CONFIG` defaulting to `config.jsonc` relative to `/app`.

The container runs as 65532:65532 (non-root), exposes 8090, declares `/app/pb_data` as a volume, and has no shell: `docker exec` is not available. `.dockerignore` keeps the build context to the Go sources; the E2E harness is not part of the image.
