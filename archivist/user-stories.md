# Archivist — User Stories

Backlog for the Archivist application. Each story follows the standard format (As a / I want / So that) with acceptance criteria in Given/When/Then. Stories are numbered; epic tags group them.

## Epic: Startup & Configuration

### US-01 — Start the application from environment variables

As the operator of the container `napnap75/archivist:latest`,
I want the application to read its technical configuration (S3 endpoint and credentials, bucket name, admin account, poll interval, HTTP port) from environment variables,
so that the container starts with no configuration file for infrastructure and secrets stay out of the image.

Acceptance criteria:

- Given the container starts with all mandatory variables set (`ARCHIVIST_S3_ENDPOINT`, `ARCHIVIST_S3_ACCESS_KEY`, `ARCHIVIST_S3_SECRET_KEY`, `ARCHIVIST_S3_BUCKET`, `ARCHIVIST_ADMIN_EMAIL`, `ARCHIVIST_ADMIN_PASSWORD`), when the application starts, then it connects to the S3 server, verifies the bucket is reachable, and starts serving HTTP on the configured port; when bucket versioning is disabled, a warning is emitted through the PocketBase logger (visible in the admin dashboard, Dashboard > Logs) recommending the user to enable it to reduce the risk of losing a file to a bug (the application starts anyway).
- Given an optional variable is not set, when the application starts, then it uses its documented default (`ARCHIVIST_S3_USE_SSL=true`, `ARCHIVIST_S3_REGION=garage`, `ARCHIVIST_S3_INBOX_PREFIX=inbox`, `ARCHIVIST_S3_PENDING_PREFIX=pending`, `ARCHIVIST_S3_FILES_PREFIX=files`, `ARCHIVIST_POLL_INTERVAL=* * * * *`, `ARCHIVIST_HTTP_ADDR=0.0.0.0:8090`).
- Given the prefix variables, when the application starts, then the three bucket prefixes (inbox, pending, files, surroundings slashes normalized) follow them: `ARCHIVIST_S3_FILES_PREFIX=/` files documents at the bucket root, the inbox and pending prefixes refuse to be empty, and the three prefixes must be distinct (the application exits with an explicit error otherwise).
- Given a mandatory variable is missing or the S3 server is unreachable, when the application starts, then it exits with a non-zero code and a single explicit error message on stderr (visible in `docker logs`) naming the faulty variable or resource.
- Given the admin account variables are set, when the application starts for the first time, then the PocketBase admin account is created with these credentials; on subsequent starts it is left unchanged unless the password differs, in which case it is updated.

### US-02 — Load and validate the functional configuration

As the operator,
I want the application to load `config.jsonc` at startup and reject it entirely if any part is invalid,
so that no document is ever processed against a partially understood configuration.

Acceptance criteria:

- Given a syntactically valid `config.jsonc` mounted in the container, when the application starts, then the file is parsed with comments allowed and validated against the configuration format, including a non-empty `french` title on every field.
- Given the schema is valid, when the application validates further, then every regex compiles, every `expr` expression (`when`, `eval`, template placeholders) compiles against the typed context `Document{Pages, Fields}` with the `firstMatch`, `frDate` and `frPeriod` helpers, and every compiled expression is cached for later use.
- Given any validation error (unknown key, bad type, missing or empty `french` field title, non-compiling regex or expression, unknown field referenced in `set`/`delete`), when the application starts, then it exits with a non-zero code, an error message pinpointing the file location of the first error, and processes nothing. Cross-references are also checked: every template name used in the rules must exist in `templates`.
- Given the configuration is valid, when a field declaration references a type, then the declared type is one of `text`, `number`, `date`, `boolean`, `enum` (schema-enforced).
- Given the configuration is valid, when the application starts, then the effective configuration (fields, rules, templates) is logged in a condensed form for traceability.

### US-03 — Create or update the `documents` collection from the field declarations

As the application,
I want the `documents` PocketBase collection to be created on first start and migrated on every subsequent start to match the current `config.jsonc`,
so that the persistence schema always reflects the declared extraction fields.

Acceptance criteria:

- Given the `documents` collection does not exist, when the application starts, then it is created with the fixed columns (`sha256` unique-indexed, `file_key`, `pages` as a JSON array, `status`), the standard PocketBase `created`/`updated` autodate columns, plus one column per field declared in `fields`.
- Given a declared field, when its column is created, then the column type maps from the field type: `text`→text, `number`→number, `date`→date, `boolean`→bool, `enum`→select (single value, its value list managed by the application — US-37).
- Given the `documents` collection exists and a field was added to `config.jsonc`, when the application starts, then the matching column is added without data loss.
- Given a field was removed from `config.jsonc`, when the application starts, then the matching column is removed and the operation is logged; existing values in that column are lost by design (they remain in the event history snapshots).
- Given a field type changed in `config.jsonc`, when the application starts, then the application refuses to start with an explicit migration error listing the field, rather than silently coercing existing values.
- Given the collection is migrated, when existing records exist, then they are left untouched (migration touches schema only).

### US-04 — Create the `document_events` collection, append-only

As the application,
I want the `document_events` collection created once on first start and protected against updates and deletes,
so that the history of a document remains an immutable, append-only source of truth.

Acceptance criteria:

- Given the `document_events` collection does not exist, when the application starts, then it is created with the columns `document` (relation to `documents`, cascade delete with the record), `event_type` (select: `imported`, `corrected`, `validated`, `relocated`), `payload` (JSON), `timestamp` (date), plus the standard PocketBase `created`/`updated` autodate columns.
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

### US-10 — Poll the inbox prefix for new objects

As the application,
I want to poll the configured inbox prefix (default `inbox/`) on the configured cron interval and only consider upload-complete objects,
so that no partially uploaded file is ever processed.

Acceptance criteria:

- Given the cron triggers, when the poller runs, then it lists all objects under the inbox prefix.
- Given an object is listed, when no `documents` record references its key yet, then it is a candidate and its size and ETag are recorded; it becomes eligible for download only if its size and ETag are unchanged on the next poll.
- Given an object is listed, when a `documents` record already references its key, then it is skipped (interrupted filings are handled by US-11).
- Given a transient S3 error occurs, when the poller runs, then the error is logged (PocketBase logger, Error level), the pass ends, and the next pass retries; no object is lost.

### US-11 — Recover interrupted filings

As the application,
I want each poll pass to complete any `documents` record whose `file_key` is still under the inbox prefix,
so that a filing interrupted between record creation and the physical move resumes automatically.

Acceptance criteria:

- Given a poll pass runs, when a `documents` record exists with `file_key` under the inbox prefix and a classification verdict already computed, then the filing procedure (US-17) runs again for that record.
- Given the recovery succeeds, when the object is moved, then the record is updated exactly as a normal filing would (same events, same conflict resolution).
- Given the inbox object of a stuck record no longer exists, when the recovery is attempted, then the record is flagged in the logs (PocketBase logger, Warn level) for manual inspection and never blocks the poll pass.

### US-12 — Download the object and reject duplicates

As the application,
I want to download each candidate object into memory, compute its sha256 during the download, and reject it if the hash is already known,
so that the same file never enters the pipeline twice.

Acceptance criteria:

- Given an eligible candidate, when it is downloaded, then it is streamed in memory with no disk write and its sha256 is computed on the fly.
- Given the computed sha256 matches an existing `documents` record, when the download completes, then the inbox key is deleted (a no-data-loss case — the original with the same content stays filed; with versioning enabled the deleted copy is additionally recoverable via delete markers), the duplicate is logged in the PocketBase logs only (Dashboard > Logs; no `document_events` entry, by design), and processing stops for that object.
- Given the sha256 is new, when the download completes, then processing continues to extraction.
- Given an object under the inbox prefix whose content does not start with the `%PDF-` header, when it is processed, then it is left as-is in the inbox prefix: it is never imported, gets no record and no event, and the skip is logged (PocketBase logger, Warn level, once per unchanged content).
- Given an object already skipped as non-PDF, when a later pass lists it with an unchanged ETag, then it is not re-downloaded; when its content changes (new ETag), then it is checked again.

### US-13 — Extract the text page by page

As the application,
I want the PDF text extracted page by page including headers and footers,
so that rules can target individual pages and full-text search covers the entire document.

Acceptance criteria:

- Given a downloaded PDF, when extraction runs, then the result is a JSON array with one entry per page, 0-based (`pages[0]` = page 1).
- Given the PDF cannot be parsed (encrypted, corrupted, image-only), when extraction runs, then the document is routed to the pending prefix with the failure recorded in the `errors` key of the `imported` event.
- Given extraction succeeds, when pages are stored, then the extraction uses native text only — OCR is out of scope.

