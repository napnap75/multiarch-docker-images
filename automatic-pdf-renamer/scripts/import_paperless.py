#!/usr/bin/env python3
"""Paperless-ngx Initial Import Script.

User Story 0: Standalone script to import existing paperless-ngx documents
into a Garage S3 bucket with their metadata as sidecars.

This script runs once, offline, from the owner's machine or the server.
It is NOT part of the renamer container and has no dashboard surface.
"""

import argparse
import hashlib
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sidecar import (
    Sidecar,
    SidecarStatus,
    build_sidecar,
    get_sidecar_key,
    matching_sha256,
    read_sidecar,
    sidecar_exists,
    write_atomic,
)
from app.storage import get_storage_backend

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class PaperlessDocument:
    """Represents a document from Paperless-ngx API."""

    id: int
    filename: str
    storage_path: str
    created: str  # Document date (emission date)
    correspondent_name: str
    document_type_name: str
    labels: dict[str, Any]  # Label name -> value
    download_url: str


@dataclass
class ImportStats:
    """Statistics for the import run."""

    imported: int = 0
    skipped: int = 0
    failed: int = 0
    period_fallbacks: int = 0
    per_template_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class PaperlessClient:
    """Client for Paperless-ngx REST API."""

    def __init__(self, base_url: str, token: str, timeout: int = 30):
        """Initialize the Paperless client.

        Args:
            base_url: Paperless-ngx base URL (e.g., http://paperless:8000).
            token: API authentication token.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
        })

    def _get(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        """Make a GET request to the Paperless API."""
        url = f"{self.base_url}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"GET {url} failed: {e}")
            raise

    def _get_raw(self, endpoint: str) -> bytes:
        """Make a GET request and return raw bytes."""
        url = f"{self.base_url}{endpoint}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            logger.error(f"GET {url} failed: {e}")
            raise

    def list_documents(self, page: int = 1, page_size: int = 100) -> list[dict[str, Any]]:
        """List all documents from Paperless (paginated).

        Args:
            page: Page number.
            page_size: Number of results per page.

        Returns:
            List of document dicts.
        """
        data = self._get("/api/documents/", params={"page": page, "page_size": page_size})
        return data.get("results", [])

    def get_document(self, doc_id: int) -> dict[str, Any]:
        """Get a single document's metadata.

        Args:
            doc_id: The document ID.

        Returns:
            Document metadata dict.
        """
        return self._get(f"/api/documents/{doc_id}/")

    def get_all_documents(self) -> list[PaperlessDocument]:
        """Get all documents from Paperless (handles pagination).

        Returns:
            List of PaperlessDocument objects.
        """
        all_docs: list[PaperlessDocument] = []
        page = 1
        page_size = 100

        while True:
            docs = self.list_documents(page=page, page_size=page_size)
            if not docs:
                break

            for doc in docs:
                # Get full document details
                full_doc = self.get_document(doc["id"])
                
                # Parse labels into a dict
                labels = {}
                for label in full_doc.get("labels", []):
                    labels[label["name"]] = label.get("value", True)

                paperless_doc = PaperlessDocument(
                    id=full_doc["id"],
                    filename=full_doc["filename"],
                    storage_path=full_doc.get("storage_path", ""),
                    created=full_doc.get("created", ""),  # This is the document date
                    correspondent_name=full_doc.get("correspondent", {}).get("name", ""),
                    document_type_name=full_doc.get("document_type", {}).get("name", ""),
                    labels=labels,
                    download_url=f"{self.base_url}/api/documents/{full_doc['id']}/download/",
                )
                all_docs.append(paperless_doc)

            # Check if there are more pages
            if len(docs) < page_size:
                break
            page += 1

        logger.info(f"Retrieved {len(all_docs)} documents from Paperless")
        return all_docs

    def download_document(self, doc_id: int) -> bytes:
        """Download a document's PDF.

        Args:
            doc_id: The document ID.

        Returns:
            PDF content as bytes.
        """
        return self._get_raw(f"/api/documents/{doc_id}/download/")


class PDFProcessor:
    """Processes PDF content for import."""

    def __init__(self):
        """Initialize the PDF processor."""
        # Import extractor (optional dependency)
        try:
            from app.extractor import extract_first_page_text
            self._extract_text = extract_first_page_text
        except ImportError:
            logger.warning("PyMuPDF not available, text extraction will be skipped")
            self._extract_text = None

    def compute_sha256(self, pdf_bytes: bytes) -> str:
        """Compute SHA256 hash of PDF content.

        Args:
            pdf_bytes: The PDF content.

        Returns:
            Hex-encoded SHA256 hash.
        """
        return hashlib.sha256(pdf_bytes).hexdigest()

    def extract_text(self, pdf_bytes: bytes) -> str:
        """Extract first page text from PDF.

        Args:
            pdf_bytes: The PDF content.

        Returns:
            Extracted text, or empty string if extraction fails.
        """
        if self._extract_text is None:
            logger.warning("Text extraction not available")
            return ""

        try:
            return self._extract_text(pdf_bytes)
        except Exception as e:
            logger.warning(f"Text extraction failed: {e}")
            return ""


class SidecarBuilder:
    """Builds sidecar metadata from Paperless documents."""

    def __init__(self, template_mappings: dict[str, str] | None = None):
        """Initialize the sidecar builder.

        Args:
            template_mappings: Optional dict mapping Paperless storage_path
                              prefixes to template names.
        """
        self.template_mappings = template_mappings or {}

    def get_template_name(self, storage_path: str) -> str:
        """Get template name from storage path.

        Uses the first path segment as the document family (template).

        Args:
            storage_path: The Paperless storage path.

        Returns:
            Template name (first segment of storage path).
        """
        if not storage_path:
            return "unknown"

        # Get first path segment
        parts = storage_path.strip("/").split("/")
        if parts:
            return parts[0]
        return "unknown"

    def parse_period_from_filename(self, filename: str) -> str | None:
        """Parse period from filename.

        Args:
            filename: The document filename.

        Returns:
            Period string, or None if cannot be parsed.
        """
        try:
            from app.period import parse_period_from_filename
            return parse_period_from_filename(filename)
        except Exception:
            return None

    def build_sidecar(
        self,
        doc: PaperlessDocument,
        pdf_bytes: bytes,
        sha256: str,
        extracted_text: str,
        template_name: str | None = None,
    ) -> Sidecar:
        """Build a sidecar from a Paperless document.

        Args:
            doc: The Paperless document.
            pdf_bytes: The PDF content.
            sha256: The SHA256 hash of the PDF.
            extracted_text: The extracted first-page text.
            template_name: Optional template name override.

        Returns:
            A Sidecar object with all metadata.
        """
        # Determine template
        if template_name:
            final_template = template_name
        else:
            final_template = self.get_template_name(doc.storage_path)

        # Parse period from filename
        period = self.parse_period_from_filename(doc.filename)
        if not period:
            # Fallback: use emission date's month-year
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(doc.created).date()
                from app.period import format_month_year
                period = format_month_year(dt.month, dt.year)
                logger.warning(f"Using fallback period for doc {doc.id}: {period}")
            except Exception:
                period = doc.created  # Last resort

        # Build optional fields from labels
        optional_fields = {}
        for label_name, label_value in doc.labels.items():
            # Skip labels that collide with common field names
            common_fields = {"template", "emission_date", "emitting_company", "document_type", "period"}
            if label_name.lower() in common_fields:
                logger.warning(f"Label '{label_name}' collides with common field, skipping")
                continue
            optional_fields[label_name] = label_value

        # Construct the original key
        if doc.storage_path:
            original_key = f"{doc.storage_path}/{doc.filename}"
        else:
            original_key = doc.filename

        # Current key is the same as original for import (no renames)
        current_key = f"files/{final_template}/{doc.filename}"

        # Build the sidecar
        sidecar = build_sidecar(
            sha256=sha256,
            original_key=original_key,
            current_key=current_key,
            status=SidecarStatus.VALIDATED,
            template_name=final_template,
            emission_date=doc.created,
            period=period,
            emitting_company=doc.correspondent_name,
            document_type=doc.document_type_name,
            confidence=1.0,
            optional_fields=optional_fields,
            extracted_text=extracted_text,
        )

        # Add imported event
        sidecar.add_event(
            "imported",
            {
                "source": "paperless-ngx",
                "document_id": doc.id,
                "document_url": doc.download_url,
            },
        )

        return sidecar


class Importer:
    """Main import class that orchestrates the import process."""

    def __init__(
        self,
        paperless_url: str,
        paperless_token: str,
        s3_endpoint: str,
        s3_access_key: str,
        s3_secret_key: str,
        s3_bucket: str,
        dry_run: bool = False,
        limit: int | None = None,
        template_mappings: dict[str, str] | None = None,
    ):
        """Initialize the importer.

        Args:
            paperless_url: Paperless-ngx API URL.
            paperless_token: Paperless-ngx API token.
            s3_endpoint: Garage S3 endpoint URL.
            s3_access_key: S3 access key.
            s3_secret_key: S3 secret key.
            s3_bucket: S3 bucket name.
            dry_run: If True, only list what would be imported.
            limit: Maximum number of documents to import (for testing).
            template_mappings: Optional dict mapping storage_path to template.
        """
        self.paperless_url = paperless_url
        self.paperless_token = paperless_token
        self.s3_endpoint = s3_endpoint
        self.s3_access_key = s3_access_key
        self.s3_secret_key = s3_secret_key
        self.s3_bucket = s3_bucket
        self.dry_run = dry_run
        self.limit = limit
        self.template_mappings = template_mappings

        # Initialize clients
        self.paperless_client = PaperlessClient(paperless_url, paperless_token)
        self.pdf_processor = PDFProcessor()
        self.sidecar_builder = SidecarBuilder(template_mappings)

        # Initialize storage
        self.storage = get_storage_backend(
            "s3",
            endpoint_url=s3_endpoint,
            aws_access_key_id=s3_access_key,
            aws_secret_access_key=s3_secret_key,
            bucket_name=s3_bucket,
        )

        # Stats
        self.stats = ImportStats()

    def check_bucket_empty(self, prefix: str = "files/") -> bool:
        """Check if the bucket is empty under the given prefix.

        Args:
            prefix: The prefix to check.

        Returns:
            True if bucket is empty (no objects), False otherwise.
        """
        objects = self.storage.list_objects(prefix)
        if objects:
            logger.error(f"Bucket is not empty under {prefix}: found {len(objects)} objects")
            logger.error(f"First few objects: {objects[:5]}")
            return False
        return True

    def import_document(self, doc: PaperlessDocument) -> bool:
        """Import a single document.

        Args:
            doc: The Paperless document to import.

        Returns:
            True if import succeeded, False otherwise.
        """
        logger.info(f"Processing document {doc.id}: {doc.filename}")

        try:
            # Download PDF
            pdf_bytes = self.paperless_client.download_document(doc.id)
            sha256 = self.pdf_processor.compute_sha256(pdf_bytes)

            # Extract text
            extracted_text = self.pdf_processor.extract_text(pdf_bytes)

            # Build sidecar
            sidecar = self.sidecar_builder.build_sidecar(
                doc, pdf_bytes, sha256, extracted_text
            )

            # Determine keys
            pdf_key = sidecar.current_key
            sidecar_key = get_sidecar_key(pdf_key)

            # Check for resumability
            if sidecar_exists(self.storage, pdf_key) and matching_sha256(
                self.storage, pdf_key, sha256
            ):
                logger.info(f"Skipping document {doc.id}: already imported with matching SHA256")
                self.stats.skipped += 1
                self.stats.per_template_counts[sidecar.template_name] = (
                    self.stats.per_template_counts.get(sidecar.template_name, 0) + 1
                )
                return True

            # Dry run: just log what would happen
            if self.dry_run:
                logger.info(f"[DRY RUN] Would import: {pdf_key}")
                logger.info(f"[DRY RUN]   SHA256: {sha256}")
                logger.info(f"[DRY RUN]   Template: {sidecar.template_name}")
                logger.info(f"[DRY RUN]   Period: {sidecar.period}")
                self.stats.imported += 1
                self.stats.per_template_counts[sidecar.template_name] = (
                    self.stats.per_template_counts.get(sidecar.template_name, 0) + 1
                )
                return True

            # Upload PDF
            self.storage.put_object(pdf_key, pdf_bytes)
            logger.debug(f"Uploaded PDF to {pdf_key}")

            # Write sidecar atomically
            write_atomic(self.storage, sidecar, sidecar_key)
            logger.debug(f"Wrote sidecar to {sidecar_key}")

            # Update stats
            self.stats.imported += 1
            self.stats.per_template_counts[sidecar.template_name] = (
                self.stats.per_template_counts.get(sidecar.template_name, 0) + 1
            )

            logger.info(f"Successfully imported document {doc.id}")
            return True

        except Exception as e:
            error_msg = f"Failed to import document {doc.id}: {e}"
            logger.error(error_msg)
            self.stats.failed += 1
            self.stats.errors.append(error_msg)
            return False

    def run(self) -> bool:
        """Run the import process.

        Returns:
            True if import succeeded (no failures), False otherwise.
        """
        logger.info("Starting Paperless-ngx import...")

        # Check bucket is empty
        if not self.dry_run:
            if not self.check_bucket_empty("files/"):
                logger.error("Refusing to run: bucket is not empty under files/")
                return False

        # Get all documents from Paperless
        logger.info("Fetching documents from Paperless...")
        docs = self.paperless_client.get_all_documents()

        if self.limit:
            docs = docs[: self.limit]
            logger.info(f"Limiting to {len(docs)} documents")

        if not docs:
            logger.info("No documents found in Paperless")
            return True

        logger.info(f"Found {len(docs)} documents to import")

        # Import each document
        for doc in docs:
            self.import_document(doc)

        # Print summary
        self.print_summary()

        # Return True if no failures
        return self.stats.failed == 0

    def print_summary(self) -> None:
        """Print import summary."""
        print("\n" + "=" * 60)
        print("IMPORT SUMMARY")
        print("=" * 60)
        print(f"Imported:   {self.stats.imported}")
        print(f"Skipped:    {self.stats.skipped}")
        print(f"Failed:     {self.stats.failed}")
        print(f"Period fallbacks: {self.stats.period_fallbacks}")
        print("\nPer-template counts:")
        for template, count in sorted(self.stats.per_template_counts.items()):
            print(f"  {template}: {count}")

        if self.stats.errors:
            print("\nErrors:")
            for error in self.stats.errors:
                print(f"  - {error}")
        print("=" * 60)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Import documents from Paperless-ngx to Garage S3 bucket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables:
  PAPERLESS_URL          Paperless-ngx API URL
  PAPERLESS_TOKEN        Paperless-ngx API token
  S3_ENDPOINT_URL        Garage S3 endpoint URL
  S3_ACCESS_KEY_ID       S3 access key
  S3_SECRET_ACCESS_KEY  S3 secret key
  S3_BUCKET_NAME         S3 bucket name

All environment variables are required unless provided as arguments.
        """,
    )

    parser.add_argument(
        "--paperless-url",
        help="Paperless-ngx API URL",
        default=os.environ.get("PAPERLESS_URL"),
    )
    parser.add_argument(
        "--paperless-token",
        help="Paperless-ngx API token",
        default=os.environ.get("PAPERLESS_TOKEN"),
    )
    parser.add_argument(
        "--s3-endpoint",
        help="Garage S3 endpoint URL",
        default=os.environ.get("S3_ENDPOINT_URL"),
    )
    parser.add_argument(
        "--s3-access-key",
        help="S3 access key",
        default=os.environ.get("S3_ACCESS_KEY_ID"),
    )
    parser.add_argument(
        "--s3-secret-key",
        help="S3 secret key",
        default=os.environ.get("S3_SECRET_ACCESS_KEY"),
    )
    parser.add_argument(
        "--s3-bucket",
        help="S3 bucket name",
        default=os.environ.get("S3_BUCKET_NAME"),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be imported without actually importing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of documents to import (for testing)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    return parser.parse_args()


def validate_env(args) -> None:
    """Validate that required environment variables or arguments are set.

    Args:
        args: Parsed command line arguments.

    Raises:
        ValueError: If required values are missing.
    """
    required = [
        ("Paperless URL", args.paperless_url),
        ("Paperless token", args.paperless_token),
        ("S3 endpoint", args.s3_endpoint),
        ("S3 access key", args.s3_access_key),
        ("S3 secret key", args.s3_secret_key),
        ("S3 bucket", args.s3_bucket),
    ]

    missing = []
    for name, value in required:
        if not value:
            missing.append(name)

    if missing:
        raise ValueError(f"Missing required values: {', '.join(missing)}")


def main():
    """Main entry point."""
    args = parse_args()

    # Configure logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        validate_env(args)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    try:
        # Create importer
        importer = Importer(
            paperless_url=args.paperless_url,
            paperless_token=args.paperless_token,
            s3_endpoint=args.s3_endpoint,
            s3_access_key=args.s3_access_key,
            s3_secret_key=args.s3_secret_key,
            s3_bucket=args.s3_bucket,
            dry_run=args.dry_run,
            limit=args.limit,
        )

        # Run import
        success = importer.run()

        # Exit with appropriate code
        if success:
            logger.info("Import completed successfully")
            sys.exit(0)
        else:
            logger.error("Import completed with failures")
            sys.exit(1)

    except KeyboardInterrupt:
        logger.info("Import interrupted by user")
        sys.exit(130)  # SIGINT exit code
    except Exception as e:
        logger.error(f"Import failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
