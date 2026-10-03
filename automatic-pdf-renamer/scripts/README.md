# Scripts

Standalone Python scripts for the Automatic PDF Renamer project.

## Available Scripts

| Script | Purpose | Dependencies |
|--------|---------|--------------|
| [`import_paperless.py`](import_paperless.py) | Import documents from Paperless-ngx to storage | requests, boto3, pymupdf, python-dateutil |

---

## `import_paperless.py` - Paperless-ngx Initial Import

**User Story 0**: Standalone script that imports existing paperless-ngx documents into a Garage S3 bucket (or local filesystem) with their metadata as sidecars.

### Features

- ✅ Connects to Paperless-ngx REST API
- ✅ Connects to Garage S3 or local filesystem
- ✅ Lists all documents via paginated API
- ✅ Maps Paperless metadata to sidecar fields
- ✅ Downloads PDFs and extracts first-page text
- ✅ Atomic sidecar writes (temp-key + copy pattern)
- ✅ Resumability (skip if already imported with matching SHA256)
- ✅ Dry-run mode for testing
- ✅ Summary reporting
- ✅ Proper exit codes

### Requirements

Install dependencies:

```bash
pip install -r scripts/requirements.txt
```

Or individually:

```bash
pip install requests boto3 pymupdf python-dateutil PyYAML
```

### Usage

#### Environment Variables (Recommended)

Set these before running:

```bash
# Paperless-ngx
 export PAPERLESS_URL="http://paperless:8000"
 export PAPERLESS_TOKEN="your-api-token"

# S3 Storage (Garage)
 export S3_ENDPOINT_URL="http://garage:7777"
 export S3_ACCESS_KEY_ID="your-access-key"
 export S3_SECRET_ACCESS_KEY="your-secret-key"
 export S3_BUCKET_NAME="pdf-renamer"

# File Storage (for testing)
 export FILE_BASE_DIR="./import_storage"
```

#### Command Line

```bash
# S3 storage (default)
python scripts/import_paperless.py

# File storage for testing
python scripts/import_paperless.py \
    --storage-backend file \
    --file-base-dir ./test_storage

# Dry run (list what would be imported)
python scripts/import_paperless.py --dry-run

# Limited import (test with 5 documents)
python scripts/import_paperless.py --limit 5

# Verbose mode
python scripts/import_paperless.py --verbose

# Full options
python scripts/import_paperless.py \
    --paperless-url http://paperless:8000 \
    --paperless-token your-token \
    --storage-backend s3 \
    --s3-endpoint http://garage:7777 \
    --s3-access-key your-key \
    --s3-secret-key your-secret \
    --s3-bucket pdf-renamer \
    --dry-run \
    --limit 10 \
    --verbose
```

### Storage Backends

The import script supports two storage backends:

| Backend | Description | Use Case |
|---------|-------------|----------|
| `s3` | Garage S3 via boto3 | Production |
| `file` | Local filesystem | Testing, development |

#### S3 Backend

Requires:
- `--s3-endpoint` or `S3_ENDPOINT_URL`
- `--s3-access-key` or `S3_ACCESS_KEY_ID`
- `--s3-secret-key` or `S3_SECRET_ACCESS_KEY`
- `--s3-bucket` or `S3_BUCKET_NAME`

The script will refuse to run if the `files/` prefix is not empty (to prevent accidental overwrites).

#### File Backend

Requires:
- `--file-base-dir` or `FILE_BASE_DIR` (default: `./import_storage`)

Creates a directory structure matching the S3 layout:
```
file_base_dir/
  files/
    Template1/
      document1.pdf
      document1.pdf.meta.json
    Template2/
      document2.pdf
      document2.pdf.meta.json
  tmp/
    (temporary files for atomic writes)
```

### Metadata Mapping

Paperless-ngx fields are mapped to sidecar fields as follows:

| Sidecar Field | Paperless Source | Notes |
|---------------|------------------|-------|
| `template_name` | `storage_path` (first segment) | Document family |
| `emission_date` | `created` | Document date |
| `emitting_company` | `correspondent.name` | Correspondent |
| `document_type` | `document_type.name` | Document type |
| `period` | Filename (parsed) or `created` (fallback) | Month-year, quarter, year |
| `extracted_text` | PDF first page | Via PyMuPDF |
| `optional_fields` | `labels` | Each label becomes an optional field |
| `sha256` | PDF binary | Computed from downloaded PDF |
| `status` | Hardcoded | `validated` |
| `confidence` | Hardcoded | `1.0` |

### Output Structure

Documents are stored at:
```
files/{template}/{original_filename}
files/{template}/{original_filename}.meta.json
```

Example:
```
files/Banque/Relevé de compte août 2025.pdf
files/Banque/Relevé de compte août 2025.pdf.meta.json
```

### Resumability

The script can be interrupted and rerun safely:
- Documents already imported with matching SHA256 are skipped
- Atomic sidecar writes ensure no partial/corrupted sidecars
- Summary shows imported, skipped, and failed counts

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success (all documents imported) |
| 1 | Failure (one or more documents failed) |
| 130 | Interrupted by user (SIGINT) |

### Example Output

```
2026-10-02 12:00:00,000 - INFO - Starting Paperless-ngx import...
2026-10-02 12:00:00,001 - INFO - Using S3 storage backend: http://garage:7777/pdf-renamer
2026-10-02 12:00:00,002 - INFO - Fetching documents from Paperless...
2026-10-02 12:00:05,123 - INFO - Retrieved 150 documents from Paperless
2026-10-02 12:00:05,124 - INFO - Processing document 1: Relevé août 2025.pdf
2026-10-02 12:00:05,456 - INFO - Successfully imported document 1
2026-10-02 12:00:05,457 - INFO - Processing document 2: Facture Q3 2025.pdf
...

============================================================
IMPORT SUMMARY
============================================================
Storage backend: s3
Imported:   150
Skipped:    0
Failed:     0
Period fallbacks: 0

Per-template counts:
  Banque: 75
  Factures: 50
  Contrats: 25
============================================================
```

### Troubleshooting

#### "Missing required values" error

Make sure all required environment variables are set or passed as CLI arguments:

```bash
# Check what's set
echo $PAPERLESS_URL
echo $S3_ENDPOINT_URL
```

#### "Bucket is not empty" error

The script refuses to run if `files/` prefix already has objects. Options:

1. **Use a different bucket**
2. **Use file storage for testing**: `--storage-backend file --file-base-dir ./test`
3. **Empty the bucket first** (if you're sure it's safe)

#### "PyMuPDF not available" warning

Text extraction will be skipped. Install it for full functionality:

```bash
pip install pymupdf
```

#### Connection errors

Check that:
- Paperless-ngx is running and accessible
- S3/Garage endpoint is correct
- Credentials are valid
- Network connectivity is available

### Development / Testing

For testing without Paperless-ngx or S3:

```bash
# Use file storage
python scripts/import_paperless.py \
    --storage-backend file \
    --file-base-dir ./test_storage \
    --dry-run

# Or create a mock Paperless client (see tests/ directory)
```

---

## Architecture

The import script uses the same reusable components as the main service:

- `app/sidecar.py` - Sidecar data model and I/O
- `app/storage/` - Storage backend abstraction
- `app/period.py` - Period parsing and computation
- `app/extractor.py` - PDF text extraction
- `app/config.py` - Configuration utilities

This ensures consistency between the import script and the main service.

---

## License

Same as the main project. See [../LICENSE](../LICENSE).