### US-14 — Evaluate the rule tree

As the application,
I want the ordered rule tree from `config.jsonc` evaluated against the document pages and the fields set so far,
so that fields are extracted deterministically from the document content.

Acceptance criteria:

- Given a document's pages, when evaluation runs, then rules are evaluated in declaration order: a rule's `when` expression runs first; `set`, `delete`, and child rules run only if `when` returned true.
- Given a rule's `set` entry with a constant `value`, when the rule fires, then the field is set to that constant.
- Given a `set` entry with an `eval` expression, when the rule fires, then the expression is evaluated in the same context (pages, current fields, `firstMatch`, `frDate`, `frPeriod`, expr builtins) and the result is assigned.
- Given a field assignment whose value does not match the declared field type (raw string to a `date`, non-numeric to a `number`), when it executes, then it is a rule error and the document is routed to the pending prefix.
- Given `firstMatch` finds no match, when its result is used, then it returns an empty string (parsing an empty string with `date()` is a rule error).
- Given `delete` is executed, when the rule fires, then the named fields become undefined again for later rules and template rendering.
- Given the whole evaluation, when it runs, then every rule evaluation and action is written in detail to the PocketBase logger at Debug level (visible in Dashboard > Logs; the same structured trace — modifying and failing rules — is returned by the classification dry-run, US-42).

### US-15 — Decide the verdict

As the application,
I want the classification verdict computed from the rule outcome,
so that the document is filed automatically or routed to human review deterministically.

Acceptance criteria:

- Given the evaluation finished, when the verdict is computed, then it is valid if and only if: no rule error occurred, every mandatory field has a value, the selected template (or `default` when no rule set `template`) exists in the configuration, and the naming template renders without error and produces no `/` in field values that would break the key.
- Given the verdict is invalid, when reasons are collected, then one `errors` entry is produced per cause (missing mandatory field, rule error, unknown template, rendering error).
- Given the verdict is valid, when the name is computed, then the rendered template is the full object path under the files prefix and a conflict suffix is reserved if a file with that name already exists.

### US-16 — Record the document and its `imported` event

As the application,
I want the `documents` record created before any physical move, and the `imported` event appended,
so that an interrupted filing leaves a recoverable state and the import is fully traced.

Acceptance criteria:

- Given a classified document (valid or not), when the record is created, then it stores `sha256`, the current inbox key in `file_key`, `pages`, the extracted fields, and the initial `status` (`processed` when valid, `pending` when not).
- Given the record is created, when the event is appended, then it is an `imported` event whose payload conforms to the section-4 format: `before.file_key` is the inbox key, `after.file_key` is the target key, `after.status` the verdict status, `errors` present on failure, and `after` lists only the recognized fields.
- Given the verdict was pending, when the event is appended, then `after` still lists the fields recognized before the failure so the future correction form can pre-fill them.

### US-17 — File the document

As the application,
I want the object physically moved to its destination only after the record exists,
so that the S3 state and the database state never contradict each other for long.

Acceptance criteria:

- Given a valid verdict, when filing runs, then the object is copied to the files prefix + rendered template (conflict suffix applied if needed), the target existence is verified, and only then is the inbox key deleted; the record's `file_key` is updated to the final, conflict-resolved key.
- Given an invalid verdict, when filing runs, then the object is moved to the pending prefix with an incremental suffix on name conflict, and the record's `file_key` is updated.
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
- Given the save also changes the status, when the hook runs, then the status rules of US-23 apply on their own: `processed` → `validated` appends a separate `validated` event, `validated` → `processed` a separate `reprocessed` event, anything else is refused.
- Given a `pending` document, when fields are edited this way, then it stays `pending` and its object stays in the pending prefix; leaving the review queue requires the full correction of the review dashboard (US-36), whose form shows all fields and the resulting name before saving.

### US-23 — Validate or reopen a document from the standard PocketBase UI

As the user,
I want to change a document's status to `validated` — or back to `processed` when I reopen it — directly in the collections UI,
so that the transition is traced as a `validated`/`reprocessed` event without a custom interface.

Acceptance criteria:

- Given a `processed` document, when I set its status to `validated` in the collections UI and save, then the update hook appends a `validated` event with the section-4 payload (snapshots carrying the status change alone) and persists the new status.
- Given a `validated` document, when I set its status back to `processed` and save, then the update hook appends a `reprocessed` event with the same payload shape and persists the new status; no re-filing occurs and the `file_key` is untouched.
- Given any update that tries a status change other than the allowed transitions (`processed`→`validated`, `validated`→`processed` — anything touching `pending` included), when the hook runs, then the save is refused with an explicit error.
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

