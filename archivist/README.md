# Archivist

Archivist automatically classifies and files your PDF documents. It watches the `inbox/` prefix of a self-hosted S3 bucket, identifies each document from the text of its pages (emitting company, type, date, business fields), then moves it — renamed according to a configurable template — into an organized prefix structure. Documents that cannot be classified with certainty go to `pending/` for review in a local web dashboard.

The application is 100% offline: no cloud APIs, no data ever leaves your server.

## How it works

1. A PDF is dropped into `inbox/` (via Nextcloud or any S3 client).
2. Archivist extracts the text page by page, applies the classification rules, and computes the new file name.
3. Valid result: the file is moved to `files/`, under the path produced by its naming template. Status `processed`.
4. Invalid result (missing mandatory field, failing rule, unknown template): the file goes to `pending/`, along with the recognized fields and the errors. Status `pending`.
5. From the dashboard, you correct the fields of a `pending` file: it is re-filed and takes the status `validated`. You can also directly validate a `processed` file.

Duplicates (same sha256) are ignored: the `inbox/` copy is deleted, the original stays in place — no data is lost, the content is identical.

## Prerequisites

- An S3 bucket, with three prefixes: `inbox/`, `pending/`, `files/`. The prefixes are created on first startup if they do not exist. Versioning is not required, but it is strongly recommended: without it, a file deleted by the application or by a bug is lost for good; with it, deleted objects stay recoverable via delete markers. The application logs a warning at startup when versioning is disabled (visible in the admin dashboard, Dashboard > Logs).
- An S3 user with read/write access to the bucket.
- Docker.

## Installation

```bash
docker run -d --name archivist \
  --restart unless-stopped \
  -p 8090:8090 \
  -v archivist-data:/app/pb_data \
  -v /path/to/config.jsonc:/app/config.jsonc:ro \
  -e ARCHIVIST_S3_ENDPOINT="s3.mydomain.fr:9000" \
  -e ARCHIVIST_S3_ACCESS_KEY="..." \
  -e ARCHIVIST_S3_SECRET_KEY="..." \
  -e ARCHIVIST_S3_BUCKET="documents" \
  -e ARCHIVIST_S3_USE_SSL="true" \
  -e ARCHIVIST_ADMIN_EMAIL="me@mydomain.fr" \
  -e ARCHIVIST_ADMIN_PASSWORD="..." \
  napnap75/archivist:latest
```

The dashboard is available at `http://<server>:8090/`, the PocketBase admin at `http://<server>:8090/_/`.

## Building the image

```bash
docker build -t napnap75/archivist:latest archivist/
```

The build has two stages: the builder compiles a static binary (CGO disabled), and the runtime image is `scratch` containing only that binary, the CA bundle (the S3 endpoint is HTTPS by default), the timezone database, and the writable data directories. The container runs as the non-root user 65532: a named volume for `/app/pb_data` works out of the box; for a host bind mount, `chown 65532:65532` the directory first. The image has no shell, so `docker exec` is not available.

## Configuration

Two distinct configurations:

- Technical (infrastructure): container environment variables. Holds the S3 server address, credentials, bucket name, admin account.
- Functional (business): the `config.jsonc` file, mounted into the container. Holds the fields to extract, the classification rules, and the naming templates.

The functional configuration is reloaded only when the container restarts.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `ARCHIVIST_S3_ENDPOINT` | yes | S3 server address (host:port) |
| `ARCHIVIST_S3_ACCESS_KEY` | yes | S3 access key |
| `ARCHIVIST_S3_SECRET_KEY` | yes | S3 secret |
| `ARCHIVIST_S3_BUCKET` | yes | Bucket name |
| `ARCHIVIST_S3_USE_SSL` | no | `true` (default) for HTTPS, `false` for HTTP |
| `ARCHIVIST_S3_REGION` | no | S3 region used for request signatures, default `garage` |
| `ARCHIVIST_POLL_INTERVAL` | no | Cron expression for polling `inbox/`, default `* * * * *` (every minute) |
| `ARCHIVIST_ADMIN_EMAIL` | yes | Admin account (dashboard and PocketBase) |
| `ARCHIVIST_ADMIN_PASSWORD` | yes | Admin account password |
| `ARCHIVIST_HTTP_ADDR` | no | Listen address, default `0.0.0.0:8090` |

