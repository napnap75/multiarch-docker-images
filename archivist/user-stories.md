# Archivist — User Stories

Backlog for the Archivist application. Each story follows the standard format (As a / I want / So that) with acceptance criteria in Given/When/Then. Stories are numbered; epic tags group them.

## Epic: Startup & Configuration

### US-01 — Start the application from environment variables

As the operator of the container `napnap75/archivist:latest`,
I want the application to read its technical configuration (S3 endpoint and credentials, bucket name, admin account, poll interval, HTTP port) from environment variables,
so that the container starts with no configuration file for infrastructure and secrets stay out of the image.

Acceptance criteria:

- Given the container starts with all mandatory variables set (`ARCHIVIST_S3_ENDPOINT`, `ARCHIVIST_S3_ACCESS_KEY`, `ARCHIVIST_S3_SECRET_KEY`, `ARCHIVIST_S3_BUCKET`, `ARCHIVIST_ADMIN_EMAIL`, `ARCHIVIST_ADMIN_PASSWORD`), when the application starts, then it connects to the S3 server, verifies the bucket is reachable and versioned, and starts serving HTTP on the configured port.
- Given an optional variable is not set, when the application starts, then it uses its documented default (`ARCHIVIST_S3_USE_SSL=true`, `ARCHIVIST_POLL_INTERVAL=* * * * *`, `ARCHIVIST_HTTP_PORT=8090`).
- Given a mandatory variable is missing or the S3 server is unreachable or the bucket is not versioned, when the application starts, then it exits with a non-zero code and a single explicit error message in the logs naming the faulty variable or resource.
- Given the admin account variables are set, when the application starts for the first time, then the PocketBase admin account is created with these credentials; on subsequent starts it is left unchanged unless the password differs, in which case it is updated.

### US-02 — Load and validate the functional configuration

As the operator,
I want the application to load `config.jsonc` at startup and reject it entirely if any part is invalid,
so that no document is ever processed against a partially understood configuration.

Acceptance criteria:

- Given a syntactically valid `config.jsonc` mounted in the container, when the application starts, then the file is parsed with comments allowed and validated against the JSON schema generated from the configuration structs.
- Given the schema is valid, when the application validates further, then every regex compiles, every `expr` expression (`when`, `eval`, template placeholders) compiles against the typed context `Document{Pages, Fields}` with the `firstMatch` and `period` helpers, and every compiled expression is cached for later use.
- Given any validation error (unknown key, bad type, non-compiling regex or expression, unknown field referenced in `set`/`delete`), when the application starts, then it exits with a non-zero code, an error message pinpointing the file location of the first error, and processes nothing. Cross-references are also checked: every template name used in the rules must exist in `templates`.
- Given the configuration is valid, when a field declaration references a type, then the declared type is one of `text`, `number`, `date`, `boolean`, `enum` (schema-enforced).
- Given the configuration is valid, when the application starts, then the effective configuration (fields, rules, templates) is logged in a condensed form for traceability.

### US-03 — Create or update the `documents` collection from the field declarations

As the application,
I want the `documents` PocketBase collection to be created on first start and migrated on every subsequent start to match the current `config.jsonc`,
so that the persistence schema always reflects the declared extraction fields.

Acceptance criteria:

- Given the `documents` collection does not exist, when the application starts, then it is created with the fixed columns (`sha256` unique-indexed, `file_key`, `pages` as a JSON array, `status`) plus one column per field declared in `fields`.
- Given a declared field, when its column is created, then the column type maps from the field type: `text`→text, `number`→number, `date`→date, `boolean`→bool, `enum`→select (single value).
- Given the `documents` collection exists and a field was added to `config.jsonc`, when the application starts, then the matching column is added without data loss.
- Given a field was removed from `config.jsonc`, when the application starts, then the matching column is removed and the operation is logged; existing values in that column are lost by design (they remain in the event history snapshots).
- Given a field type changed in `config.jsonc`, when the application starts, then the application refuses to start with an explicit migration error listing the field, rather than silently coercing existing values.
- Given the collection is migrated, when existing records exist, then they are left untouched (migration touches schema only).

### US-04 — Create the `document_events` collection, append-only

As the application,
I want the `document_events` collection created once on first start and protected against updates and deletes,
so that the history of a document remains an immutable, append-only source of truth.

Acceptance criteria:

- Given the `document_events` collection does not exist, when the application starts, then it is created with the columns `document` (relation to `documents`, cascade delete with the record), `event_type` (select: `imported`, `corrected`, `validated`), `payload` (JSON), `timestamp` (date).
- Given the collection exists, when the application starts, then its schema is verified and left unchanged.
- Given any API request that tries to update or delete a `document_events` record, when it is served, then it is rejected by the collection's API rules.
- Given a record is appended, when the write succeeds, then it is never modified afterwards by the application itself.

### US-05 — Refuse to serve on invalid configuration

As the operator,
I want the application to fail fast and completely when the configuration is invalid,
so that the container either runs fully configured or does not run at all.

Acceptance criteria:

- Given any configuration error (technical or functional), when the application starts, then no HTTP listener is opened, no cron job is registered, no collection is migrated, and the process exits non-zero.
- Given the container runs under Docker, when the application exits non-zero, then the `--restart unless-stopped` policy loops the start and the last error remains visible in `docker logs`.

## Epic: Polling & Classification

### US-10 — Poll `inbox/` for new objects

As the application,
I want to poll the `inbox/` prefix on the configured cron interval and only consider upload-complete objects,
so that no partially uploaded file is ever processed.

Acceptance criteria:

- Given the cron triggers, when the poller runs, then it lists all objects under `inbox/`.
- Given an object is listed, when no `documents` record references its key yet, then it is a candidate and its size and ETag are recorded; it becomes eligible for download only if its size and ETag are unchanged on the next poll.
- Given an object is listed, when a `documents` record already references its key, then it is skipped (interrupted filings are handled by US-11).
- Given a transient S3 error occurs, when the poller runs, then the error is logged, the pass ends, and the next pass retries; no object is lost.

### US-11 — Recover interrupted filings

As the application,
I want each poll pass to complete any `documents` record whose `file_key` is still under `inbox/`,
so that a filing interrupted between record creation and the physical move resumes automatically.

Acceptance criteria:

- Given a poll pass runs, when a `documents` record exists with `file_key` under `inbox/` and a classification verdict already computed, then the filing procedure (US-17) runs again for that record.
- Given the recovery succeeds, when the object is moved, then the record is updated exactly as a normal filing would (same events, same conflict resolution).
- Given the inbox object of a stuck record no longer exists, when the recovery is attempted, then the record is flagged in the logs for manual inspection and never blocks the poll pass.

### US-12 — Download the object and reject duplicates

As the application,
I want to download each candidate object into memory, compute its sha256 during the download, and reject it if the hash is already known,
so that the same file never enters the pipeline twice.

Acceptance criteria:

- Given an eligible candidate, when it is downloaded, then it is streamed in memory with no disk write and its sha256 is computed on the fly.
- Given the computed sha256 matches an existing `documents` record, when the download completes, then the `inbox/` key is deleted (versioned delete marker), the duplicate is logged in the application logs only (no `document_events` entry, by design), and processing stops for that object.
- Given the sha256 is new, when the download completes, then processing continues to extraction.

### US-13 — Extract the text page by page

As the application,
I want the PDF text extracted page by page including headers and footers,
so that rules can target individual pages and full-text search covers the entire document.

Acceptance criteria:

- Given a downloaded PDF, when extraction runs, then the result is a JSON array with one entry per page, 0-based (`pages[0]` = page 1).
- Given the PDF cannot be parsed (encrypted, corrupted, image-only), when extraction runs, then the document is routed to `pending/` with the failure recorded in the `errors` key of the `imported` event.
- Given extraction succeeds, when pages are stored, then the extraction uses native text only — OCR is out of scope.

### US-14 — Evaluate the rule tree

As the application,
I want the ordered rule tree from `config.jsonc` evaluated against the document pages and the fields set so far,
so that fields are extracted deterministically from the document content.

Acceptance criteria:

- Given a document's pages, when evaluation runs, then rules are evaluated in declaration order: a rule's `when` expression runs first; `set`, `delete`, and child rules run only if `when` returned true.
- Given a rule's `set` entry with a constant `value`, when the rule fires, then the field is set to that constant.
- Given a `set` entry with an `eval` expression, when the rule fires, then the expression is evaluated in the same context (pages, current fields, `firstMatch`, `period`, expr builtins) and the result is assigned.
- Given a field assignment whose value does not match the declared field type (raw string to a `date`, non-numeric to a `number`), when it executes, then it is a rule error and the document is routed to `pending/`.
- Given `firstMatch` finds no match, when its result is used, then it returns an empty string (parsing an empty string with `date()` is a rule error).
- Given `delete` is executed, when the rule fires, then the named fields become undefined again for later rules and template rendering.
- Given the whole evaluation, when it runs, then every rule evaluation and action is written in detail to the application logs (the same trace feeds the future dry-run mode).