## Epic: Review Dashboard (US-30 to US-42, US-45, US-46)

The static frontend served from `pb_public/` (arch.md section 7): document list, document detail, tools and configuration-editor pages, driven by the configuration metadata route. No build toolchain, vendored dependencies only (100% offline). Document persistence goes through the standard PocketBase API and its hooks; application routes handle operations the standard API cannot express (config metadata, PDF proxy, dry-run, full correction, enum value registration). The configuration editor is specified separately in US-50 to US-52.

### US-30 — Serve the dashboard as static files

As the user,
I want the review dashboard served by the application itself at the root URL,
so that no additional server or build step is needed.

Acceptance criteria:

- Given the image is built and running, when I open `http://<server>:8090/`, then the dashboard pages are served from `pb_public/` — plain HTML/CSS/JS, no build toolchain, dependencies vendored locally, nothing loaded from a CDN.
- Given a page is opened, when it renders, then it carries no document data by itself; everything is loaded through authenticated API calls.
- Given an unauthenticated visitor, when a page requests data, then the page prompts for superuser credentials, authenticates through PocketBase, and retries the request; the API refuses access until authentication succeeds.

### US-31 — Expose the configuration metadata

As the dashboard,
I want the declared fields, their types and the enum vocabularies exposed by a read-only route,
so that the UI is entirely driven by the configuration.

Acceptance criteria:

- Given an authenticated request to `GET /api/archivist/config`, when it is served, then it returns the declared fields (name, French title, type, mandatory), the enum vocabularies read from the `documents` collection schema (dummy placeholder excluded), and the template names — and nothing sensitive (no S3 credentials).
- Given an unauthenticated request, when it is served, then it is refused.
- Given the configuration is loaded at startup or successfully saved through the configuration editor, when the route is called, then it serves metadata from the currently active compiled configuration used by the pipeline.

### US-32 — Browse documents in a configurable table

As the user,
I want the documents in a table whose columns I choose, sortable and filterable per column,
so that I can work through the review queue efficiently.

Acceptance criteria:

- Given the list page, when it opens, then it shows a paginated table whose default columns are all mandatory fields plus `status` and `file_key`, and a column chooser exposing every declared field plus `created`/`updated`; the choice persists in the browser.
- Given a column header, when I click it, then the list is sorted by that column server-side and the state persists.
- Given a column, when I open its filter, then the control matches the field type (contains for `text`, dropdown for `enum` and `status`, range for `date`, tri-state for `boolean`) and the filter is applied server-side through a PocketBase filter expression.
- Given an `enum` filter dropdown, when the `empty` entry is chosen, then the list shows only the documents whose field holds no value (`(field='')` server-side); the `—` entry still clears the filter.
- Given the list page, when I type a search term, then after a short pause the list shows documents where any page contains that term, case-insensitively, without displaying a text excerpt.
- Given the list, when I open it, then quick tabs show the per-status counts and pre-filter the review queue (`pending` first).
- Given the header checkbox, when I use it, then it cycles none → page → the whole filtered set → none (indeterminate display while the selection is partial or page-limited), and changing any filter (status tab, column filter, page search) empties the selection; no dedicated select-all or clear button is needed.

### US-33 — Change one field for a selection of documents

As the user,
I want to select several documents and change one declared field — or the status — for all of them,
so that a systematic correction does not require opening every document.

Acceptance criteria:

- Given the list, when I select rows (page, or the whole filtered set through the header checkbox), then a bulk action proposes choosing a declared field and a new value in a control matching the field type, and shows the number of impacted documents.
- Given the bulk action is confirmed, when it runs, then it issues one standard PATCH per document — the US-22 hook applies per record: a `corrected` event each, no re-filing, no status change.
- Given one document of the selection fails, when the bulk action finishes, then the others are applied and the failure is reported with its error.
- Given the bulk action, when the recompute mode is chosen instead of a manual value, then for each selected document the classification dry-run of US-35 (no body) re-runs the rule tree against the stored pages and the field takes the proposed value — only when that proposal differs from the document's current value; a document with no proposal for the field is left untouched, and a document whose proposal equals its current value counts as unchanged (no PATCH, no event).
- Given the recompute mode updates a document, when it saves, then it goes through the same standard PATCH as a manual value (US-22 hook: `corrected` event alone), and an enum value missing from the field's list is registered first (US-37), like the import and correction paths.
- Given the field dropdown, when it opens, then it also proposes the `status` field — not a declared field — with only the `validated` and `processed` values in its control: applying it issues the same one standard PATCH per document and the US-23 hook traces each change (`validated` or `reprocessed` event, no re-filing); the recalculate action does not apply to it.

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

