# Technical Architecture

## 1. Overview

Single docker container Go application built on PocketBase as an embedded framework (https://pocketbase.io/). The binary contains:

- PocketBase's HTTP/API server and admin dashboard;
- the custom review dashboard — a static frontend whose files live in `pb_public/` and are served by PocketBase's built-in static file serving;
- the S3 poller and the PDF processing pipeline, executed through PocketBase's standard job scheduler (`app.Cron()`);
- the declarative rule engine (JSONC config + `expr-lang/expr` expressions);
- access to the self-hosted S3 storage via the MinIO Go SDK.

Everything runs offline on the server infrastructure: no cloud APIs, no outbound calls other than to the S3 cluster.

## 2. Stack

| Component | Choice |
|---|---|
| Framework / runtime | PocketBase v0.41.1 (Go framework, single binary; module `ganigeorgiev.com/pocketbase/pocketbase`) |
| Review dashboard | static HTML/CSS/JS in `pb_public/`, no JS build toolchain (htmx or Alpine vendored as a local file, never loaded from a CDN: the application is 100% offline) |
| Language | Go |
| Scheduling | `app.Cron()` (S3 poller, periodic tasks) |
| S3 | MinIO Go SDK (`github.com/minio/minio-go/v7`) |
| PDF text extraction | `github.com/ledongthuc/pdf` (native text, no OCR) |
| Rule expressions | `github.com/expr-lang/expr` |
| Regex | stdlib `regexp` (RE2); `regexp2` only if a lookaround proves indispensable |
| Config validation | `santhosh-tekuri/jsonschema` (schema generated from structs via `invopop/jsonschema`) |
| Database | SQLite (embedded in PocketBase) |
| Full-text search | Standard PocketBase record filter on the `pages` JSON field; no separate index |
| Logging | PocketBase `app.Logger()` (Dashboard > Logs); stderr before bootstrap |

## 3. S3 Layout

A single bucket. Versioning is not required, but it is strongly recommended: without it, any deletion performed by the application (or by a bug) is final; with it, delete markers keep deleted objects recoverable.

```
inbox/       # drop zone for new PDFs (via Nextcloud / S3 client)
pending/     # files awaiting human review
files/       # renamed and filed files (configured prefix structure)
```

The three prefixes are configurable through environment variables: `ARCHIVIST_S3_INBOX_PREFIX`, `ARCHIVIST_S3_PENDING_PREFIX` and `ARCHIVIST_S3_FILES_PREFIX`, all optional with the defaults shown above. Surrounding slashes are normalized; `ARCHIVIST_S3_FILES_PREFIX=/` files documents at the bucket root (the reconciliation scan of section 7.5 then covers the whole bucket, with the inbox and pending keys still excluded); the inbox and pending prefixes cannot be empty, and the three prefixes must be distinct — the application refuses to start otherwise.

Nextcloud mounts the bucket as external storage: it is the user-facing interface for uploading, downloading, and sharing. The application only manipulates S3 keys, never a local filesystem for documents.

## 4. Data Model (PocketBase Collections)

### `documents`
One row per processed PDF file:

- `sha256`: a unique-indexed column — the system rejects as duplicate any file with the same hash
- `file_key`: current S3 key of the file
- `pages`: page-by-page extracted text as a JSON array (0-based, `pages[0]` = page 1); the list's full-text search applies a server-side contains filter to this field, covering the whole document
- `status`: `pending` | `processed` | `validated`
- `created` / `updated`: the standard PocketBase autodate columns, added for convenience in the admin UI; the authoritative history remains `document_events`
+ all the fields declared by the user in the configuration file

The append-only `document_events` collection is the single source of truth for a document's history; no history is stored on the `documents` record itself.

### `document_events`
Append-only history: `document`, `event_type` (`imported`, `corrected`, `validated`, `relocated`, `reprocessed`), `payload` as JSON, `timestamp`, plus the standard `created`/`updated` autodate columns; the event's business timestamp is `timestamp`, mirrored in the payload.

The payload is self-contained: each event carries its own type, its own timestamp and the document state before and after, so a document's full history is reconstructed from this collection alone; the snapshots are stored as diffs — each `before`/`after` carries only the fields the event changes. All dates are RFC 3339; all S3 keys are complete, prefix included. The examples in this section use the default prefixes (section 3).

Common payload keys:

- `event_type`: `imported` | `corrected` | `validated` | `relocated` | `reprocessed`
- `timestamp`: RFC 3339 date of the event
- `before` / `after`: the event's diff, never full snapshots — a key appears on a side only when its value changes in the event: same value on both sides, it is omitted; absent from `before` and present in `after`, the field was added (every `after` key at import, since nothing existed before); present in `before` and absent from `after`, the field was deleted. `file_key` (the S3 key, unified with the `documents` record field) follows the same rule and appears only when the object actually moves; at import the status does not exist before, so `before` carries no `status`. A missing key means the value is unknown, there is no `null`

Per event type:

| Key | imported | corrected | validated | relocated |
|---|---|---|---|---|
| `event_type` | always | always | always | always |
| `timestamp` | always | always | always | always |
| `errors` | on failure | — | — | — |
| `before` | always | always | always | always |
| `after` | always | always | always | always |

`imported` — created after filing (valid or not), one event for the whole import + classification:

```json
{
  "event_type": "imported",
  "timestamp": "2026-01-15T14:23:07Z",
  "before": {
    "file_key": "inbox/scan_2026-01-15.pdf"
  },
  "after": {
    "file_key": "files/BRED/Décembre 2025_Relevé de compte.pdf",
    "status": "processed",
    "emittingCompany": "BRED",
    "documentType": "Relevé de compte",
    "emissionDate": "2026-01-15T00:00:00Z",
    "template": "bank_statement"
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

`corrected` — created whenever declared fields are modified, whatever the interface. A field edited from the standard PocketBase UI is a draft correction (US-22): the values persist as they are, no template evaluation, no re-filing, no status change, and the snapshots carry the field diff alone. A dashboard correction (US-36) adds the side effects: the file is re-filed and the status moves to `validated`, so `file_key` and `status` join the diff:

```json
{
  "event_type": "corrected",
  "timestamp": "2026-01-18T09:41:52Z",
  "before": {
    "file_key": "pending/scan_2026-01-15.pdf",
    "status": "pending",
    "emittingCompany": "BRED",
    "emissionDate": "2026-01-15T00:00:00Z"
  },
  "after": {
    "file_key": "files/Fortunéo/Janvier 2026_Relevé de compte.pdf",
    "status": "validated",
    "emittingCompany": "Fortunéo",
    "emissionDate": "2026-02-15T00:00:00Z",
    "template": "bank_statement"
  }
}
```

The diff is derived by comparing the two snapshots: a field present in `before` and absent from `after` was deleted; the reverse means it was added (`template` above).

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

`reprocessed` — the reverse transition, from `validated` back to `processed` (validation undone, from the bulk actions of the list page like the validation itself): no re-filing, no field change, the snapshots carry the status alone, and a `pending` status is never reachable this way:

```json
{
  "event_type": "reprocessed",
  "timestamp": "2026-01-21T09:02:45Z",
  "before": {
    "status": "validated"
  },
  "after": {
    "status": "processed"
  }
}
```

`relocated` — emitted by the consistency tools (section 7.5) when an object of the bucket was moved outside the application: the record's `file_key` is corrected to the key where the object was found (the object itself never moves), and the diff carries `file_key` alone:

```json
{
  "event_type": "relocated",
  "timestamp": "2026-02-01T10:12:44Z",
  "before": {
    "file_key": "files/BRED/Décembre 2025_Relevé de compte.pdf"
  },
  "after": {
    "file_key": "files/BRED/Archives/Décembre 2025_Relevé de compte.pdf"
  }
}
```


## 5. Configuration File (JSONC)

A `config.jsonc` file kept alongside the binary, reloaded on restart. Three sections:

1. `fields`: declaration of fields to extract from the files:
   - `name`: name of the field
    - `french`: required French title displayed by the dashboard
   - `type`: `text` | `number` | `date` | `boolean` | `enum`
   - `mandatory`: true | false

   Type semantics in rule expressions:
   - `number` and `boolean` expose native values
   - `date` exposes a native Go `time.Time` (comparable, arithmetic via `duration()`, usable with the `date()`/`now()` builtins)
   - `text` and `enum` expose strings
   - `enum` does not declare its values: the possible values live in the `documents` schema itself (a PocketBase select field, single value) and are managed by the application — at collection creation the list holds the single dummy value `-----`, a value assigned by the rules or entered from the dashboard is added to the list when missing, a startup pass removes the values no document uses anymore (the dummy stays only if the list would otherwise be empty), and the `template` field is the exception: its list always equals the template names of the configuration; the dashboard dropdowns offer only the possible values, completed by an add-a-value modal
   - assigning a value that does not match the field type (e.g. a raw string to a `date` field, a non-numeric value to a `number` field) is a rule error and routes the document to `pending/`

2. `rules`: an ordered tree of rules. Each rule contains four keys:
   - `when` (required): an `expr-lang/expr` expression evaluated against the document pages (`pages`) and the current field values; the rest of the rule runs only if it returns true (e.g. `pages[0] contains `Relevé de compte``, or `emittingCompany in [`BRED`, `Fortunéo`] && documentType == `Relevé de compte``)
   - `set`: list of assignments, evaluated in order — `{ "field": ..., "value": ... }` for a constant, `{ "field": ..., "eval": ... }` for an expression evaluated in the same context
   - `delete`: list of field names to unset (they become undefined again for later rules and template rendering)
   - `rules`: child rules, evaluated in order only if the parent `when` matched; nesting is recursive

   The expression environment provides expr's standard `date(value, layout)` builtin plus three built-in helpers registered by the application:

   - `date(value, layout)`: expr's standard builtin. Parses a string into a native date using a Go date layout (e.g. `date(`15/01/2026`, `02/01/2006`)`). The format is carried by the parsing rule, so it can differ per document type. It only knows English month names: see `frDate` below for French text.
   - `firstMatch(regex, texts?)`: returns the first match of the regex in the document text (first capture group when present), or an empty string when no match is found — e.g. `firstMatch(`[0-9]{2}/[0-9]{2}/[0-9]{4}`, pages[0])`. The optional second argument is an expression evaluated to the text(s) to search:
     - a single page: `pages[0]` (the first page)
     - a slice: `pages[0:3]` (pages 1 to 3, expr's standard slice syntax, end exclusive)
     - any list of strings
     - when omitted, the whole document is searched
     Parsing an empty result with `date()` is a rule error and routes the document to `pending/`.
   - `frDate(text)`: extracts and parses a French date — the layout Go cannot express for the natural-language form, since `date(value, layout)` only knows English month names.
     - It matches `le 19 janvier 2026`; the day and the leading `le` are optional (`Janvier 2026` gives the first of the month), `1er` and abbreviated month names like `févr.` are accepted, accents are optional.
     - It also matches a numeric date `jj/mm/aaaa` or `jj/mm/aa` (e.g. `Le : 25/09/26` or `du 15/01/2026`); a two-digit year follows Go's `06` convention (00–68 → the 2000s, 69–99 → the 1900s).
     - When the text holds several dates, the earliest occurrence wins, the natural-language form taking precedence when both start at the same position; an impossible numeric date (`31/02/26`) behaves like an impossible natural-language one (31 février).
     - It returns a native date — e.g. `frDate(firstMatch(`(?:le )?\d{1,2} \S+ \d{4}`, pages[0]))`, or `frDate(pages[0])` when the first French-looking date of the page is the right one.
     - A text containing no French date, or an impossible date (31 février, 31/02/26), is a rule error and routes the document to `pending/`.
   - `frPeriod(value, unit, shift, layout?)`: renders a period instead of a raw date.
     - `value`: a native date (e.g. `emissionDate`); a missing or invalid date is a rule error and routes the document to `pending/`.
     - `unit`: `month` | `quarter` | `half` | `year`.
     - `shift`: `-1` (previous period), `0` (period containing the date) or `+1` (next period) — e.g. a rent receipt issued mid-month uses the containing month, a service-charge call issued at the end of a quarter uses the next quarter, a bank statement uses the previous month.
     - Returns a French string: `Janvier 2026`, `T2 2026`, `S1 2026` ou `2026`.
     - The optional `layout` argument is a standard Go reference-date layout applied to the period's first day, with month names rendered in French by the application (default layouts: `January 2006` for `month`, `2006` for `year`); it is ignored for `quarter` and `half`, whose formats (`T<quarter> <year>`, `S<half> <year>`) are fixed.

3. `templates`: named naming templates (e.g. `bank_statement: {emittingCompany}/{frPeriod(emissionDate, `month`, -1)}_{documentType}_{bankAccount}.pdf`). A template is a literal string containing `{...}` placeholders:
   - the inside of each placeholder is an `expr-lang/expr` expression evaluated in the document context (fields, helpers, builtins — e.g. `{emissionDate.Format(`2006-01-02`)}` or `{upper(emittingCompany)}`) and replaced by its result
   - the static text between placeholders stays literal
   - unset optional fields evaluate to an empty string
   - a missing mandatory field or an evaluation error renders the result invalid and routes the file to `pending/`
   - all placeholder expressions are compiled at config load

   The template is chosen by the rules themselves: `template` is a field declared in the `fields` section like any other and set with `set` (e.g. `set template = `bank_statement`` when `emittingCompany in [`BRED`, `Fortunéo`] and documentType == `Relevé de compte``). If no rule sets `template`, the special `default` template is used. A `template` value with no matching entry renders the result invalid and routes the file to `pending/`.

Validation on load: JSONC decoding and field declaration checks (including a non-empty `french` title), regex compilation, `expr` expression compilation (`when`, `eval` and template placeholder expressions) against a typed context (`Document{Pages, Fields}`) with the `firstMatch`, `frDate` and `frPeriod` helpers, regex precompilation and caching. Full rejection if invalid, no partial processing.

## 6. Processing Pipeline

Executed by the PocketBase cron job (configurable interval, e.g. every minute):

1. **Poll**: list objects in the configured inbox prefix (default `inbox/`, section 3); an object is a candidate only if no `documents` record references its key yet, and its size and ETag are stable across two consecutive polls (write-completion wait). The same pass also picks up interrupted filings: any `documents` record whose `file_key` is still under the inbox prefix (a filing that was interrupted between record creation and the physical move) is completed as described below.
2. **Download**: fetch the object into memory (streamed), no disk writes. The sha256 is computed during download; if a `documents` record with the same hash already exists, the object is a duplicate: the inbox key is deleted (a no-data-loss case — the original with the same content stays filed), a message is logged via the PocketBase logger (section 9), and processing stops there. By design, no `document_events` entry is recorded: the duplicate is not a document of its own, and the trace lives in the PocketBase logs only. An object that is not a PDF (its content does not start with the `%PDF-` header) is left as-is in the inbox prefix: it is never imported, gets no record and no event; the skip is logged at Warn level once per unchanged content (the object is not re-downloaded on later passes until its ETag changes) and processing stops there.
3. **Extraction**: page-by-page text (one entry per page, for rule evaluation and the FTS index) via `ledongthuc/pdf`, including headers and footers
4. **Rule evaluation**: rules evaluated in order against the document pages and the fields set so far; a structured evaluation trace — every rule that modified a value (`set`/`delete`) or errored, with the field, the final value and the producing `eval` expression — is written to the PocketBase logger at Debug level (section 9) and returned as the `trace` field of the classification dry-run (section 7.3).
5. **Validation**: a result is valid if and only if: all rules have been processed with no error, all mandatory fields are identified, the selected template exists in the configuration, the naming template evaluated with no error, and no rendered placeholder value contains a `/` (the S3 key delimiter). Otherwise: route directly to `pending/`.
6. **Record**: create the `documents` record (sha256, current inbox key, pages, fields, status) and add an `imported` event including the original key (`before.file_key`); for enum fields, any value missing from the field's possible-values list is registered in the list before the record is saved (section 5). The record exists before any physical move, so an interrupted filing leaves a state the poller recovers automatically on its next pass.
7. **Filing**:
   - valid → resolve the target name first: if a file with the same name already exists in the target prefix, append a number to the filename; then copy to the new key (the configured files prefix + rendered template — the template renders the full object path, there is no separate prefix), verify the target object exists, and delete the inbox key only after the copy is confirmed; finally update the `documents` record with the final, conflict-resolved filename (`file_key`, status `processed`); the whole import is recorded as the single `imported` event of section 4.
   - invalid → move to the pending prefix (incremental suffix on name conflict); update `file_key`; status `pending`.

When the bucket is versioned, any deletion creates a delete marker and the data remains recoverable; when it is not, deletions are final. The application logs a warning at startup when versioning is disabled, recommending the user to enable it to reduce the risk of losing a file to a bug.

## 7. Review Dashboard

The review dashboard is a static frontend — plain HTML/CSS/JS files, no build toolchain, vendored dependencies only (no CDN: the application is 100% offline) — served by PocketBase's built-in static file serving from the `pb_public/` directory shipped in the image next to `pb_data/`. Four pages: the document list, the document detail, the tools page (section 7.5), and the configuration editor (section 7.6). document data persists through the standard PocketBase API and its request hooks (US-22 / US-23 / US-24); the configuration editor uses dedicated superuser-only JSON routes because it edits and activates the functional configuration rather than a record. The cross-cutting interface principles and conventions shared by these pages — technology, authentication, visual language, typed controls, interaction, error display, accessibility — are specified in `interface.md`, which this section does not repeat.

### 7.1 Document list (US-32 / US-33)

- Table: one row per document, paginated (50 by default), default columns: all mandatory fields plus `status` and `file_key`; a column chooser exposes every declared field plus `created`/`updated`, and the choice persists in the browser (`localStorage`).
- Sorting: click on a column header, ascending/descending, executed server-side through the standard PocketBase `sort` parameter.
- Per-column filters, typed from the field declaration: contains for `text`, dropdown for `enum` and `status` (vocabulary from the config metadata route, plus an `empty` entry matching the documents whose field holds no value), date range for `date`, tri-state for `boolean`; translated client-side into a PocketBase filter expression.
- Quick tabs with the per-status counts pre-filter the review queue (`pending` first).
- Full-text search: a field above the table searches case-insensitively across every page in `pages`; after 1.5 seconds without typing, it adds a server-side PocketBase contains filter on `pages`. It combines with status tabs and per-column filters, returns matching documents without excerpts, and uses no dedicated route or FTS index.
- Bulk edit (US-33): row selection through the header checkbox cycling none → page → the whole filtered set → none (indeterminate display while the selection is partial or page-limited, the tooltip names the next step; changing any filter — status tab, column filter, page search — empties the selection; no dedicated select-all or clear button), then one declared field and its new value in a control matching the type, or the `status` field whose dropdown offers `validated` and `processed`; applied as one standard PATCH per document, so the US-22/US-23 hook runs per record: a `corrected` event each (a `validated`/`reprocessed` event for the status), no re-filing; per-document success and failure are reported, one failure does not stop the rest.
- Bulk recompute (US-33): instead of a manual value, the recompute mode takes the field's proposal from the classification dry-run of US-35 (no body, re-run against the stored pages) per selected document, and PATCHes only the documents whose current value differs from it; missing proposals and equal values leave the document untouched, an enum proposal is registered in the field's list first (US-37), and no route beyond the existing dry-run and PATCH is needed.
- Selection re-file (US-44): a fifth icon action, separated from the field-editing actions by a vertical bar, re-files the selected documents through `POST /api/archivist/documents/{id}/refile` — one request per document, reusing the refile-all tool's shared per-document logic (`RefileOne`); pending documents are refused, unchanged documents are counted and untouched, skipped ones are reported with their reason, and the summary shows moved / unchanged / skipped / failed counts.
- Selection re-run (US-46): a re-run button next to the re-file action re-runs the whole rule engine on the selected documents through `POST /api/archivist/documents/rerun/{id}` — one request per document, the same engine call as the import (`EvaluateWithTrace`) applied to the stored pages: the proposal replaces the declared fields, the status goes back to `processed`, and the object re-filing reuses `RefileOne`'s move sequence; a proposal with errors leaves the document untouched (`skipped`, a skip never counted as an error), an identical proposal is `unchanged`, a `pending` document is refused, and the summary shows updated / unchanged / skipped / failed counts.
- Bulk delete (US-45): a delete icon, separated from the re-file action by a vertical bar, deletes the selected documents through the standard delete route — one request per document, guarded by a confirmation dialog showing the number of documents. The US-24 hook deletes the S3 object before the record and refuses the whole deletion when that fails, the cascade delete of `document_events` removes the history with the record, and the summary shows the deleted and failed counts.

### 7.2 Document detail (US-34 to US-36)

- Layout: two columns — the PDF preview embedded via the proxy route of section 7.3, and the correction form; below them, the document's event history, read-only, sorted by timestamp, payloads rendered as before/after diffs.
- Form: one typed control per declared field, pre-filled with the current values; mandatory fields marked; the classification errors of the `imported` event, if any, displayed at the top.
- Pipeline proposals (US-35): under each field whose value the pipeline would extract differently today, a discreet banner offers the proposed value with one-click adoption. The proposals come from the classification dry-run (section 7.3), re-run against the stored pages.
- Live name (US-35): as the form changes, the full target key (files prefix included) — the `rendered` value of the dry-run, used as-is — is recomputed by the render dry-run and displayed with its errors; the draft save remains available, but the full correction refuses to run while errors remain.
- Rule trace (US-42): below the event history, a panel collapsed by default shows how the rules evaluate the document today. Its header shows a badge with the error count when the dry-run returned errors; unfolded, it first lists the dry-run errors (classification and name rendering) in the error style, then one line per traced rule: the rule's `when` expression (rules carry no name), the field, the producing `eval` expression, final value (deletions marked as unset, failing rules with their error message). The trace comes from the classification dry-run re-run against the stored pages, fetched in the same request as the proposals; it reflects what the rules would do today, not what happened at import, and nothing is persisted.
- Exits, three and distinct: draft save (standard PATCH, US-22: values persist, no re-filing, no status change); full correction (US-36: the `/correct` route, re-filing and validation in one confirmed step); simple validation of a `processed` document (standard PATCH, US-23). A `pending` document only leaves the review queue through the full correction.

### 7.3 Application routes

Beyond the standard PocketBase API, authenticated only:

- `GET /api/archivist/config` — the configuration metadata the UI needs: declared fields (name, french, type, mandatory), the enum vocabularies read from the `documents` collection schema (dummy placeholder excluded), the template names. Read-only; the dashboard is entirely driven by it (columns, filters, controls).
- `GET /api/archivist/config/source` — returns the active functional configuration source as JSON (`{"source":"..."}`), including its JSONC comments, for the configuration editor. Superuser-only.
- `POST /api/archivist/config/dry-run` — accepts `{"source":"...","pages":[...]}` and validates/compiles the submitted JSONC independently of the active configuration. If valid, evaluates its rules and templates against the supplied page texts and returns `fields`, `template`, `rendered`, `errors`, and `trace`, matching the document classification dry-run response. Configuration parse/validation failures return an error response and never change active state; classification and rendering errors are returned in `errors`. It persists nothing and does not access S3 or the database. Superuser-only.
- `POST /api/archivist/config/when-match` — the "impacted files" test of the configuration editor section headers (US-50). Accepts `{"source":"...","when":["..."]}`: the draft source and the ordered when chain of one rule (its parents first, its own when last). On a valid compiled draft the chain is evaluated in document context against every document's stored pages and fields, whatever its status; the response lists the matching documents (id, file_key, status) and persists nothing and touches no S3 object. Configuration and when compilation failures are returned as 422 error responses. Superuser-only.
- `PUT /api/archivist/config` — accepts `{"source":"..."}`, fully validates and compiles it, writes it to the active configuration path using a temporary sibling file and atomic rename, then activates the compiled configuration for the poller and API routes. The containing directory must be writable. On validation or write failure, the active configuration and file remain unchanged and the response identifies the errors. Superuser-only.
- `POST /api/archivist/fields/{field}/values` — registers a value in the possible-values list of an enum field (JSON body `{"value": "..."}`); refused on an unknown or non-enum field, an empty value, the reserved dummy `-----` and the `template` field (its list follows the configuration alone); returns the field's updated list. The add-a-value modal of the dashboard dropdowns calls it; the pipeline registers new values on its own before saving.
- `GET /api/archivist/documents/{id}/pdf` — proxy route streaming the document from S3; the browser never reaches the S3 cluster.
- `POST /api/archivist/documents/{id}/dry-run` — two exclusive modes, one response shape (`fields`, `template`, `rendered`, `errors`), persisting nothing; `rendered` is the full target S3 key, files prefix included (and empty when an error remains), so no client ever assembles the key itself. Without a body: a classification re-run — the rule tree evaluated against the stored pages, returning the proposed fields (the source of the pipeline proposals) plus a `trace` field: the flat, ordered list of the rules that modified a value (`set`/`delete`) or errored during the evaluation — each entry carrying the rule's `when` expression as its rule identifier (rules carry no name), the field, the final value after the operation (marked as unset for a `delete`), the literal `eval` expression that produced it (a constant `set` returns its own value) and the error message for a failing rule; rules that match without modifying anything and non-matching rules are omitted. The trace reflects what the rules would do today, not what happened at import (US-42). With field values: a template render — the values converted to their native types (per-field error on type mismatch), merged with the record values, rendered against the stored pages (placeholders may call `firstMatch`/`frDate` on the document text), returning the target key and its errors. The modes are never hybrid: user values never enter the rule-evaluation context, so the proposal always stays what the pipeline would do alone.
- `POST /api/archivist/documents/{id}/refile` — the selection re-file of US-44: the per-document re-file of US-43 through the shared `RefileOne` (the refile-all tool's core, also driving the tools job), so no parallel business logic exists. Refused for a `pending` document; returns the outcome (`moved` with the final target key, `unchanged`, or `skipped` with the reason). A route because the intent (re-render + physical move + relocated event) is not expressible in record data, like `/correct`.
- `POST /api/archivist/documents/{id}/rerun` — the selection re-run of US-46: the whole rule engine re-evaluated against the stored pages through the shared `ReclassifyOne` (same engine call as the import), the fields replaced by the proposal, the status set back to `processed`, then the re-filing of `RefileOne` on the rendered template; refused for a `pending` document; a proposal with errors skips the document entirely (outcome `skipped` with the reason, nothing written); returns the outcome (`updated` with the final target key, `unchanged`, or `skipped`). The change commits in one transaction (record save + `corrected` event with the full diff) with a best-effort S3 rollback on mid-failure — the same line drawn by `/correct`.
- `POST /api/archivist/documents/{id}/correct` — the full correction of US-36: transactional orchestration (conflict-suffix resolution, S3 copy, verification, old key deletion, record update with `file_key` and status `validated`, `corrected` event carrying the full diff, S3 rollback on mid-failure with the save refused). It reuses the engine internals (`FieldsFromRecord`, `ValidateValue`, `Render`, `ResolveConflict`, `DiffSnapshots`, `AppendEvent`), no parallel business logic. A route rather than a standard PATCH because the intent (re-file + validate) is not expressible in record data, the standard PATCH must stay side-effect-free for every client and the bulk edit (US-22 / US-33), and the operation is a multi-step transaction rather than a record save — the same line drawn by `POST /api/archivist/poll`.

### 7.4 Standard collections UI and hooks

- Correction: performed directly from the standard PocketBase collections UI (or any PocketBase client) by editing the extracted fields. The application's `OnRecordUpdateRequest` hook on `documents` intercepts the save and appends the `corrected` event carrying the field diff — and nothing else. The UI edits one field at a time with no preview of the resulting file name, so the hook deliberately performs no template evaluation and no re-filing, and the status does not move: an inconsistent draft cannot trigger irreversible side effects. The full correction — all fields at once, dry-run of the resulting name, re-filing and validation — is the dashboard's job (US-34 to US-36).
- Validation: performed from the same interface by changing the status to `validated`; the hook appends the `validated` event. The reverse transition `validated` → `processed` is accepted the same way and appends a `reprocessed` event. Any other status change (`pending`, any third state) is refused. A `pending` file can only leave the review queue through a full correction from the dashboard (US-36).
- Deletion: deleting a `documents` record from the same interface first deletes the S3 object at `file_key` (US-24); on S3 failure the deletion is refused and the record stays. The cascade delete of the `document_events` relation removes the history with the record; with versioning the object stays recoverable via its delete marker.

The static pages themselves carry no data and are public by nature; every fetch is authenticated against the PocketBase API. An unauthenticated visitor is prompted to sign in with the superuser account; the dashboard exchanges those credentials through PocketBase's standard auth endpoint, stores the returned token locally, and retries the blocked request. The API continues to enforce superuser access. The configuration is loaded at startup and can also be replaced at runtime through the validated configuration editor save flow in section 7.6.

### 7.5 Tools page (US-40, US-41, US-43)

A third static page, `tools.html` + `tools.js`, linked from the dashboard's top bar: one card per tool, with its description and a run button; while a job runs, a progress bar (processed / total objects, current key) refreshed by polling; when it ends, a summary (per-outcome counts) plus the detail lists — for `find-lost-files`, every impacted object category by category (corrections old key → found key, unknown objects with their inbox destination, stray copies, missing keys), for `refile-all`, the planned moves and the skipped documents — and the per-object errors. One job at a time; a running job can be cancelled at the next object boundary. The panel is restored on page reload from the latest job state.

The tools are long-running maintenance tasks started by hand, the exact opposite of the cron poller: each run executes as a background job in a `internal/tools` registry (id, status `running`/`done`/`failed`/`cancelled`, progress, summary), served by dedicated routes and never blocking the pipeline.

First tool, `find-lost-files` (US-41) — reconciling the bucket with the database when files have been moved by hand (e.g. through Nextcloud):

1. List only `.pdf` objects (extension matched case-insensitively) recursively under the pending and files prefixes — the whole bucket when files is the root; inbox keys excluded in every case. Objects under the inbox prefix and objects with another extension are ignored.
2. For each listed object, stream-compute its sha256 and look up a `documents` record by hash:
   - the record's `file_key` equals the object key: nothing to do (`ok`);
   - the record's `file_key` no longer exists in the bucket: the file was moved outside the application — the record's `file_key` is corrected to the found key and a `relocated` event is appended (the object itself never moves); refused if another record already references the key (`corrected`);
   - the record's `file_key` still exists: the scanned object is a stray copy — left in place and reported (`duplicates`);
   - no record at all: the object is unknown of the database — moved to the inbox prefix (conflict suffix on name collision) so the poller processes it automatically (`moved`).
3. After the scan, every record whose `file_key` is a `.pdf` under the pending or files prefix (the whole bucket when files is the root) but has no corresponding listed object is reported as missing, never acted upon (`missing`). Other keys, including interrupted filings under the inbox prefix, are not reported. A record whose object was found under another key is a `corrected`, not a `missing` — in simulation the record is not updated, so the planned correction is remembered and excluded from the missing list: the two categories never overlap. The outcomes add up exactly: scanned = ok + corrected + moved + duplicates + errors.

Every outcome is logged through the PocketBase logger; per-object failures do not stop the scan and join the job's error list.

Second tool, `refile-all` (US-43) — applying a change of the files prefix or of a naming template to the documents already filed:

1. Select the `documents` records with status `processed` or `validated`; documents pending remain in the review queue and are never touched.
2. For each record, render its stored `template` (the `default` one when the record carries no template) from the fields stored in the record, under the active compiled configuration, and compute the full target key under the active files prefix:
   - the target equals the current `file_key`: nothing to do (`unchanged`);
   - the stored template does not exist in the active configuration, or the render fails or renders an empty key: the object is left in place and the reason is listed in the summary (`skipped`) — a skip is not an error;
   - the target differs: the object is moved to the target (conflict suffix on name collision; copy first, existence verified, then the source removed), the record's `file_key` is updated and a `relocated` event is appended with the `file_key` diff — both in one transaction, with a best-effort S3 rollback of the move if the save fails (`moved`).

Every outcome is logged through the PocketBase logger; per-document failures do not stop the re-filing and join the job's error list. The job is cancellable at the next document boundary.

Each tool runs for real or in dry-run mode (the simulation button of the tools page). A dry-run performs every check of the tool but writes nothing — no S3 move, no record update, no event — and reports what would happen through the same counts and detail lists as a real run (per tool, see above), with, over refile-all, the list of the planned moves, each "old → new" and marked when a collision suffix would be applied; the planned name collisions are resolved against the current bucket contents and against the moves already planned by the same run (the resolution is read-only). A dry-run job keeps `dry_run: true` in its state.

Routes (superuser-only, same rules as section 7.3):

- `GET /api/archivist/tools` — the tool descriptors (id, name, description) and the latest job state (running or last finished).
- `POST /api/archivist/tools/{id}/run` — starts the job, with an optional body `{"dry_run": true}` for a simulation; refused with 409 while one is running; a non-JSON non-empty body is refused with 400.
- `GET /api/archivist/tools/jobs/{id}` — the job's live state (progress, summary, lists, `dry_run`, planned moves).
- `POST /api/archivist/tools/jobs/{id}/cancel` — asks the job to stop at the next object boundary.

### 7.6 Configuration editor (US-50 to US-52)

The `config.html` page is linked from the document list and document detail. It is a static, superuser-only editor for the active JSONC functional configuration. The source is read from the exact path selected by `ARCHIVIST_CONFIG` or `--config`; the containing directory must be writable by the application user for save-and-activate. A successful save updates the file and activates the compiled configuration without a restart.

The page has two columns:

- Left: three editable blocks in configuration order. `fields` is a table with name, French title, type and mandatory controls; rows can be added or removed. `rules` are shown as editable JSONC text, grouped into collapsible sections whose headings are the original comments beginning `// --- ` — each heading carries a pencil control opening a rename dialog that deliberately rewrites that section's separator comment, a magnifier opening the impacted-files popup (the chain of when of each rule, parents first, as single-selection radio choices; the lower part lists the matching documents with their key and status, evaluated under the current draft over every document whatever its status) and a trash control; group order and separator comments are otherwise preserved when editing and saving. `templates` is a table of template name and template text; rows can be added or removed.
- Right: a large textarea accepts the JSON array of page texts stored in `documents.pages`. With no document context it starts empty. With a document context it is populated from that record; previous/next controls use the same document order as the list page. The field proposals, classification/rendering errors, rendered target and rule trace use the same presentation as the document detail page.

Any edit to the configuration or page array schedules a debounced draft dry-run. Draft evaluation compiles the complete draft and runs the normal classification and rendering engine against the supplied pages, including `EvaluateWithTrace`; it does not persist data. Invalid JSONC or configuration displays validation errors and suppresses classification results until corrected. Navigating to the previous or next document preserves the configuration draft and re-runs it against the newly selected document's pages. When opened without a document context, document navigation is hidden.

Saving is explicit. The save action validates and compiles the complete draft before writing. A validation or filesystem error leaves the previous file and in-memory configuration active and keeps the draft on screen with errors. After a successful atomic file replacement, the new compiled configuration becomes active for subsequent polls and requests. Existing requests already in progress may finish using the configuration they started with.

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

- starts a disposable Garage (`dxflrs/garage:v2.5.0` by default, `GARAGE_IMAGE` overrides) on a private docker network, bootstraps its layout, bucket and key, and publishes its S3 API on 127.0.0.1:3900;
- starts the image under test with a minimal `config.jsonc` embedded in the test file and the cron disabled;
- drives everything through the PocketBase HTTP API and the S3 API; test PDFs are generated in memory from a minimal one-page template, so the classification outcome is the proof that extraction works, without any fixture.

The scenarios form one narrative; each subtest names the US it proves:

1. Startup — health, collections migrated, versioning warning in the PocketBase logs (Garage has no versioning: the US-01 warning path is exercised for free), poll route refused without superuser auth (US-01, US-03, US-04).
2. Import — a valid simulated statement is classified and filed to `files/` with its `imported` event (US-10 to US-17).
3. Duplicate — the same content re-uploaded is deleted from `inbox/`, no record, no event; a non-PDF object uploaded to `inbox/` is left in place, never imported (US-12).
4. Pending — a missing mandatory date routes the document to `pending/` with `errors` in the imported event and the recognized fields kept (US-13 to US-15).
5. Correction — a field PATCH through the API persists the values with no side effect (no re-filing, no status change), `corrected` event carrying the field diff (US-22).
6. Validation — a status-only PATCH appends the `validated` event, `file_key` untouched; the reverse PATCH back to `processed` appends the `reprocessed` event and a change to `pending` is refused (US-23).
7. Append-only — PATCH and DELETE on `document_events` are refused (US-04).
8. Deletion — a deleted record removes the S3 object; the history follows via the cascade delete (US-24).
9. Recovery — a stuck record (`file_key` still under `inbox/`, created directly through the API) is completed by the next pass (US-11).
10. Restart — the container restarts with all state intact.
11. Config metadata — the configuration route serves the declared fields, the enum vocabularies and the template names (US-31).
12. PDF proxy — the route streams the S3 object, refused without superuser auth (US-34).
13. Dry-run — both modes return the shared response shape and persist nothing; the classification mode also returns the rule trace (modifying and failing rules only, in evaluation order) (US-35, US-42).
14. Full correction — a pending document is re-filed and validated; value errors leave everything unchanged; an unchanged name skips the S3 move and the diff drops file_key (US-36).
15. Static dashboard — the review pages are served from pb_public/ with no external resource (US-30).
16. Enum lifecycle — the value-registration route extends the possible-values list and its refusals hold; a restart drops an unused added value, keeps a used one, and the `template` list follows the configuration (US-37).
17. Tools — the tools route serves the descriptors and refuses unauthenticated calls; a scan considers only PDFs under `pending/` and `files/`, ignores other prefixes and extensions, and produces the matching summary for an in-place file, a relocated file, an unknown file and a deleted file (ok, corrected with `relocated` event, moved to `inbox/`, missing reported); the re-filing follows the active templates and files prefix (skipped on an unknown template, never touching a `pending` document, `relocated` event on a move) and a dry-run reports the same counts without moving any object, correcting any `file_key` or appending any event; the list-page re-file route moves one document, is unchanged on a second call and refuses a `pending` one; the list-page re-run route re-classifies a document from its stored pages and re-files it (US-40, US-41, US-43, US-44, US-46).
18. Configuration editor — the source route is superuser-only; the draft dry-run returns fields and trace without persistence; the when-match route matches the filed statements and refused invalid drafts, empty chains and unauthenticated calls; valid save updates the active file and configuration; invalid JSONC or an unwritable directory leaves the old active configuration unchanged (US-50 to US-52).
19. Prefixes — a second app container started with custom prefixes (inbox `in/`, pending `wait/`, files at the root) imports an upload filed directly at the bucket root, without the historical files prefix; a container started with an invalid pending prefix exits at startup with an explicit error (US-01).

`POST /api/archivist/poll` (superuser only) runs one poll pass immediately. It exists so the gate and the operator trigger passes on demand instead of waiting for the cron; a fresh upload needs two calls because the poller waits for size/ETag stability across two passes (US-10).

The dashboard application routes are covered by the gate: the config metadata/source/draft-test/save routes, the enum value-registration route, both document dry-run modes, the `/correct` orchestration, the selection re-file route, the selection re-run route, the PDF proxy, and the tools routes with both tools and their dry-run mode; a second app container covers the custom prefixes and the invalid-prefix startup refusal (US-01, US-30 to US-37, US-40, US-41, US-43, US-44, US-46, US-50 to US-52). The gate is a regression detector for dependency upgrades (PocketBase, minio-go, ledongthuc/pdf, expr, Garage), not an acceptance proof: acceptance criteria are reviewed on the stories themselves. `E2E_KEEP=1` leaves the containers running after the run for debugging.

## 11. Packaging

`Dockerfile`, two-stage build:

- Builder: `golang:1.27-alpine` (matches the `go 1.27` directive of PocketBase v0.41.1), CGO disabled, static binary, `TARGETARCH` honored for multi-platform builds under buildx. `go mod tidy` runs there because `go.sum` is not committed; once it is committed, the line becomes `go mod download` for fully reproducible builds.
- Runtime: `scratch` with the `/archivist` binary, the CA bundle (the S3 endpoint is HTTPS by default), the timezone database (the cron and date rendering honor `TZ`), the static review-dashboard frontend copied to `/app/pb_public/` (read-only at runtime), and `/app/pb_data` + `/tmp`, owned by the unprivileged runtime user 65532. `config.jsonc` is never baked in: its containing directory is mounted read-write at runtime so the configuration editor can atomically save it; `ARCHIVIST_CONFIG` defaults to `config.jsonc` relative to `/app`.

The container runs as 65532:65532 (non-root), exposes 8090, declares `/app/pb_data` as a volume, and has no shell: `docker exec` is not available. `.dockerignore` keeps the build context to the Go sources and the dashboard frontend; the E2E harness is not part of the image.