### US-15 — Decide the verdict

As the application,
I want the classification verdict computed from the rule outcome,
so that the document is filed automatically or routed to human review deterministically.

Acceptance criteria:

- Given the evaluation finished, when the verdict is computed, then it is valid if and only if: no rule error occurred, every mandatory field has a value, the selected template (or `default` when no rule set `template`) exists in the configuration, and the naming template renders without error and produces no `/` in field values that would break the key.
- Given the verdict is invalid, when reasons are collected, then one `errors` entry is produced per cause (missing mandatory field, rule error, unknown template, rendering error).
- Given the verdict is valid, when the name is computed, then the rendered template is the full object path under `files/` and a conflict suffix is reserved if a file with that name already exists.

### US-16 — Record the document and its `imported` event

As the application,
I want the `documents` record created before any physical move, and the `imported` event appended,
so that an interrupted filing leaves a recoverable state and the import is fully traced.

Acceptance criteria:

- Given a classified document (valid or not), when the record is created, then it stores `sha256`, the current `inbox/` key in `file_key`, `pages`, the extracted fields, and the initial `status` (`processed` when valid, `pending` when not).
- Given the record is created, when the event is appended, then it is an `imported` event whose payload conforms to the section-4 format: `before.file_key` is the inbox key, `after.file_key` is the target key, `after.status` the verdict status, `errors` present on failure, and `after` lists only the recognized fields.
- Given the verdict was pending, when the event is appended, then `after` still lists the fields recognized before the failure so the future correction form can pre-fill them.

### US-17 — File the document

As the application,
I want the object physically moved to its destination only after the record exists,
so that the S3 state and the database state never contradict each other for long.

Acceptance criteria:

- Given a valid verdict, when filing runs, then the object is copied to `files/` + rendered template (conflict suffix applied if needed), the target existence is verified, and only then is the `inbox/` key deleted; the record's `file_key` is updated to the final, conflict-resolved key.
- Given an invalid verdict, when filing runs, then the object is moved to `pending/` with an incremental suffix on name conflict, and the record's `file_key` is updated.
- Given a copy or delete fails mid-filing, when the pass ends, then the recovery of US-11 picks the document up on the next poll.
- Given any deletion, when it executes, then it relies on bucket versioning: the data remains recoverable via delete markers.

## Epic: Data visualization (standard PocketBase UI)

The standard PocketBase collections UI is the read-only window on the data until the custom dashboard exists. Corrections and validations remain reserved to the application flows (custom dashboard, later epic): editing a field or a status directly in the collections UI bypasses re-filing and event tracking, and is not part of the nominal flow.

### US-20 — Browse documents in the collections UI

As the user,
I want to open the `documents` collection in the standard PocketBase admin and see one row per processed file,
so that I can inspect the pipeline state without any custom interface.

Acceptance criteria:

- Given the admin UI, when I open `documents`, then I see the fixed columns (`sha256`, `file_key`, `status`, `pages`) and one column per declared field, with readable names.
- Given the list view, when I filter, then I can filter by `status` and by any declared field using the standard PocketBase filters.
- Given a record, when I open it, then the `pages` JSON array is viewable and the `document_events` relation shows the document's history.

### US-21 — Read a document's event history

As the user,
I want the full `document_events` history of a document readable from the collections UI,
so that I can audit what the application did without querying the database by hand.

Acceptance criteria:

- Given the admin UI, when I open `document_events`, then I can sort by `timestamp` and filter by `document` or `event_type`.
- Given an event, when I open it, then the `payload` JSON is displayed and readable, matching the section-4 format.
- Given the collection, when I try to edit or delete a record from the UI, then the operation is rejected by the API rules (US-04).

## Stories deferred to later epics

- US-3x (Custom review dashboard): file list with status, combined search (FTS5 + fields), detail with PDF preview streamed from S3, correction form with pre-filled fields and template check, validation, rule dry-run.
- US-4x (FTS5): full-text index synchronization via hooks.