- Given the detail page, when it opens, then it calls `POST /api/archivist/documents/{id}/dry-run` with no body; the route re-runs the rule tree against the stored pages and returns the proposed fields, the selected template, the rendered target key (full S3 key, files prefix included) and the errors, persisting nothing.
- Given the classification dry-run, when it runs, then the response also carries a `trace` field: the flat, ordered list of the rules that modified a value (`set`/`delete`) or errored — the rule's `when` expression as rule identifier (rules carry no name), field, final value after the operation (marked as unset for a `delete`), the literal `eval` expression that produced it (a constant `set` returns its own value) and the error message for a failing rule; rules that match without modifying anything and non-matching rules are omitted (US-42).
- Given a field whose proposed value differs from the current value, when the response arrives, then a discreet banner under the control offers the proposed value with one-click adoption.
- Given the form is modified, when the typing settles, then the page calls the dry-run route with the proposed field values; the route converts them to their native types (per-field error on mismatch), merges them with the record values, renders the template against the stored pages and returns the target key (full S3 key, files prefix included) and its errors in the same response shape.
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
- Given a `pending` document, when the correction succeeds, then the object in the pending prefix is removed and the document leaves the review queue.
- Given an unauthenticated client, when it calls the route, then it is refused.
- Given the implementation, when it is reviewed, then it reuses the engine internals (`FieldsFromRecord`, `ValidateValue`, `Render`, `ResolveConflict`, `DiffSnapshots`, `AppendEvent`) with no parallel business logic.

### US-37 — Manage the enum value lists

As the user,
I want the possible values of an enum field stored in the `documents` schema and maintained by the application itself,
so that the dropdowns never show an empty or stale list and the values stay consistent between the pipeline and the dashboard.

Acceptance criteria:

- Given an enum field, when its column is created, then the select holds the single dummy value `-----`; for the `template` field, the list is initialized to the template names of the configuration and always follows it.
- Given a document is processed (import or full correction), when a rule assigns a value missing from the field's list, then the value is added to the list before the record is saved.
- Given the dashboard, when I pick the add-a-value entry in an enum dropdown, then a modal asks for the value, the `POST /api/archivist/fields/{field}/values` route registers it in the list, and the dropdown offers only the possible values afterwards.
- Given the route, when it is called with an unknown or non-enum field, an empty value, the reserved dummy `-----`, or the `template` field, then it is refused; the list of `template` follows the configuration alone.
- Given a startup, when the cleanup pass runs, then every enum value no document uses anymore is removed from the list; the dummy `-----` is kept only when the list would otherwise be empty; removals are logged.

### US-42 — Show how the rules evaluated a document

As the user,
I want to see, on the detail page, how the rules evaluate the document today — the processing errors and every rule that changed a value —
so that I understand where the proposed values come from and why a document is pending.

Acceptance criteria:

- Given the detail page, when it opens, then a panel below the event history, collapsed by default, holds the rule evaluation trace fetched with the classification dry-run in the same request (no extra call when unfolding).
- Given the panel header, when the dry-run returned errors (classification or name rendering), then they are displayed at the top of the panel in the existing error style, and the collapsed header shows a badge with their count.
- Given the trace, when the panel is unfolded, then each entry shows the rule's `when` expression (rules carry no name), the field, the producing `eval` expression and the final value; deletions are marked as unset, failing rules show their error message.
- Given the evaluation, when it runs, then nothing is persisted: the trace reflects what the rules would do today against the stored pages, not what happened at import.

### US-45 — Delete a selection of documents from the list

As the user,
I want to delete the documents I selected in the list — the same deletion as US-24, applied to the whole selection at once,
so that I can remove obsolete or wrongly imported documents without opening them one by one.

Acceptance criteria:

- Given the list, when a selection exists, then a delete icon, separated from the re-file action by a vertical bar, triggers the deletion of the selected documents after a confirmation.
- Given the confirmation, when it opens, then it displays the number of documents about to be deleted and warns that the action is irreversible; without a confirmation nothing is deleted.
- Given a selected document, when the action reaches it, then it runs through the standard `DELETE /api/collections/documents/records/{id}`: the US-24 hook applies — the S3 object at `file_key` is deleted before the record, the cascade delete of `document_events` removes the history with the record, and a refused deletion (S3 failure) leaves both the record and the object in place without stopping the rest.
- Given the action runs, when it finishes, then the summary shows the deleted and failed counts, one failure does not stop the rest, and the selection is cleared.
- Given no selection, when the delete action is used, then nothing is deleted and the user is told no document is selected.
- Given an unauthenticated client, when it calls the delete route, then it is refused (same protection as US-24: the hook binds to superusers as well).

### US-46 — Re-run the rules on a selection of documents

As the user,
I want a button next to the selection re-file that re-runs the whole rule engine on the documents I selected in the list — the same classification as the import, from their stored pages, followed by the re-filing of US-44,
so that a change of the rules applies to a handful of already classified documents without re-importing them by hand.

Acceptance criteria:

- Given the list, when a selection exists, then a re-run button, placed next to the re-file action, triggers the full re-classification of the selected documents after a confirmation.
- Given a selected document, when the action reaches it, then it runs through `POST /api/archivist/documents/rerun/{id}`: the stored pages are re-classified by the active configuration (the same engine as the import), the declared fields are replaced by the proposal (a field the rules no longer produce is unset), the status goes back to `processed` like at import, and the object is re-filed under the rendered template like US-43 (conflict suffix on collision, copy first, existence verified, source removed), the record save and the `corrected` event carrying the full diff (fields, `file_key`, status) committing in one transaction, with a best-effort S3 rollback when the save fails.
- Given a document whose new classification has errors (mandatory field missing, rule error, unknown template, render failure), when the action reaches it, then the document is left untouched — no field change, no move, no event — and reported as skipped with the reason; a skip is not an error.
- Given a document whose re-classification changes nothing, when the action reaches it, then nothing moves or is written (`unchanged`), and the summary counts it.
- Given a `pending` document in the selection, when the action reaches it, then the route refuses it: review-queue documents are never moved (US-43).
- Given the action runs, when it finishes, then the summary shows the updated, unchanged, skipped and failed counts, one failure does not stop the rest, and the selection is cleared.
- Given an unauthenticated client, when it calls the route, then it is refused.

## Epic: Tools (dashboard)

The tools page (arch.md section 7.5): maintenance tasks started by hand from a dedicated page — one card per tool with its description, a run button, live progress while the job runs, and a final summary. One job at a time, cancellable. The first tool reconciles the bucket with the database when files have been moved by hand (e.g. through Nextcloud); the second re-files every document after a change of the files prefix or of a naming template.

### US-40 — A tools page with run, progress and summary

As the user,
I want a page listing the maintenance tools, each with a description, a run button, a live progress display and a final summary,
so that one-off maintenance tasks are visible and controllable instead of hidden scripts.

Acceptance criteria:

- Given the dashboard, when I open the tools page, then each tool appears as a card with its name, its description, a run button and a simulation button.
- Given the simulation button is used, when the job runs, then every check of the tool is performed and the summary reports what would happen, but nothing is written: no S3 move, no record update, no event.
- Given a tool is started, when the job runs, then the page shows a progress bar (processed / total objects, the key currently being examined), refreshed by polling, and a cancel button that stops the job at the next object boundary.
- Given the job ends, when the page refreshes, then it shows a summary with the per-outcome counts plus the detail lists (missing keys, per-object errors), and the panel is restored after a page reload.
- Given a job is already running, when a tool is started, then the request is refused with an explicit message.
- Given an unauthenticated visitor, when any tools route is called, then it is refused.

### US-41 — Find the files lost in the bucket

As the user,
I want a tool that hashes every object of the bucket and reconciles it with the database,
so that files moved by hand are re-attached to their record and unknown files re-enter the pipeline.

Acceptance criteria:

- Given the tool runs, when it scans, then it lists only objects whose filename ends in `.pdf` (case-insensitive) under the pending and files prefixes — the whole bucket when `ARCHIVIST_S3_FILES_PREFIX=/` —, stream-computes each object's sha256 and looks up a `documents` record by hash; objects under the inbox prefix, or with another extension, are ignored.
- Given an object whose record's `file_key` equals its key, when it is scanned, then nothing happens (`ok`).
- Given an object whose record's `file_key` no longer exists in the bucket, when it is scanned, then the record's `file_key` is corrected to the found key (the object itself never moves), a `relocated` event is appended with the `file_key` diff, and the correction is refused when another record already references the key (`corrected`).
- Given an object whose record's `file_key` still exists elsewhere, when it is scanned, then the stray copy is left in place and reported (`duplicates`).
- Given an object no record knows, when it is scanned, then it is moved to the inbox prefix (conflict suffix on name collision) so the poller processes it automatically (`moved`).
- Given a record whose `file_key` exists nowhere in the bucket and is not an interrupted filing under the inbox prefix, when the scan ends, then it is reported in the summary (`missing`) with its key, never acted upon; a record whose object was found under another key is a `corrected`, not a `missing` — the two categories never overlap.
- Given the scan ends, when the summary is displayed, then every impacted object is listed in detail category by category, not only the missing files: the corrections (`old key → found key`), the unknown objects with their inbox destination, and the stray copies with their original, in real runs as in simulation mode.
- Given a per-object failure (S3 error, lookup error), when it occurs, then it is logged, counted in the summary and listed, and the scan continues with the next object.
- Given the tool runs in simulation mode, when it scans, then every outcome is computed, counted and listed exactly as in a real run (ok, corrected, moved, duplicates, missing), but no object moves, no `file_key` is corrected and no `relocated` event is appended; the planned moves resolve their name collisions against the current bucket contents and against the moves already planned by the same run.

### US-43 — Re-file every document after a naming change

As the user,
I want a tool that re-computes every filed document's target name from the active configuration and moves the objects that are no longer where their name says,
so that a change of `ARCHIVIST_S3_FILES_PREFIX` or of a naming template is applied to the documents already in the bucket.

Acceptance criteria:

- Given the tool runs, when it starts, then it selects only the documents with status `processed` or `validated`; documents still in the review queue (status `pending`) are never touched, neither in the bucket nor in the database.
- Given a selected document, when its target is computed, then the stored `template` of the record is rendered (the `default` template when the record carries no template) from the fields stored in the record under the active compiled configuration, and the full target key is the rendered name under the active files prefix.
- Given a record whose stored template does not exist in the active configuration, or whose template render fails or renders an empty key, when it is re-filed, then the object is left in place, the reason is listed in the summary (`skipped`) and it is not counted as an error.
- Given a document whose computed target equals its current `file_key`, when it is re-filed, then nothing happens (`unchanged`).
- Given a document whose computed target differs, when it is re-filed, then the object is moved with a conflict suffix on name collision (copy first, existence verified, then the source removed), the record's `file_key` is updated and a `relocated` event is appended with the `file_key` diff — both in one transaction, with a best-effort S3 rollback of the move if the save fails (`moved`).
- Given the job is cancelled, when it runs, then it stops at the next document boundary without partial work on the current document.
- Given a per-document failure (S3 error, lookup error), when it occurs, then it is logged, counted in the summary and listed, and the re-filing continues with the next document.
- Given the tool runs in simulation mode, when it re-files, then every target is computed and checked (rendering, files prefix, collision resolution against the current bucket contents) but nothing is written: the object stays in place, `file_key` keeps its value and no `relocated` event is appended; the planned moves are listed old key → new key, with a collision mention when the target key is already occupied.

### US-44 — Re-file a selection of documents from the list

As the user,
I want to re-file just the documents I selected in the list — the same re-filing as the refile-all tool, applied to the selection only,
so that I can apply a configuration change to a handful of documents without running a full re-filing pass.

Acceptance criteria:

- Given the list, when a selection exists, then a fifth action, separated from the field-editing actions, triggers the re-file of the selected documents after a confirmation.
- Given a selected document, when the action reaches it, then it runs through `POST /api/archivist/documents/{id}/refile`, one request per document: the per-document re-file of US-43, driven by the same shared logic as the refile-all tool (no parallel business logic) — the stored template (the `default` one when absent) rendered from the stored fields under the active compiled configuration, the object moved when the target differs (conflict suffix on collision, copy first, existence verified, source removed), the record's `file_key` updated and a `relocated` event appended in one transaction, with a best-effort S3 rollback when the save fails.
- Given a document already where its name says, when the action reaches it, then nothing moves or is written (`unchanged`), and the summary counts it.
- Given a document whose stored template does not exist in the active configuration, or whose render fails or renders an empty key, when the action reaches it, then the document is left in place and reported as skipped with the reason; a skip is not an error.
- Given a `pending` document in the selection, when the action reaches it, then the route refuses it: review-queue documents are never moved (US-43).
- Given the action runs, when it finishes, then the summary shows the moved, unchanged, skipped and failed counts, one failure does not stop the rest, and the selection is cleared.
- Given an unauthenticated client, when it calls the route, then it is refused.

