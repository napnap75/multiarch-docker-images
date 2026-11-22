# Interface Specification — Principles

This document records the cross-cutting principles and conventions the review
dashboard's interface has followed so far. It deliberately does not repeat what
the other specifications already cover: the per-page features and behaviors
live in `arch.md` section 7, the functional requirements in `user-stories.md`,
the installation and usage in `README.md`. Read this document as the "how" of
the interface, whatever the page; `arch.md` 7 remains the "what".

Everything below applies to the static frontend shipped in `pb_public/`
(`index.html`, `document.html`, `tools.html`, `config.html`, one script per
page, `common.js`, `styles.css`).

## 1. Technology principles

- **No toolchain.** Plain HTML, CSS and JavaScript, hand-written, vendored,
  served as static files by PocketBase (`pb_public/`); no build step, no
  framework, no bundler, no CDN — the application is 100% offline. JavaScript
  is deliberately conservative (ES5-style `var`/`function`, `"use strict"`,
  small global scripts) so any future edit needs no tooling to be understood.
- **Thin pages, one shared kernel.** Pages carry no embedded data: each page
  renders everything from API responses at startup. The shared helpers
  (`common.js`) own authentication, theme, typed field controls and value
  formatting; page scripts own layout and calling. No DOM library: direct
  `document` API and small builders.
- **Metadata-driven rendering.** The dashboard is entirely driven by
  `GET /api/archivist/config` (declared fields, enum vocabularies, statuses,
  template names): table columns, filters, form controls, bulk-edit field
  picker are built from the active configuration, never hardcoded. The only
  built-in knowledge is the fixed document plumbing (`status`, `file_key`,
  `file_name`, `created`, `updated`).
- **One operation = the most standard route possible.** The UI composes the
  standard PocketBase API (record PATCH, DELETE, list filters) whenever the
  intent is expressible through record data — the hooks then run per record
  and keep the event history correct (bulk edit, bulk delete, validation).
  A dedicated application route is added only when the intent cannot be
  expressed in record data (`/correct`, `/dry-run`, `/rerun`, `/when-match`).
  The UI never re-implements business logic; it sequences routes.

## 2. Authentication

- Every request carries the PocketBase `Authorization` header; the server
  keeps enforcing superuser-only access, the client never assumes it.
- The token is shared with the standard PocketBase admin UI through the
  `pocketbase_auth` localStorage entry, so signing in once covers both; a
  proactive silent refresh runs when the token expires within 24 hours.
- A 401 on any request, and a 403 on an application route
  (`/api/archivist/…`), triggers a blocking, non-cancellable login dialog
  (native `<dialog>`), then the blocked request is retried once with the new
  token. Failures of that single retry are surfaced, never retried in loops.
- Protected binary resources that must land in an `<iframe>` (the PDF
  preview) cannot carry the header: they are fetched with `apiBlob` and
  assigned as object URLs, revoked on page hide.

## 3. Visual language

- **One hand-written stylesheet, no framework** (`styles.css`), per the
  Beszel-inspired reference design: top bar, cards, compact tables, pill
  badges, dense controls at a 14px body size.
- **Semantic design tokens.** All colors are HSL custom properties with a
  light/dark pair switched by a `light`/`dark` class on `<html>`; the class
  is set by an inline head script before first paint (no theme flash), the
  manual choice persists in `archivist.theme` and falls back to the OS
  preference. `color-scheme` matches the class.
- **Rounded vocabularies.** One `--radius` derives every radius; `--ring`
  draws every focus ring (`:focus-visible` outlines throughout);
  `--destructive` is reserved for errors and destructive emphasis.
- **Iconography.** Icons are inline Font Awesome SVG paths, `fill:
  currentColor`, sized around 1rem, with `viewBox` kept in the markup.
  Repeated icons are JS constants (`TRASH_SVG`, `GLASS_SVG` in
  `config-editor.js`). Every icon-only control carries a French `aria-label`
  and a matching `title`.
- **Action idioms.** Text actions are `.btn` (`primary`/`secondary`); icon
  actions are square `.icon-control` / `.btn.iconbtn` buttons. The bulk bar
  groups its actions by family with vertical separators (field editing |
  re-file & re-run | delete). A trash icon means destruction. Destroying
  committed data always asks for a confirmation naming the count and the
  consequences first (the bulk document delete), while removing an unsaved
  draft element — a field or template row, a rule section — is immediate,
  because nothing is written until the explicit save. Every trash icon turns
  red on hover.

## 4. Typed controls and value conventions

- **One builder per concern.** `buildValueControl` (in `common.js`) builds
  the typed control of any declared field from its metadata: `number` input,
  `date` input, tri-state `boolean` dropdown, `enum` dropdown, `text` input.
  The same builder serves the document form, the bulk edit and the filters;
  a field's control looks and behaves identically everywhere.
- **Enum dropdowns.** An enum dropdown lists exactly the field's official
  values, then the "Ajouter une valeur…" sentinel entry which opens the
  registration modal (`POST /api/archivist/fields/{field}/values`) and
  selects the registered value. A value a record carries but the list does
  not know yet appears as a temporary option (the correction or import path
  registers it). Sentinel values are namespaced strings
  (`__archivist_add_value__`, `__archivist_empty__`) that never collide with
  real data.
- **Empty means "—".** A null/absent value renders as an em dash everywhere
  (cells, diffs, proposals); the leading "—" entry of a dropdown means "no
  value / no filter", and clearing a control reads back as
  `{clear: true}`. Rendering goes through the shared `fmtVal` (null → —,
  booleans as true/false, strings verbatim, dates normalized).
