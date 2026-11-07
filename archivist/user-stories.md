# Archivist — User Stories

Backlog for the Archivist application. Each story follows the standard format (As a / I want / So that) with acceptance criteria in Given/When/Then. Stories are numbered; epic tags group them.

## Epic: Startup & Configuration

### US-01 — Start the application from environment variables

As the operator of the container `napnap75/archivist:latest`,
I want the application to read its technical configuration (S3 endpoint and credentials, bucket name, admin account, poll interval, HTTP port) from environment variables,
so that the container starts with no configuration file for infrastructure and secrets stay out of the image.

Acceptance criteria:

- Given the container starts with all mandatory variables set (`ARCHIVIST_S3_ENDPOINT`, `ARCHIVIST_S3_ACCESS_KEY`, `ARCHIVIST_S3_SECRET_KEY`, `ARCHIVIST_S3_BUCKET`, `ARCHIVIST_ADMIN_EMAIL`, `ARCHIVIST_ADMIN_PASSWORD`), when the application starts, then it connects to the S3 server, verifies the bucket is reachable, and starts serving HTTP on the configured port; when bucket versioning is disabled, a warning is emitted through the PocketBase logger (visible in the admin dashboard, Dashboard > Logs) recommending the user to enable it to reduce the risk of losing a file to a bug (the application starts anyway).
- Given an optional variable is not set, when the application starts, then it uses its documented default (`ARCHIVIST_S3_USE_SSL=true`, `ARCHIVIST_S3_REGION=garage`, `ARCHIVIST_POLL_INTERVAL=* * * * *`, `ARCHIVIST_HTTP_ADDR=0.0.0.0:8090`).
- Given a mandatory variable is missing or the S3 server is unreachable, when the application starts, then it exits with a non-zero code and a single explicit error message on stderr (visible in `docker logs`) naming the faulty variable or resource.
- Given the admin account variables are set, when the application starts for the first time, then the PocketBase admin account is created with these credentials; on subsequent starts it is left unchanged unless the password differs, in which case it is updated.

### US-02 — Load and validate the functional configuration

As the operator,
I want the application to load `config.jsonc` at startup and reject it entirely if any part is invalid,
so that no document is ever processed against a partially understood configuration.

Acceptance criteria:

- Given a syntactically valid `config.jsonc` mounted in the container, when the application starts, then the file is parsed with comments allowed and validated against the JSON schema generated from the configuration structs.
- Given the schema is valid, when the application validates further, then every regex compiles, every `expr` expression (`when`, `eval`, template placeholders) compiles against the typed context `Document{Pages, Fields}` with the `firstMatch`, `frDate` and `frPeriod` helpers, and every compiled expression is cached for later use.
- Given any validation error (unknown key, bad type, non-compiling regex or expression, unknown field referenced in `set`/`delete`), when the application starts, then it exits with a non-zero code, an error message pinpointing the file location of the first error, and processes nothing. Cross-references are also checked: every template name used in the rules must exist in `templates`.
- Given the configuration is valid, when a field declaration references a type, then the declared type is one of `text`, `number`, `date`, `boolean`, `enum` (schema-enforced).
- Given the configuration is valid, when the application starts, then the effective configuration (fields, rules, templates) is logged in a condensed form for traceability.

### US-03 — Create or update the `documents` collection from the field declarations

As the application,
I want the `documents` PocketBase collection to be created on first start and migrated on every subsequent start to match the current `config.jsonc`,
so that the persistence schema always reflects the declared extraction fields.

Acceptance criteria:

- Given the `documents` collection does not exist, when the application starts, then it is created with the fixed columns (`sha256` unique-indexed, `file_key`, `pages` as a JSON array, `status`), the standard PocketBase `created`/`updated` autodate columns, plus one column per field declared in `fields`.
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

- Given the `document_events` collection does not exist, when the application starts, then it is created with the columns `document` (relation to `documents`, cascade delete with the record), `event_type` (select: `imported`, `corrected`, `validated`), `payload` (JSON), `timestamp` (date), plus the standard PocketBase `created`/`updated` autodate columns.
- Given the collection exists, when the application starts, then its schema is verified and left unchanged.
- Given any API request that tries to update or delete a `document_events` record, when it is served, then it is rejected by the application's request hooks (`OnRecordUpdateRequest` / `OnRecordDeleteRequest` for the collection, bound to superusers as well). API rules alone cannot enforce this: superusers bypass all API rules, and no rule value blocks everyone (`null` = superusers only, empty string = public).
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
- Given a transient S3 error occurs, when the poller runs, then the error is logged (PocketBase logger, Error level), the pass ends, and the next pass retries; no object is lost.