## Epic: Configuration Editor

### US-50 — Edit the active functional configuration

As the operator,
I want to edit the active functional configuration from a dedicated dashboard page,
so that I can maintain fields, classification rules and naming templates without editing the file outside the application.

Acceptance criteria:

- Given an authenticated superuser opens the configuration editor, when the page loads, then it fetches the complete active JSONC source from `GET /api/archivist/config/source`; unauthenticated requests are refused.
- Given the source is displayed, when I edit it, then `fields` appears as a table of name, French title, type and mandatory controls; rows can be added or removed.
- Given the rules are displayed, when I expand a section, then its JSONC rule text is editable; section headings and boundaries are determined by comments beginning `// --- `, and the original separator comments and group order are preserved when the draft is serialized.
- Given a section heading, when I click its pencil icon, then a dialog offers the current section name for editing; on a non-empty confirmation the section's `// --- ` comment is rewritten and the draft is re-tested, and an empty name is refused.
- Given a section heading, when I click its magnifier icon — a button between the pencil and the trash — then a popup opens whose upper part lists every when of the section, sub-rules indented under their parents, as single-selection radio choices.
- Given a chosen when in the popup, when the evaluation returns, then the lower part shows the documents matching the full when chain — parents evaluated before the chosen rule, hierarchy applied — with their `file_key` and status, over every document whatever its status, under the current draft configuration; the chain is evaluated in document context against the stored pages and fields, and nothing is persisted.
- Given an invalid section JSON, an invalid draft configuration or an invalid when expression, when the popup reacts, then the errors are displayed in the popup and no result list is shown as if it came from the draft.
- Given the templates are displayed, when I edit them, then a table exposes each template name and template text; rows can be added or removed.
- Given the page is opened from the document list, then the test input is empty and document navigation is hidden.
- Given the page is opened from a document detail page, then the test input is initialized with that record's `pages` JSON array and previous/next navigation follows the same document order as the list.
- Given a configuration draft is being edited, when I navigate to another document, then the draft is retained and tested against that document's pages.

### US-51 — Test a configuration draft against document text

As the operator,
I want to test my edited configuration against pasted page text or an existing document,
so that I can inspect the extracted values, target name, errors and rule trace before activating it.

Acceptance criteria:

- Given the draft or page array changes, when typing settles, then the page calls `POST /api/archivist/config/dry-run` with the full draft source and a JSON array of page texts.
- Given the submitted configuration is valid, when the dry-run completes, then the response shows the proposed fields, selected template, rendered target key (full S3 key, files prefix included), classification/rendering errors and the ordered rule trace using the same presentation as the document detail page.
- Given the JSONC or configuration is invalid, when the dry-run runs, then it reports the configuration errors and does not display stale results as if they came from the current draft.
- Given any draft dry-run, when it runs, then it compiles and evaluates the draft independently of the active configuration and persists nothing: no record, event, file or enum schema is changed.
- Given the dry-run route is called without superuser authentication, then the request is refused.

### US-52 — Save and activate a valid configuration

As the operator,
I want to save and activate a valid configuration draft,
so that subsequent polls and dashboard operations use the changes without a container restart.

Acceptance criteria:

- Given a draft, when I explicitly choose save and activate, then `PUT /api/archivist/config` validates and compiles the complete source before writing it to the active path selected by `ARCHIVIST_CONFIG` or `--config`.
- Given the containing directory is writable, when validation succeeds, then the source is written using a temporary sibling file and atomic rename; the newly compiled configuration becomes active for subsequent pipeline polls and API requests.
- Given configuration validation fails, when save is attempted, then the file and active configuration remain unchanged, the validation errors are shown, and the draft remains editable.
- Given writing or activation fails, when save is attempted, then the previous file and active configuration remain in use and the error is displayed.
- Given the active config path's containing directory is read-only, then save is refused with a clear error; the draft remains available for correction or export outside this page.
- Given save and activate is called without superuser authentication, then the request is refused.