The `pb_data` volume holds the PocketBase SQLite database (documents, history, full-text search index): it must persist across restarts.

### The config.jsonc file

Three sections:

- `fields`: fields to extract, with name, type (`text`, `number`, `date`, `boolean`, `enum`) and whether they are mandatory. An unidentified mandatory field sends the document to `pending/`.
- `rules`: an ordered tree of rules. Each rule activates if its `when` expression is true, then assigns fields (`set`), removes some (`delete`), and evaluates its child rules.
- `templates`: naming templates. A template produces the full path under `files/`, with `{...}` expressions evaluated in the document context.

A commented `config.jsonc` example file is provided in the repository. The dashboard offers a dry-run mode to test rules on an existing document.

### Minimal example

```jsonc
{
  "fields": [
    { "name": "emittingCompany", "type": "text", "mandatory": true },
    { "name": "documentType",    "type": "enum", "mandatory": true },
    { "name": "emissionDate",    "type": "date", "mandatory": true },
    { "name": "template",        "type": "text", "mandatory": false }
  ],
  "rules": [
    {
      "when": "pages[0] contains `Relevé de compte`",
      "set": [
        { "field": "documentType", "value": "Relevé de compte" },
        { "field": "template",     "value": "bank_statement" }
      ]
    }
  ],
  "templates": {
    "bank_statement": "{emittingCompany}/{emissionDate.Format(`2006-01-02`)}_{documentType}.pdf",
    "default":        "Divers/{documentType}.pdf"
  }
}
```

## Dashboard

- Document list with status, filters, and combined search: full text (content of all pages) and fields.
- Detail view with PDF preview (streamed from S3) and extracted fields. Debug mode shows the detail of every rule evaluation.
- Correction: a form to edit the fields. Saving is only possible if the selected template can be evaluated without error. A corrected file is re-filed and becomes `validated`; a corrected `pending` file leaves the review queue.
- Validation: direct transition from `processed` to `validated`.
- History: every document has a complete, timestamped trace (import, correction, validation) readable from its detail view.

## Nextcloud integration

Mount the S3 bucket as Nextcloud external storage. Users drop files into `inbox/`, browse and share from `files/`. Archivist and Nextcloud work on the same objects; neither is the exclusive interface of the other.

## Logs

- Startup errors (missing environment variable, invalid `config.jsonc`) are written to the container's stderr: read them with `docker logs archivist`.
- Runtime activity — imports, duplicates, recoveries, and the rule-evaluation trace at Debug level — is logged through PocketBase's logger and browsable from the admin dashboard: Dashboard > Logs, filterable by level and message. Retention and the minimum log level are configured from the standard PocketBase logs settings.

## E2E tests

A single-file end-to-end suite runs a built image against a disposable Garage container and follows documents through the whole pipeline (import, duplicate, pending, correction, validation, append-only, recovery, restart):

```
cd archivist
E2E_IMAGE=archivist:e2e go test ./e2e -v -count=1
```

Build the image first: `docker build -t archivist:e2e .` (see Building the image).

Without `E2E_IMAGE` the suite skips. The suite drives the poller through the superuser-only `POST /api/archivist/poll` route instead of waiting for the cron. `E2E_KEEP=1` leaves the containers running after the run for debugging; `GARAGE_IMAGE` overrides the Garage image (default `dxflrs/garage:latest`). See arch.md section 10.

## Troubleshooting

- A file stays in `inbox/`: check the logs — admin dashboard, Dashboard > Logs (`docker logs archivist` shows startup errors). Frequent causes: unreadable PDF (goes to `pending/`), transient S3 error (retried on the next pass).
- A file goes to `pending/`: open its detail view in the dashboard; the classification error(s) are displayed there.
- The container does not start: invalid `config.jsonc`. The full error message is in the container logs; the application refuses to start with a partially valid configuration.
- After editing `config.jsonc`: `docker restart archivist`.
