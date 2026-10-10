#!/usr/bin/env python3
"""Paperless-ngx Import Script for Automatic PDF Renamer.

User Story: Import documents from Paperless-ngx into a Garage S3 bucket
with config-driven processing and auto-updating configuration.

Workflow:
1. Fetch all documents from Paperless-ngx
2. Auto-update config.jsonc with missing companies, document types, and additional field values
3. Resolve mapping for each document (template, period_format, additional_fields)
4. Match Paperless tags to additional field categories (strict validation)
5. Calculate period from created_date
6. Write to files/ if all valid, or pending/{paperless_id} if validation fails
7. Write sidecar with full metadata (including errors for pending)

Note: This script runs from the owner's machine or server, NOT in the renamer container.
It processes all documents fresh on each run (clears files/ and pending/ first).

Storage Backends:
- S3: For production Garage S3 (default)
- File: For local filesystem testing
"""

import argparse
import hashlib
import json
import logging
import os
from pydoc import doc
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.extractor import extract_first_page_text
from app.sidecar import (
    Sidecar,
    SidecarStatus,
    build_sidecar,
    get_sidecar_key,
    write_atomic,
    serialize,
)
from app.storage import get_storage_backend, StorageBackend
from app.config import (
    TemplateRegistry,
    PeriodFormatConfig,
    MappingRule,
    MappingMatch,
    MappingSet,
)
from app.period import format_period_from_emission_date

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")


@dataclass
class PaperlessDocument:
    """Represents a document from Paperless-ngx API."""

    id: int
    filename: str
    created_date: str  # Document creation date (used as emission_date)
    correspondent_name: str
    document_type_name: str
    tags: list[str]  # List of tag values (strings, no types)
    download_url: str


@dataclass
class ImportStats:
    """Statistics for the import run."""

    imported: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    config_updated: bool = False
    per_template_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class ResolvedMapping:
    """Resolved mapping for a document."""

    template: str | None = None
    period_format: str | None = None
    additional_fields: list[str] = field(default_factory=list)


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
        
        # Cache for fetched entities
        self._correspondent_cache: dict[int, str] = {}
        self._document_type_cache: dict[int, str] = {}
        self._tag_cache: dict[int, str] = {}

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
        """List all documents from Paperless (paginated)."""
        data = self._get("/api/documents/", params={"page": page, "page_size": page_size})
        return data.get("results", [])

    def get_document(self, doc_id: int) -> dict[str, Any]:
        """Get a single document's metadata."""
        return self._get(f"/api/documents/{doc_id}/")

    def get_correspondent_name(self, correspondent_id: int) -> str:
        """Get correspondent name from ID."""
        if not correspondent_id:
            return ""
        if correspondent_id in self._correspondent_cache:
            return self._correspondent_cache[correspondent_id]
        try:
            correspondent = self._get(f"/api/correspondents/{correspondent_id}/")
            name = correspondent.get("name", f"unknown_correspondent_{correspondent_id}")
            self._correspondent_cache[correspondent_id] = name
            return name
        except Exception as e:
            logger.warning(f"Failed to fetch correspondent {correspondent_id}: {e}")
            return f"unknown_correspondent_{correspondent_id}"

    def get_document_type_name(self, document_type_id: int) -> str:
        """Get document type name from ID."""
        if not document_type_id:
            return ""
        if document_type_id in self._document_type_cache:
            return self._document_type_cache[document_type_id]
        try:
            doc_type = self._get(f"/api/document_types/{document_type_id}/")
            name = doc_type.get("name", f"unknown_type_{document_type_id}")
            self._document_type_cache[document_type_id] = name
            return name
        except Exception as e:
            logger.warning(f"Failed to fetch document type {document_type_id}: {e}")
            return f"unknown_type_{document_type_id}"

    def get_tag_name(self, tag_id: int) -> str:
        """Get tag name from ID."""
        if not tag_id:
            return ""
        if tag_id in self._tag_cache:
            return self._tag_cache[tag_id]
        try:
            tag = self._get(f"/api/tags/{tag_id}/")
            name = tag.get("name", f"unknown_tag_{tag_id}")
            self._tag_cache[tag_id] = name
            return name
        except Exception as e:
            logger.warning(f"Failed to fetch tag {tag_id}: {e}")
            return f"unknown_tag_{tag_id}"

    def get_all_documents(self, limit: int | None = None) -> list[PaperlessDocument]:
        """Get all documents from Paperless (handles pagination)."""
        all_docs: list[PaperlessDocument] = []
        page = 1
        page_size = 100

        while True:
            docs = self.list_documents(page=page, page_size=page_size)
            if not docs:
                break

            for doc in docs:
                full_doc = self.get_document(doc["id"])

                # Extract fields
                archived_file_name = full_doc.get("archived_file_name", "")
                if not archived_file_name:
                    archived_file_name = full_doc.get("title", "") + ".pdf"
                
                correspondent_id = full_doc.get("correspondent")
                correspondent_name = self.get_correspondent_name(correspondent_id) if correspondent_id else ""
                
                document_type_id = full_doc.get("document_type")
                document_type_name = self.get_document_type_name(document_type_id) if document_type_id else ""
                
                # Get tags
                tag_ids = full_doc.get("tags", [])
                tags = []
                for tag_id in tag_ids:
                    tag_name = self.get_tag_name(tag_id)
                    if tag_name:
                        tags.append(tag_name)

                paperless_doc = PaperlessDocument(
                    id=full_doc["id"],
                    filename=archived_file_name,
                    created_date=full_doc.get("created", ""),
                    correspondent_name=correspondent_name,
                    document_type_name=document_type_name,
                    tags=tags,
                    download_url=f"{self.base_url}/api/documents/{full_doc['id']}/download/",
                )
                logger.debug(f"Retrieved document: {paperless_doc}")
                all_docs.append(paperless_doc)

                if limit and len(all_docs) >= limit:
                    logger.info(f"Reached limit of {limit} documents")
                    return all_docs

            if len(docs) < page_size:
                break
            page += 1

        logger.info(f"Retrieved {len(all_docs)} documents from Paperless")
        return all_docs

    def download_document(self, doc_id: int) -> bytes:
        """Download a document's PDF."""
        return self._get_raw(f"/api/documents/{doc_id}/download/")