### US-11 — Recover interrupted filings

As the application,
I want each poll pass to complete any `documents` record whose `file_key` is still under `inbox/`,
so that a filing interrupted between record creation and the physical move resumes automatically.

Acceptance criteria:

- Given a poll pass runs, when a `documents` record exists with `file_key` under `inbox/` and a classification verdict already computed, then the filing procedure (US-17) runs again for that record.
- Given the recovery succeeds, when the object is moved, then the record is updated exactly as a normal filing would (same events, same conflict resolution).
- Given the inbox object of a stuck record no longer exists, when the recovery is attempted, then the record is flagged in the logs (PocketBase logger, Warn level) for manual inspection and never blocks the poll pass.

### US-12 — Download the object and reject duplicates

As the application,
I want to download each candidate object into memory, compute its sha256 during the download, and reject it if the hash is already known,
so that the same file never enters the pipeline twice.

Acceptance criteria:

- Given an eligible candidate, when it is downloaded, then it is streamed in memory with no disk write and its sha256 is computed on the fly.
- Given the computed sha256 matches an existing `documents` record, when the download completes, then the `inbox/` key is deleted (a no-data-loss case — the original with the same content stays filed; with versioning enabled the deleted copy is additionally recoverable via delete markers), the duplicate is logged in the PocketBase logs only (Dashboard > Logs; no `document_events` entry, by design), and processing stops for that object.
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
- Given a `set` entry with an `eval` expression, when the rule fires, then the expression is evaluated in the same context (pages, current fields, `firstMatch`, `frDate`, `frPeriod`, expr builtins) and the result is assigned.
- Given a field assignment whose value does not match the declared field type (raw string to a `date`, non-numeric to a `number`), when it executes, then it is a rule error and the document is routed to `pending/`.
- Given `firstMatch` finds no match, when its result is used, then it returns an empty string (parsing an empty string with `date()` is a rule error).
- Given `delete` is executed, when the rule fires, then the named fields become undefined again for later rules and template rendering.
- Given the whole evaluation, when it runs, then every rule evaluation and action is written in detail to the PocketBase logger at Debug level (visible in Dashboard > Logs; the same trace feeds the future dry-run mode).

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
- Given any deletion, when it executes, then it is final unless bucket versioning is enabled, in which case the data remains recoverable via delete markers.

## Epic: Data visualization (standard PocketBase UI)

The standard PocketBase collections UI is the nominal interface for reading and correcting documents. Field edits and status changes made through it (or through any PocketBase API client) are intercepted by the application's `OnRecordUpdateRequest` hook on `documents`, which records the change in the event history (US-22) and enforces the status rules (US-23) — but deliberately performs no re-filing: the UI edits one field at a time and shows no preview of the resulting file name, so an inconsistent draft cannot trigger irreversible side effects. The full correction — all fields at once, dry-run of the resulting name, re-filing and validation — is the review dashboard's job (US-34 to US-36), and record deletions propagate to S3 (US-24).
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
- Given the collection, when I try to edit or delete a record from the UI, then the operation is rejected by the application's request hooks (US-04) — including for the superuser account the admin UI uses.

### US-22 — Edit a document's fields from the standard PocketBase UI without side effects

As the user,
I want field edits saved from the standard PocketBase UI (or any PocketBase client) to persist the new values without any side effect — no template evaluation, no re-filing, no status change,
so that the UI's editing model — one field at a time, no preview of the resulting file name — cannot trigger irreversible actions I cannot review before saving.

Acceptance criteria:

- Given a document, when I edit one or more declared fields in the collections UI and save, then the `OnRecordUpdateRequest` hook on `documents` persists the new values as they are: no template evaluation, no re-filing, no `file_key` change, no status change.
- Given a field edit is saved, when the record is persisted, then a `corrected` event is appended whose `before`/`after` diff (section-4 format) carries the changed fields only; `file_key` and `status` do not appear since they did not change.
- Given a save where no declared field actually changed, when the hook runs, then no event is appended.
- Given the save also changes the status, when the hook runs, then the status rules of US-23 apply on their own: `processed` → `validated` is appended as a separate `validated` event, anything else is refused.
- Given a `pending` document, when fields are edited this way, then it stays `pending` and its object stays in `pending/`; leaving the review queue requires the full correction of the review dashboard (US-36), whose form shows all fields and the resulting name before saving.
### US-23 — Validate a document from the standard PocketBase UI

As the user,
I want to change a document's status to `validated` directly in the collections UI,
so that the validation is traced as a `validated` event without a custom interface.

Acceptance criteria:

- Given a `processed` document, when I set its status to `validated` in the collections UI and save, then the update hook appends a `validated` event with the section-4 payload (snapshots carrying the status change alone) and persists the new status.
- Given any update that tries a status change other than the allowed transition (`processed`→`validated`), when the hook runs, then the save is refused with an explicit error.
- Given a status change to `validated`, when no field changed, then no re-filing occurs and the `file_key` is untouched.

### US-24 — Delete a document and its S3 object together

As the user,
I want deleting a `documents` record from the standard PocketBase UI (or any PocketBase client) to also delete the underlying S3 object,
so that the database and the bucket never contradict each other.

Acceptance criteria:

- Given a `documents` record, when it is deleted, then the `OnRecordDeleteRequest` hook on `documents` deletes the S3 object at `file_key` before the record is removed; the cascade delete of the `document_events` relation (US-04) removes the history with it.
- Given the S3 deletion fails (transient error, unreachable cluster), when the hook runs, then the record deletion is refused with an explicit error and both the record and the object are left in place.
- Given the object at `file_key` is already gone from the bucket, when the hook runs, then the deletion is idempotent: the record is removed.
- Given the bucket is versioned, when the object is deleted, then a delete marker keeps it recoverable; without versioning the deletion is final (same caveat as US-01).
- Given a superuser issues the DELETE, when the hook runs, then the hook applies: like the append-only protection of US-04, it cannot rely on API rules alone.
## Epic: Review Dashboard (US-30 to US-36)

The static frontend served from `pb_public/` (arch.md section 7): a document list and a document detail page, entirely driven by the configuration metadata route. No build toolchain, vendored dependencies only (100% offline). Every persistence goes through the standard PocketBase API and its hooks; the only application routes are those the standard API cannot express (config metadata, PDF proxy, dry-run, full correction).

### US-30 — Serve the dashboard as static files

As the user,
I want the review dashboard served by the application itself at the root URL,
so that no additional server or build step is needed.

Acceptance criteria:

- Given the image is built and running, when I open `http://<server>:8090/`, then the dashboard pages are served from `pb_public/` — plain HTML/CSS/JS, no build toolchain, dependencies vendored locally, nothing loaded from a CDN.
- Given a page is opened, when it renders, then it carries no document data by itself; everything is loaded through authenticated API calls.
- Given an unauthenticated visitor, when a page requests data, then the API refuses it and the page redirects to the admin UI (`/_/`).

### US-31 — Expose the configuration metadata

As the dashboard,
I want the declared fields, their types and the enum vocabularies exposed by a read-only route,
so that the UI is entirely driven by the configuration.

Acceptance criteria:

- Given an authenticated request to `GET /api/archivist/config`, when it is served, then it returns the declared fields (name, type, mandatory), the enum vocabularies computed from the constants the rules assign, and the template names — and nothing sensitive (no S3 credentials).
- Given an unauthenticated request, when it is served, then it is refused.
- Given the configuration is reloaded only on restart, when the route is called, then it serves the in-memory compiled configuration (the same the pipeline uses).

### US-32 — Browse documents in a configurable table

As the user,
I want the documents in a table whose columns I choose, sortable and filterable per column,
so that I can work through the review queue efficiently.

Acceptance criteria:

- Given the list page, when it opens, then it shows a paginated table whose default columns are all mandatory fields plus `status` and `file_key`, and a column chooser exposing every declared field plus `created`/`updated`; the choice persists in the browser.
- Given a column header, when I click it, then the list is sorted by that column server-side and the state persists.
- Given a column, when I open its filter, then the control matches the field type (contains for `text`, dropdown for `enum` and `status`, range for `date`, tri-state for `boolean`) and the filter is applied server-side through a PocketBase filter expression.
- Given the list, when I open it, then quick tabs show the per-status counts and pre-filter the review queue (`pending` first).

### US-33 — Change one field for a selection of documents

As the user,
I want to select several documents and change one declared field for all of them,
so that a systematic correction does not require opening every document.

Acceptance criteria:

- Given the list, when I select rows (page or whole filtered set), then a bulk action proposes choosing a declared field and a new value in a control matching the field type, and shows the number of impacted documents.
- Given the bulk action is confirmed, when it runs, then it issues one standard PATCH per document — the US-22 hook applies per record: a `corrected` event each, no re-filing, no status change.
- Given one document of the selection fails, when the bulk action finishes, then the others are applied and the failure is reported with its error.

### US-34 — Preview a document and correct each field

As the user,
I want a page showing the PDF preview and a form for every field,
so that I can correct a document with its content in view.

Acceptance criteria:

- Given the detail page of a document, when it opens, then the PDF preview is embedded via `GET /api/archivist/documents/{id}/pdf` and the declared fields appear in typed controls pre-filled with the current values; mandatory fields are marked.
- Given a `pending` document, when the page opens, then the classification errors of its `imported` event are displayed at the top of the form.
- Given the page, when I look below the form, then the document's event history is listed read-only, sorted by timestamp, payloads rendered as before/after diffs.
- Given the form, when I save with the draft button, then the values persist through a standard PATCH (US-22: no re-filing, no status change).
- Given a `processed` document with no field change, when I use the validate button, then the US-23 transition applies.

### US-35 — Propose the pipeline values and the resulting name live

As the user,
I want, for each field, the value the pipeline would extract today when it differs from the current value, and the resulting file name recomputed as I type,
so that I see the effect of a correction before committing it.

Acceptance criteria:

- Given the detail page, when it opens, then it calls `POST /api/archivist/documents/{id}/dry-run` with no body; the route re-runs the rule tree against the stored pages and returns the proposed fields, the selected template, the rendered name and the errors, persisting nothing.
- Given a field whose proposed value differs from the current value, when the response arrives, then a discreet banner under the control offers the proposed value with one-click adoption.
- Given the form is modified, when the typing settles, then the page calls the dry-run route with the proposed field values; the route converts them to their native types (per-field error on mismatch), merges them with the record values, renders the template against the stored pages and returns the target name and its errors in the same response shape.
- Given the two modes, when either runs, then they are exclusive: user values never enter the rule-evaluation context, and no record, event or S3 object is touched.
- Given the dry-run returns errors, when the form still allows a draft save, then the full correction refuses to run while errors remain.

### US-36 — Correct fully and validate

As the user,
I want a single action that re-files the document under its corrected name and validates it,
so that a reviewed document leaves the queue in one confirmed step.

Acceptance criteria:

- Given the form, when I trigger the full correction, then `POST /api/archivist/documents/{id}/correct` runs the transactional orchestration: conflict-suffix resolution, S3 copy, target verification, deletion of the old key, record update (`file_key`, status `validated`), and a `corrected` event carrying the full diff.
- Given any step fails midway, when the route handles it, then the save is refused, the S3 state is restored, and the record and history are left unchanged.
- Given the rendered name equals the current `file_key`, when the correction runs, then the copy and delete are skipped, the status alone changes, and `file_key` is absent from the diff.
- Given a `pending` document, when the correction succeeds, then the `pending/` object is removed and the document leaves the review queue.
- Given an unauthenticated client, when it calls the route, then it is refused.
- Given the implementation, when it is reviewed, then it reuses the engine internals (`FieldsFromRecord`, `ValidateValue`, `Render`, `ResolveConflict`, `DiffSnapshots`, `AppendEvent`) with no parallel business logic.

## Stories deferred to later epics

- US-5x (Config editor): pages to declare fields and build rules with user assistance — guided rule construction, test bench against existing documents through the dry-run routes.
- US-4x (FTS5): full-text index synchronization via hooks.