- **Dates at display precision.** Dates are shown truncated to
  `YYYY-MM-DD HH:MM`, whatever spelling the source carries (RFC 3339 from
  the engine, the space-separated PocketBase record format — both
  recognized). Date comparisons use the same display precision, so two
  spellings of the same instant never appear as a difference or highlight
  a proposal.
- **Labels.** User-facing names come from the declared `french` title when
  present, the field name otherwise; mandatory fields carry a ` *` mark.
  Identifiers that are data (statuses, enum values, rule expressions, keys)
  are shown verbatim, never translated.

## 5. Interaction principles

- **Debounce free typing** against the server: 1.5 s for the full-text page
  search, 300 ms for text filters, 400 ms for the form and editor dry-runs.
  Discrete selections (dropdown, date picker, checkbox) fire immediately.
- **Sequence guards.** Every async flow that can race a newer one carries a
  monotonically increasing sequence; a stale response is discarded, so a
  slow result never overwrites a fresher view.
- **Full region re-render.** After each change the affected DOM region
  (table body, panel, dialog section) is rebuilt from state — no in-place
  patching, no virtual DOM. State lives in a per-page object; rendering is
  a pure function of it.
- **Confirmation before irreversibility.** Bulk delete, re-file and re-run
  ask a `confirm()` stating the count and the consequences; run results are
  then reported as a single aggregated summary line (counts per outcome,
  first failure message) in place, with `alert()` only for blocked
  submissions.
- **Per-document batch loops.** Every bulk action loops the selection with
  one request per document, collects per-document successes, skips and
  failures, lets one failure never stop the rest, then refreshes the counts
  and the table and clears the selection.
- **State boundaries.** The URL carries only the document context
  (`document.html?id=`, `config.html?id=`), maintained with `pushState` and
  `popstate`. Transient cross-page context (the current page's document ids,
  powering the previous/next navigation) lives in
  `sessionStorage` `archivist.documentIds`. User preferences persist in
  `localStorage` (`archivist.theme`, `archivist.columns`, `archivist.sort`)
  and nothing else — no server-side UI state.
- **Editing guarded by validity.** Actions that commit derived data stay
  disabled while validation fails: "Valider" is disabled while dry-run
  errors remain; "Enregistrer et activer" is disabled until the draft
  compiles and until something changed; a save racing a new edit re-enters
  the draft state instead of losing it.
- **Polling, not sockets.** Long-running tool jobs are followed by 1 s
  polling of the job state; the panel is restored on reload from the
  latest stored job.

## 6. Error and feedback display

- Errors render in the shared `errbox` style (destructive color) as plain
  lines, one per error, honoring the backend error shape (`message` +
  `errors[]` for 422 responses, the message otherwise). The boxes that
  announce themselves carry `role="alert"` (the configuration editor's test
  and popup error boxes, the modal error lines, the detail page's error
  boxes).
- Progress and saving states use a dedicated muted status line
  (`role="status"`, `aria-live="polite"`) — "Test en cours…",
  "Modifications non activées" — and the bulk bar's result line reports
  each batch through the same `role="status"`. The bulk bar stays visible
  while its result line shows a message, even though the action has cleared
  the selection, so the outcome remains readable without re-selecting. No
  toast, no modal success dialogs.
- Errors block what they invalidate: dry-run errors disable the correction,
  save errors re-enable editing and keep the draft, a failed filter or
  load leaves the previous view in place.
- The rule trace and event diffs mark deletions as "valeur supprimée" and
  failing rules with their message, in the destructive style, inside the
  otherwise neutral trace list.

## 7. Layout idioms

- One `.wrap` centered container per page, `.card` blocks, sticky table
  headers, horizontal scroll for wide tables, `word-break`/`overflow-wrap:
  anywhere` for keys and expressions, `mono` for anything machine-formatted
  (keys, expressions, JSON).
- Detail and configuration pages are two-column grids (preview or source
  left, form or test bench right) collapsing to one column on narrow
  screens; collapsible secondary panels save new routes or pages by
  toggling a `hidden` div.
- Collapsible rule sections are native `<details>`/`<summary>`; the
  summary carries the section title plus its icon actions (pencil, then
  magnifier, trash aligned right), and icon clicks
  `preventDefault()` so they never toggle the fold.
- Dialogs are native `<dialog>` elements (`showModal`) with a shared card
  look and backdrop; the login dialog is the only one that swallows
  `cancel`.

## 8. Accessibility

- Every icon-only control: `aria-label` (+ `title`), a square touch target
  of 2rem or more (1.6rem only for the compact icons inside a rule section
  header), `:focus-visible` ring from the tokens.
- Async regions announce through `aria-live` (status lines, popup results,
  bulk results); alerts use `role="alert"`; expandable panels and details expose
  `aria-expanded`/native summary semantics; disabled navigation links use
  `aria-disabled` and leave the tab order.
- Native interactive elements (`dialog`, `details`, `input`, `select`,
  `button`) are preferred over role-played divs, so keyboard behavior is
  inherited, not re-implemented.

## 9. Internationalization

- The interface language is French (labels, placeholders, confirmations,
  error wording, aria-labels); data values, statuses and configuration
  content stay verbatim. Field titles come from the `french` metadata of
  the configuration, so renaming a display label is a configuration edit,
  not a code change.