class PDFProcessor:
    """Processes PDF content for import."""

    def compute_sha256(self, pdf_bytes: bytes) -> str:
        """Compute SHA256 hash of PDF content."""
        return hashlib.sha256(pdf_bytes).hexdigest()


class ConfigUpdater:
    """Handles auto-updating the configuration from Paperless data."""

    def __init__(self, config_path: str):
        """Initialize the config updater."""
        self.config_path = config_path
        self.registry = TemplateRegistry()
        self.registry.load_from_file(config_path)
        self._changes: dict[str, Any] = {}
        # Track added items in memory for immediate use
        self._added_companies: set[str] = set()
        self._added_document_types: set[str] = set()
        self._added_additional_fields: dict[str, set[str]] = {}

    def add_company(self, company: str) -> bool:
        """Add a company to config if not present."""
        if company and company not in self.registry.get_companies() and company not in self._added_companies:
            self._changes.setdefault("companies_added", []).append(company)
            self._added_companies.add(company)
            return True
        return False

    def add_document_type(self, doc_type: str) -> bool:
        """Add a document type to config if not present."""
        if doc_type and doc_type not in self.registry.get_document_types() and doc_type not in self._added_document_types:
            self._changes.setdefault("document_types_added", []).append(doc_type)
            self._added_document_types.add(doc_type)
            return True
        return False

    def add_additional_field_value(self, category: str, value: str) -> bool:
        """Add a value to an additional field category, creating category if needed."""
        if category and value:
            # Check if already in registry
            if category in self.registry.get_additional_fields():
                if value not in self.registry.get_additional_field_values(category):
                    self._changes.setdefault("additional_fields_added", {}).setdefault(category, []).append(value)
                    self._added_additional_fields.setdefault(category, set()).add(value)
                    return True
            # Check if already added in memory
            elif category in self._added_additional_fields:
                if value not in self._added_additional_fields[category]:
                    self._changes.setdefault("additional_fields_added", {}).setdefault(category, []).append(value)
                    self._added_additional_fields.setdefault(category, set()).add(value)
                    return True
            # New category and value
            else:
                self._changes.setdefault("additional_fields_added", {}).setdefault(category, []).append(value)
                self._added_additional_fields.setdefault(category, set()).add(value)
                return True
        return False

    def has_changes(self) -> bool:
        """Check if there are any pending changes."""
        return bool(self._changes)

    def _strip_jsonc_comments(self, content: str) -> str:
        """Strip // line comments and /* */ block comments from JSONC content."""
        import re
        # Remove /* */ block comments first
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        # Remove // line comments
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        # Remove leading/trailing whitespace from the result
        return content.strip()

    def save_config(self) -> None:
        """Save the updated configuration to disk."""
        if not self.has_changes():
            logger.info("No config changes to save")
            return

        logger.info("Saving updated configuration...")
        
        # Get current config data
        import json
        with open(self.config_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Strip JSONC comments before parsing
        json_content = self._strip_jsonc_comments(content)
        config_data = json.loads(json_content)
        
        # Apply changes
        if "companies_added" in self._changes:
            existing = set(config_data.get("companies", []))
            for company in self._changes["companies_added"]:
                if company not in existing:
                    config_data.setdefault("companies", []).append(company)
                    existing.add(company)
                    logger.info(f"  Added company: {company}")
        
        if "document_types_added" in self._changes:
            existing = set(config_data.get("document_types", []))
            for doc_type in self._changes["document_types_added"]:
                if doc_type not in existing:
                    config_data.setdefault("document_types", []).append(doc_type)
                    existing.add(doc_type)
                    logger.info(f"  Added document_type: {doc_type}")
        
        if "additional_fields_added" in self._changes:
            for category, values in self._changes["additional_fields_added"].items():
                config_data.setdefault("additional_fields", {}).setdefault(category, [])
                existing = set(config_data["additional_fields"][category])
                for value in values:
                    if value not in existing:
                        config_data["additional_fields"][category].append(value)
                        existing.add(value)
                        logger.info(f"  Added additional_field value: {category}={value}")
        
        # Write back to file
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        
        logger.info("Configuration saved successfully")
        
        # Reload registry with updated config
        self.registry = TemplateRegistry()
        self.registry.load_from_file(self.config_path)
        self._changes = {}


class DocumentProcessor:
    """Processes a single document through the import workflow."""

    def __init__(self, registry: TemplateRegistry, storage: StorageBackend):
        """Initialize the document processor."""
        self.registry = registry
        self.storage = storage
        self.pdf_processor = PDFProcessor()

    def _unique_target_key(self, pdf_key: str) -> str:
        """Add an incremental suffix if the PDF key or its sidecar already exists."""
        if not self.storage.object_exists(pdf_key) and not self.storage.object_exists(
            get_sidecar_key(pdf_key)
        ):
            return pdf_key

        directory, separator, filename = pdf_key.rpartition("/")
        stem, extension = os.path.splitext(filename)
        counter = 2

        while True:
            candidate_filename = f"{stem} ({counter}){extension}"
            candidate = f"{directory}{separator}{candidate_filename}"
            if not self.storage.object_exists(candidate) and not self.storage.object_exists(
                get_sidecar_key(candidate)
            ):
                return candidate
            counter += 1

    def resolve_mapping(self, company: str, document_type: str) -> ResolvedMapping:
        """Resolve the mapping for a document."""
        result = ResolvedMapping()
        
        # Get all mapping rules
        for rule in self.registry.get_mapping_rules():
            if rule.matches(company, document_type):
                if rule.set.template is not None:
                    result.template = rule.set.template
                if rule.set.period_format is not None:
                    result.period_format = rule.set.period_format
                if rule.set.additional_fields:
                    result.additional_fields = rule.set.additional_fields.copy()
        
        return result

    def match_tags_to_fields(
        self, 
        tags: list[str], 
        expected_categories: list[str]
    ) -> tuple[dict[str, str], list[str]]:
        """Match Paperless tags to additional field categories.
        
        Strict validation: each tag must match exactly one category.
        If any tag doesn't match or matches multiple categories, return errors.
        
        Args:
            tags: List of tag values from Paperless
            expected_categories: List of additional field categories from mapping
        
        Returns:
            Tuple of (assigned_fields, errors)
        """
        assigned_fields: dict[str, str] = {}
        errors: list[str] = []

        if not expected_categories:
            # No additional fields expected - all tags are just metadata
            if tags:
                assigned_fields["tags"] = ", ".join(tags)
            return assigned_fields, errors
        
        if len(expected_categories) == 1:
            # Single category - all tags belong to it
            category = expected_categories[0]
            # Values are already in config (added by ConfigUpdater)
            for tag in tags:
                assigned_fields[category] = tag
            return assigned_fields, errors
        
        # Multiple categories - need strict matching
        for tag in tags:
            matching_categories = []
            for category in expected_categories:
                values = self.registry.get_additional_field_values(category)
                if tag in values:
                    matching_categories.append(category)
            
            if len(matching_categories) == 0:
                errors.append(f"Tag '{tag}' does not match any expected category")
            elif len(matching_categories) > 1:
                errors.append(f"Tag '{tag}' matches multiple categories: {matching_categories}")
            else:
                assigned_fields[matching_categories[0]] = tag
        
        return assigned_fields, errors

    def process_document(
        self,
        doc: PaperlessDocument,
        config_updater: ConfigUpdater,
    ) -> tuple[str, Sidecar | None, list[str]]:
        """Process a single document.
        
        Args:
            doc: The Paperless document
            config_updater: For auto-updating config
        
        Returns:
            Tuple of (target_key, sidecar, errors)
            - target_key: Either files/{final_key}.pdf or pending/{paperless_id}.pdf
            - sidecar: Sidecar metadata (or None if failed)
            - errors: List of error messages
        """
        errors: list[str] = []
        
        # Step 1: Auto-update config with company and document type
        company = doc.correspondent_name
        doc_type = doc.document_type_name
        
        # Add to config (this tracks changes in memory)
        config_updater.add_company(company)
        config_updater.add_document_type(doc_type)
        
        # Reload registry from config_updater to get the updated state
        self.registry = config_updater.registry
        
        # Validate - check both registry and in-memory additions
        is_valid_company = (company in self.registry.get_companies() or 
                           company in config_updater._added_companies)
        is_valid_doc_type = (doc_type in self.registry.get_document_types() or 
                            doc_type in config_updater._added_document_types)
        
        if not is_valid_company:
            errors.append(f"Company '{company}' could not be added to config")
        if not is_valid_doc_type:
            errors.append(f"Document type '{doc_type}' could not be added to config")
        
        if errors:
            return self._route_to_pending(doc, errors)
        
        # Step 2: Resolve mapping
        resolved = self.resolve_mapping(company, doc_type)
        
        if not resolved.template:
            errors.append(f"No template resolved for company='{company}', document_type='{doc_type}'")
        
        if not resolved.period_format:
            errors.append(f"No period_format resolved for company='{company}', document_type='{doc_type}'")
        
        if errors:
            return self._route_to_pending(doc, errors)
        
        # Step 3: Auto-update additional fields config
        for category in resolved.additional_fields:
            for tag in doc.tags:
                config_updater.add_additional_field_value(category, tag)
        
        # Reload registry after potential changes
        self.registry = config_updater.registry
        
        # Step 4: Match tags to additional field categories
        # First, ensure all expected categories exist and have values added
        for category in resolved.additional_fields:
            for tag in doc.tags:
                config_updater.add_additional_field_value(category, tag)
        
        # Reload registry after potential changes
        self.registry = config_updater.registry
        
        # Now match tags to categories
        assigned_fields, tag_errors = self.match_tags_to_fields(
            doc.tags, 
            resolved.additional_fields
        )
        errors.extend(tag_errors)
        
        if resolved.additional_fields and not assigned_fields:
            errors.append("The tags " + ", ".join(doc.tags) + " did not match expected additional fields: " + ", ".join(resolved.additional_fields))
        
        if errors:
            return self._route_to_pending(doc, errors)
        
        logger.debug(f"Resolved mapping for document {doc.id}: template={resolved.template}, period_format={resolved.period_format}, additional_fields={assigned_fields}")
        
        # Step 5: Calculate period
        try:
            period_format_config = self.registry.get_period_format_config(resolved.period_format)
            if not period_format_config:
                errors.append(f"Period format '{resolved.period_format}' not found")
                return self._route_to_pending(doc, errors)
            
            period = format_period_from_emission_date(
                doc.created_date,
                period_format_config.to_dict()
            )

            logger.debug(f"Calculated period '{period}' for document {doc.id} using format '{resolved.period_format}'")
        except Exception as e:
            errors.append(f"Failed to calculate period: {e}")
            logger.exception(f"Error calculating period for document {doc.id}")
            return self._route_to_pending(doc, errors)
        
        # Step 6: Generate final key
        template_name = resolved.template
        key_pattern = self.registry.get_key_pattern(template_name)
        
        if not key_pattern:
            errors.append(f"No key_pattern found for template '{template_name}'")
            return self._route_to_pending(doc, errors)
        
        try:
            final_key = key_pattern.format(
                emitting_company=company,
                document_type=doc_type,
                emission_date=doc.created_date,
                period=period,
                original_filename=doc.filename,
                additional_fields=", ".join(assigned_fields.values()) if assigned_fields else "",
                **assigned_fields
            )
        except Exception as e:
            errors.append(f"Failed to format key pattern: {e}")
            return self._route_to_pending(doc, errors)
        
        # Step 7: Build sidecar for files/
        pdf_key = f"files/{final_key}"
        if not pdf_key.lower().endswith(".pdf"):
            pdf_key += ".pdf"
        pdf_key = self._unique_target_key(pdf_key)
        sha256 = ""  # Will be computed when writing
        
        sidecar = build_sidecar(
            sha256=sha256,
            original_key=doc.filename,
            current_key=pdf_key,
            status=SidecarStatus.PROCESSED,
            template_name=template_name,
            period_format=resolved.period_format,
            emission_date=doc.created_date,
            period=period,
            emitting_company=company,
            document_type=doc_type,
            confidence=1.0,
            optional_fields=assigned_fields,
            extracted_text="",
        )
        
        # Add import event
        sidecar.add_event(
            "imported",
            {
                "source": "paperless-ngx",
                "document_id": doc.id,
                "paperless_filename": doc.filename,
            },
        )
        
        return pdf_key, sidecar, []

    def _route_to_pending(
        self, 
        doc: PaperlessDocument, 
        errors: list[str]
    ) -> tuple[str, Sidecar | None, list[str]]:
        """Route document to pending/ with error sidecar."""
        pdf_key = f"pending/{doc.id}.pdf"
        
        optional_fields = {"tags": doc.tags} if doc.tags else {}
        
        # Build sidecar
        sidecar = build_sidecar(
            sha256="",
            original_key=doc.filename,
            current_key=pdf_key,
            status=SidecarStatus.NOT_PROCESSED,
            template_name="",
            period_format="",
            emission_date=doc.created_date,
            period="",
            emitting_company=doc.correspondent_name,
            document_type=doc.document_type_name,
            confidence=0.0,
            optional_fields=optional_fields,
            extracted_text="",
        )
        
        # Add import event with errors
        sidecar.add_event(
            "imported",
            {
                "source": "paperless-ngx",
                "document_id": doc.id,
                "paperless_filename": doc.filename,
                "errors": errors,
            },
        )
        
        return pdf_key, sidecar, errors


class Importer:
    """Main import class that orchestrates the import process."""

    def __init__(
        self,
        paperless_url: str,
        paperless_token: str,
        config_path: str,
        storage_backend: str = "s3",
        s3_endpoint: str | None = None,
        s3_access_key: str | None = None,
        s3_secret_key: str | None = None,
        s3_bucket: str | None = None,
        file_base_dir: str | None = None,
        dry_run: bool = False,
        limit: int | None = None,
    ):
        """Initialize the importer."""
        self.paperless_url = paperless_url
        self.paperless_token = paperless_token
        self.config_path = config_path
        self.storage_backend_type = storage_backend
        self.s3_endpoint = s3_endpoint
        self.s3_access_key = s3_access_key
        self.s3_secret_key = s3_secret_key
        self.s3_bucket = s3_bucket
        self.file_base_dir = file_base_dir
        self.dry_run = dry_run
        self.limit = limit

        # Initialize clients
        self.paperless_client = PaperlessClient(paperless_url, paperless_token)
        self.storage: StorageBackend | None = None
        self._init_storage()

        # Initialize config updater
        self.config_updater = ConfigUpdater(config_path)

        # Document processor
        self.doc_processor = DocumentProcessor(
            self.config_updater.registry, 
            self.storage
        )

        # Stats
        self.stats = ImportStats()

    def _init_storage(self) -> None:
        """Initialize the storage backend."""
        if self.storage_backend_type == "s3":
            if not all([self.s3_endpoint, self.s3_access_key, self.s3_secret_key, self.s3_bucket]):
                raise ValueError(
                    "S3 storage requires: s3_endpoint, s3_access_key, s3_secret_key, s3_bucket"
                )
            self.storage = get_storage_backend(
                "s3",
                endpoint_url=self.s3_endpoint,
                aws_access_key_id=self.s3_access_key,
                aws_secret_access_key=self.s3_secret_key,
                bucket_name=self.s3_bucket,
            )
            logger.info(f"Using S3 storage backend: {self.s3_endpoint}/{self.s3_bucket}")
        elif self.storage_backend_type == "file":
            if not self.file_base_dir:
                raise ValueError("File storage requires: file_base_dir")
            self.storage = get_storage_backend("file", base_dir=self.file_base_dir)
            logger.info(f"Using File storage backend: {self.file_base_dir}")
        else:
            raise ValueError(f"Unknown storage backend: {self.storage_backend_type}")

    def clear_directories(self) -> None:
        """Clear files/ and pending/ directories at start of run."""
        if self.storage is None:
            raise RuntimeError("Storage not initialized")
        
        logger.info("Clearing files/ and pending/ directories...")
        
        # List and delete all objects under files/
        files_objects = self.storage.list_objects("files/")
        for obj_key in files_objects:
            self.storage.delete_object(obj_key)
            logger.debug(f"Deleted {obj_key}")
        
        # List and delete all objects under pending/
        pending_objects = self.storage.list_objects("pending/")
        for obj_key in pending_objects:
            self.storage.delete_object(obj_key)
            logger.debug(f"Deleted {obj_key}")
        
        logger.info(f"Cleared {len(files_objects)} files/ and {len(pending_objects)} pending/ objects")

    def import_document(self, doc: PaperlessDocument) -> bool:
        """Import a single document.

        Args:
            doc: The Paperless document to import.

        Returns:
            True if import succeeded, False otherwise.
        """
        if self.storage is None:
            raise RuntimeError("Storage not initialized")

        logger.info(f"Processing document {doc.id}: {doc.filename}")

        try:
            # Download PDF
            pdf_bytes = self.paperless_client.download_document(doc.id)
            sha256 = self.doc_processor.pdf_processor.compute_sha256(pdf_bytes)

            # Process document
            target_key, sidecar, errors = self.doc_processor.process_document(
                doc, self.config_updater
            )

            # Dry run: just log what would happen
            if self.dry_run:
                if errors:
                    logger.info(f"[DRY RUN] Would route to pending: {target_key}")
                    logger.info(f"[DRY RUN]   Errors: {errors}")
                else:
                    logger.info(f"[DRY RUN] Would import: {target_key}")
                self.stats.imported += 1
                return True

            # Write PDF
            self.storage.put_object(target_key, pdf_bytes)
            logger.debug(f"Uploaded PDF to {target_key}")

            # Update sidecar
            sidecar.sha256 = sha256
            sidecar.extracted_text = extract_first_page_text(pdf_bytes)

            # Write sidecar
            sidecar_key = get_sidecar_key(target_key)
            sidecar_json = serialize(sidecar)
            self.storage.put_object(sidecar_key, sidecar_json.encode("utf-8"))
            logger.debug(f"Wrote sidecar to {sidecar_key}")

            # Update stats
            if errors:
                self.stats.pending += 1
            else:
                self.stats.imported += 1
                self.stats.per_template_counts[sidecar.template_name] = (
                    self.stats.per_template_counts.get(sidecar.template_name, 0) + 1
                )

            logger.info(f"Successfully {'imported' if not errors else 'routed to pending'} document {doc.id}")
            return True

        except Exception as e:
            error_msg = f"Failed to import document {doc.id}: {e}"
            logger.error(error_msg)
            logger.debug("Exception details:", exc_info=True)
            self.stats.failed += 1
            self.stats.errors.append(error_msg)
            return False

    def run(self) -> bool:
        """Run the import process.

        Returns:
            True if import succeeded (no failures), False otherwise.
        """
        if self.storage is None:
            raise RuntimeError("Storage not initialized")

        logger.info("Starting Paperless-ngx import...")
        logger.info(f"Storage backend: {self.storage_backend_type}")
        logger.info(f"Config path: {self.config_path}")

        # Clear directories at start
        self.clear_directories()

        # Get all documents from Paperless
        logger.info("Fetching documents from Paperless...")
        docs = self.paperless_client.get_all_documents(self.limit)

        if not docs:
            logger.info("No documents found in Paperless")
            # Save config if changes were made
            if self.config_updater.has_changes():
                self.config_updater.save_config()
            return True

        logger.info(f"Found {len(docs)} documents to import")

        # Import each document
        for doc in docs:
            self.import_document(doc)

        # Save config at end if changes were made
        if self.config_updater.has_changes():
            self.config_updater.save_config()
            self.stats.config_updated = True

        # Print summary
        self.print_summary()

        # Return True if no failures
        return self.stats.failed == 0

    def print_summary(self) -> None:
        """Print import summary."""
        print("\n" + "=" * 60)
        print("IMPORT SUMMARY")
        print("=" * 60)
        print(f"Storage backend: {self.storage_backend_type}")
        print(f"Config path: {self.config_path}")
        print(f"Config updated: {'Yes' if self.stats.config_updated else 'No'}")
        print(f"Imported to files/:   {self.stats.imported}")
        print(f"Routed to pending/:   {self.stats.pending}")
        print(f"Skipped:              {self.stats.skipped}")
        print(f"Failed:               {self.stats.failed}")
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
        description="Import documents from Paperless-ngx with config-driven processing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables for S3 storage:
  S3_ENDPOINT_URL        Garage S3 endpoint URL
  S3_ACCESS_KEY_ID       S3 access key
  S3_SECRET_ACCESS_KEY   S3 secret key
  S3_BUCKET_NAME         S3 bucket name

Environment variables for Paperless:
  PAPERLESS_URL          Paperless-ngx API URL
  PAPERLESS_TOKEN        Paperless-ngx API token

For file storage, use --storage-backend file --file-base-dir /path/to/dir
        """,
    )

    # Paperless configuration
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

    # Config file
    parser.add_argument(
        "--config-path",
        help="Path to config.jsonc file",
        default="./config.jsonc",
    )

    # Storage backend configuration
    parser.add_argument(
        "--storage-backend",
        choices=["s3", "file"],
        default="s3",
        help="Storage backend type: 's3' for Garage S3, 'file' for local filesystem (default: s3)",
    )

    # S3 configuration
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

    # File storage configuration
    parser.add_argument(
        "--file-base-dir",
        help="Base directory for file storage backend",
        default=os.environ.get("FILE_BASE_DIR", "./import_storage"),
    )

    # Import options
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
    """Validate that required environment variables or arguments are set."""
    missing = []

    # Paperless is always required
    if not args.paperless_url:
        missing.append("Paperless URL (--paperless-url or PAPERLESS_URL)")
    if not args.paperless_token:
        missing.append("Paperless token (--paperless-token or PAPERLESS_TOKEN)")

    # Storage backend specific requirements
    if args.storage_backend == "s3":
        if not args.s3_endpoint:
            missing.append("S3 endpoint (--s3-endpoint or S3_ENDPOINT_URL)")
        if not args.s3_access_key:
            missing.append("S3 access key (--s3-access-key or S3_ACCESS_KEY_ID)")
        if not args.s3_secret_key:
            missing.append("S3 secret key (--s3-secret-key or S3_SECRET_ACCESS_KEY)")
        if not args.s3_bucket:
            missing.append("S3 bucket (--s3-bucket or S3_BUCKET_NAME)")
    elif args.storage_backend == "file":
        if not args.file_base_dir:
            missing.append("File base dir (--file-base-dir or FILE_BASE_DIR)")

    if missing:
        raise ValueError(f"Missing required values:\n  " + "\n  ".join(missing))


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
            config_path=args.config_path,
            storage_backend=args.storage_backend,
            s3_endpoint=args.s3_endpoint,
            s3_access_key=args.s3_access_key,
            s3_secret_key=args.s3_secret_key,
            s3_bucket=args.s3_bucket,
            file_base_dir=args.file_base_dir,
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


# Add PDFProcessor class for completeness
class PDFProcessor:
    """Processes PDF content for import."""

    def compute_sha256(self, pdf_bytes: bytes) -> str:
        """Compute SHA256 hash of PDF content."""
        return hashlib.sha256(pdf_bytes).hexdigest()


if __name__ == "__main__":
    main()
