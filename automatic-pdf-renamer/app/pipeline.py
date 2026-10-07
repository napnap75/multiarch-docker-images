"""Processing pipeline module for Automatic PDF Renamer.

Implements the document processing workflow:
1. Load the first page and extract text using PyMuPDF
2. Find the emission date using regex patterns from config (ordered list, first match wins)
3. Find company and document_type using ML, validated against predefined lists in config
   - Unknown values must route to review
4. Look up the (company, document_type) tuple in mappings to get template, period_format, and additional_fields
   - Mappings use match/set structure: match criteria (company, document_type, both, or neither)
   - Rules are processed in order, later matches override earlier ones
   - Empty match {} matches all documents (default rule)
5. Apply period_format (with granularity, format template, and offset) to calculate the human-readable period string
6. Apply template key_pattern to generate the final filename
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import (
    TemplateRegistry,
    MappingRule,
    PeriodFormatConfig,
)
from .extractor import extract_first_page_text, is_text_empty, TextExtractionError
from .period import (
    format_period_from_emission_date,
)
from .sidecar import Sidecar, SidecarStatus, build_sidecar

logger = logging.getLogger(__name__)


@dataclass
class ExtractedDate:
    """Represents an extracted date from text."""

    date_str: str
    datetime_obj: datetime | None
    pattern_index: int = -1


@dataclass
class ProcessingResult:
    """Result of processing a document."""

    success: bool
    sidecar: Sidecar | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    emission_date: str = ""
    company: str = ""
    document_type: str = ""
    template_name: str = ""
    period_format: str = ""
    period: str = ""
    status: str = SidecarStatus.NOT_PROCESSED.value


class DocumentProcessor:
    """Processes PDF documents through the pipeline."""

    def __init__(
        self,
        registry: TemplateRegistry,
        min_text_length: int = 10,
    ):
        """Initialize the document processor.

        Args:
            registry: TemplateRegistry with configuration.
            min_text_length: Minimum text length for valid extraction.
        """
        self.registry = registry
        self.min_text_length = min_text_length

    def process(
        self,
        pdf_bytes: bytes,
        original_key: str,
        current_key: str,
        sha256: str,
        extracted_text: str | None = None,
    ) -> ProcessingResult:
        """Process a PDF document through the full pipeline.

        Args:
            pdf_bytes: The PDF content as bytes.
            original_key: The original S3 key of the file.
            current_key: The current S3 key of the file.
            sha256: The SHA256 hash of the PDF.
            extracted_text: Optional pre-extracted text (for caching).

        Returns:
            ProcessingResult with the processing outcome.
        """
        result = ProcessingResult(success=False)

        try:
            # Step 1: Extract first page text
            if extracted_text is None:
                extracted_text = self._extract_text(pdf_bytes)
            
            if extracted_text is None:
                result.errors.append("Failed to extract text from PDF")
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            # Step 2: Find emission date using regex patterns from config
            emission_date, date_error = self._extract_emission_date(extracted_text)
            if date_error:
                result.errors.append(date_error)
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            result.emission_date = emission_date

            # Step 3: Find company and document_type using ML
            company, doc_type, ml_errors = self._extract_company_and_type(extracted_text)
            
            if ml_errors:
                result.errors.extend(ml_errors)
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            result.company = company
            result.document_type = doc_type

            # Validate company and document_type against config lists
            # Strict validation: unknown values route to review
            if not self.registry.is_valid_company(company):
                result.errors.append(
                    f"Unknown company '{company}'. "
                    f"Valid companies: {', '.join(self.registry.get_companies()[:10])}..."
                )
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            if not self.registry.is_valid_document_type(doc_type):
                result.errors.append(
                    f"Unknown document type '{doc_type}'. "
                    f"Valid types: {', '.join(self.registry.get_document_types()[:10])}..."
                )
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            # Step 4: Resolve mapping using match/set rules
            # Rules are processed in order, later matches override earlier ones
            resolved = self.registry.resolve_mapping(company, doc_type)
            
            if not resolved:
                result.errors.append(
                    f"No mapping found for company='{company}', "
                    f"document_type='{doc_type}'. Routing to review."
                )
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result
            
            # Extract resolved fields
            template_name = resolved.get("template")
            period_format = resolved.get("period_format")
            additional_field_groups = resolved.get("additional_fields", [])
            
            if not template_name:
                result.errors.append(
                    f"No template resolved for company='{company}', "
                    f"document_type='{doc_type}'. Routing to review."
                )
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result
            
            result.template_name = template_name
            result.period_format = period_format

            # Step 4b: Extract additional fields from additional_fields lists
            optional_fields = {}
            for group_name in additional_field_groups:
                field_value, field_error = self._extract_field_value(
                    extracted_text, group_name
                )
                if field_error:
                    result.errors.append(field_error)
                    result.status = SidecarStatus.NOT_PROCESSED.value
                    return result
                if field_value:
                    optional_fields[group_name] = field_value

            # Step 5: Calculate period using period_format configuration
            if not period_format:
                result.errors.append(
                    f"No period_format resolved for company='{company}', "
                    f"document_type='{doc_type}'. Routing to review."
                )
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result
            
            period_format_config = self.registry.get_period_format_config(period_format)
            if period_format_config is None:
                result.errors.append(
                    f"Period format '{period_format}' not found in configuration"
                )
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result
            
            try:
                period = format_period_from_emission_date(
                    emission_date,
                    period_format_config.to_dict()
                )
                result.period = period
            except Exception as e:
                result.errors.append(f"Failed to compute period: {e}")
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            # Step 6: Generate the final key using template's key_pattern
            key_pattern = self.registry.get_key_pattern(template_name)
            if key_pattern:
                try:
                    final_key = self._format_key_pattern(
                        key_pattern,
                        company=company,
                        document_type=doc_type,
                        emission_date=emission_date,
                        period=period,
                        template_name=template_name,
                        period_format=period_format,
                        emitting_company=company,
                        original_filename=original_key.split("/")[-1],
                        **optional_fields,
                    )
                except Exception as e:
                    result.errors.append(f"Failed to format key pattern: {e}")
                    result.status = SidecarStatus.NOT_PROCESSED.value
                    return result
            else:
                # Fallback: use a simple pattern
                final_key = f"{template_name}/{period}/{company}/{doc_type}/{emission_date}_{original_key.split('/')[-1]}"

            # Step 7: Build the sidecar
            confidence = 1.0  # Will be set by ML classifier
            sidecar = build_sidecar(
                sha256=sha256,
                original_key=original_key,
                current_key=final_key,
                status=SidecarStatus.PROCESSED,
                template_name=template_name,
                period_format=period_format,
                emission_date=emission_date,
                period=period,
                emitting_company=company,
                document_type=doc_type,
                confidence=confidence,
                optional_fields=optional_fields,
                extracted_text=extracted_text,
            )

            # Add processing event
            sidecar.add_event(
                "classified",
                {
                    "template": template_name,
                    "company": company,
                    "document_type": doc_type,
                    "emission_date": emission_date,
                    "period": period,
                    "confidence": confidence,
                    "period_format": period_format,
                    "optional_fields": optional_fields,
                },
            )

            result.success = True
            result.sidecar = sidecar
            result.status = SidecarStatus.PROCESSED.value

        except Exception as e:
            result.errors.append(f"Unexpected error during processing: {e}")
            result.status = SidecarStatus.NOT_PROCESSED.value
            logger.exception(f"Error processing document {original_key}")

        return result

    def _extract_text(self, pdf_bytes: bytes) -> str | None:
        """Extract first page text from PDF.

        Args:
            pdf_bytes: The PDF content as bytes.

        Returns:
            Extracted text, or None if extraction fails.
        """
        try:
            text = extract_first_page_text(pdf_bytes)
            if is_text_empty(text, self.min_text_length):
                return None
            return text
        except TextExtractionError as e:
            logger.error(f"Text extraction failed: {e}")
            return None

    def _extract_emission_date(self, text: str) -> tuple[str, str | None]:
        """Extract emission date from text using regex patterns from config.

        Uses ordered list of regex patterns. Tries them in order and stops
        at first match.

        Args:
            text: The extracted text from the PDF.

        Returns:
            Tuple of (date_string, error_message).
            If successful, error_message is None.
        """
        patterns = self.registry.get_date_extraction_compiled()
        
        if not patterns:
            # Fallback: try to find any date-like pattern
            return self._extract_emission_date_fallback(text)

        # Try each pattern in order, first match wins
        for idx, pattern in enumerate(patterns):
            matches = pattern.finditer(text)
            for match in matches:
                # Get the first date-like match
                groups = match.groups()
                date_str = self._reconstruct_date(groups)
                if date_str:
                    # Validate it looks like a date
                    if self._is_valid_date_string(date_str):
                        return (date_str, None)

        # If no patterns matched, try fallback
        return self._extract_emission_date_fallback(text)

    def _reconstruct_date(self, groups: tuple[str, ...]) -> str | None:
        """Reconstruct a date string from regex match groups.

        Args:
            groups: The regex match groups.

        Returns:
            Reconstructed date string, or None if cannot reconstruct.
        """
        # Filter out None and empty strings
        non_empty = [g for g in groups if g]
        if non_empty:
            return " ".join(non_empty) if len(non_empty) > 1 else non_empty[0]
        return None

    def _is_valid_date_string(self, date_str: str) -> bool:
        """Check if a string looks like a valid date.

        Args:
            date_str: The string to check.

        Returns:
            True if it looks like a valid date.
        """
        # Simple heuristic: contains digits and possibly separators
        if not date_str:
            return False
        
        # Check for common date patterns
        date_patterns = [
            r"\d{4}-\d{2}-\d{2}",  # YYYY-MM-DD
            r"\d{2}/\d{2}/\d{4}",  # DD/MM/YYYY
            r"\d{2}-\d{2}-\d{4}",  # DD-MM-YYYY
            r"\d{4}",  # YYYY
            r"\d{4}/\d{2}",  # YYYY/MM
            r"\d{2}\s+[a-zA-Z]+\s+\d{4}",  # DD Month YYYY
        ]
        
        for p in date_patterns:
            if re.match(p, date_str.strip()):
                return True
        
        return False

    def _extract_emission_date_fallback(self, text: str) -> tuple[str, str | None]:
        """Fallback method for extracting emission date.

        Args:
            text: The extracted text.

        Returns:
            Tuple of (date_string, error_message).
        """
        # Try common date patterns
        fallback_patterns = [
            r"(\d{4}-\d{2}-\d{2})",  # YYYY-MM-DD
            r"(\d{2}/\d{2}/\d{4})",  # DD/MM/YYYY
            r"(\d{2}-\d{2}-\d{4})",  # DD-MM-YYYY
            r"(\d{4})",  # YYYY
        ]
        
        for pattern in fallback_patterns:
            matches = re.finditer(pattern, text)
            for match in matches:
                date_str = match.group(1)
                if self._is_valid_date_string(date_str):
                    return (date_str, None)
        
        return ("", "Could not find emission date in text")

    def _extract_company_and_type(self, text: str) -> tuple[str, str, list[str]]:
        """Extract company and document type from text using ML.

        This is a placeholder for the actual ML implementation.
        For now, it tries to match company and document type names from config.

        Args:
            text: The extracted text.

        Returns:
            Tuple of (company, document_type, errors).
        """
        errors = []
        
        # Try to find company names from the config
        companies = self.registry.get_companies()
        for company in companies:
            if company.lower() in text.lower():
                # Try to find document type
                doc_types = self.registry.get_document_types()
                for doc_type in doc_types:
                    if doc_type.lower() in text.lower():
                        return (company, doc_type, errors)
                
                # If no document type found, use first one as default
                if doc_types:
                    return (company, doc_types[0], [f"Could not determine document type, using default: {doc_types[0]}"])
        
        # If no company found, return unknown
        return ("", "", ["Could not determine company and document type"])

    def _extract_field_value(self, text: str, group_name: str) -> tuple[str, str | None]:
        """Extract a field value from text using ML from predefined list.

        For each additional field group in the mapping, the value should be found
        using ML from the predefined list of possible values for that group.
        
        Strict validation: if value not found in predefined list, route to review.

        Args:
            text: The extracted text.
            group_name: The name of the additional field group.

        Returns:
            Tuple of (field_value, error_message).
            If successful, error_message is None.
            If no value can be determined, error_message describes the issue.
        """
        # Get the predefined list of values for this additional field group
        possible_values = self.registry.get_additional_field_values(group_name)
        
        if not possible_values:
            return ("", f"No predefined values for additional field group '{group_name}'")
        
        # Try to find a value from the predefined list in the text
        # This is a placeholder for ML-based extraction
        # For now, we'll do simple string matching
        text_lower = text.lower()
        for value in possible_values:
            if value.lower() in text_lower:
                return (value, None)
        
        # Strict validation: if no value found in predefined list, route to review
        return ("", f"Could not determine value for additional field '{group_name}' from predefined list")

    def _format_key_pattern(
        self,
        pattern: str,
        **kwargs: Any,
    ) -> str:
        """Format a key pattern with the provided values.

        Args:
            pattern: The key pattern with placeholders.
            **kwargs: Values to substitute into the pattern.

        Returns:
            Formatted key string.
        """
        try:
            return pattern.format(**kwargs)
        except KeyError as e:
            # If a placeholder is missing, use a default value
            missing = str(e).strip("'")
            kwargs[missing] = "unknown"
            return pattern.format(**kwargs)


class DateExtractor:
    """Specialized class for extracting dates from text using config patterns."""

    def __init__(self, registry: TemplateRegistry):
        """Initialize the date extractor.

        Args:
            registry: TemplateRegistry with date extraction patterns.
        """
        self.registry = registry
        self._patterns = registry.get_date_extraction_compiled()

    def extract_first_date(self, text: str) -> str | None:
        """Extract the first date found in text using configured patterns.

        Args:
            text: The text to search.

        Returns:
            The first date string found, or None.
        """
        if not self._patterns:
            return None

        for pattern in self._patterns:
            match = pattern.search(text)
            if match:
                groups = match.groups()
                date_str = " ".join([g for g in groups if g])
                if date_str.strip():
                    return date_str.strip()

        return None

    def extract_all_dates(self, text: str) -> list[str]:
        """Extract all dates found in text using configured patterns.

        Args:
            text: The text to search.

        Returns:
            List of date strings found.
        """
        dates = []
        if not self._patterns:
            return dates

        for pattern in self._patterns:
            matches = pattern.finditer(text)
            for match in matches:
                groups = match.groups()
                date_str = " ".join([g for g in groups if g])
                if date_str.strip() and date_str.strip() not in dates:
                    dates.append(date_str.strip())

        return dates


def create_processor(registry: TemplateRegistry) -> DocumentProcessor:
    """Factory function to create a DocumentProcessor.

    Args:
        registry: TemplateRegistry with configuration.

    Returns:
        DocumentProcessor instance.
    """
    return DocumentProcessor(registry)
