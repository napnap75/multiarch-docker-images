"""Processing pipeline module for Automatic PDF Renamer.

Implements the document processing workflow:
1. Extract first page text
2. Find emission date using regex patterns from config
3. Find company and document_type using ML, validated against config lists
4. Look up (company, document_type) in mappings to get template and period rule
5. If no mapping found -> route to review
6. Apply period rule to calculate period from emission date
7. Apply template key_pattern to generate filename
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import (
    TemplateRegistry,
    CompanyTypeMapping,
    DateExtractionPattern,
)
from .extractor import extract_first_page_text, is_text_empty, TextExtractionError
from .period import (
    PeriodGranularity,
    PeriodOffset,
    PeriodRule,
    compute_period_from_date,
    format_month_year,
    format_quarter,
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

            # Step 2: Find emission date using regex patterns
            emission_date, date_error = self._extract_emission_date(extracted_text)
            if date_error:
                result.errors.append(date_error)
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            result.emission_date = emission_date

            # Step 3: Find company and document_type using ML
            # For now, we'll use placeholder ML functions
            # The actual ML implementation will be added later
            company, doc_type, ml_errors = self._extract_company_and_type(extracted_text)
            
            if ml_errors:
                result.errors.extend(ml_errors)
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            result.company = company
            result.document_type = doc_type

            # Validate company and document_type against config lists
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

            # Step 4: Look up (company, document_type) in mappings
            mapping = self.registry.get_mapping(company, doc_type)
            if mapping is None:
                result.errors.append(
                    f"No mapping found for company='{company}', "
                    f"document_type='{doc_type}'. Routing to review."
                )
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            result.template_name = mapping.template

            # Step 5: Calculate period from emission date and mapping's period rule
            period_rule = PeriodRule(
                granularity=PeriodGranularity(mapping.period.granularity),
                offset=PeriodOffset(mapping.period.offset),
            )
            
            try:
                period = compute_period_from_date(emission_date, period_rule)
                result.period = period
            except Exception as e:
                result.errors.append(f"Failed to compute period: {e}")
                result.status = SidecarStatus.NOT_PROCESSED.value
                return result

            # Step 6: Generate the final key using template's key_pattern
            key_pattern = self.registry.get_key_pattern(mapping.template)
            if key_pattern:
                try:
                    final_key = self._format_key_pattern(
                        key_pattern,
                        company=company,
                        document_type=doc_type,
                        emission_date=emission_date,
                        period=period,
                        template_name=mapping.template,
                        original_filename=original_key.split("/")[-1],
                    )
                except Exception as e:
                    result.errors.append(f"Failed to format key pattern: {e}")
                    result.status = SidecarStatus.NOT_PROCESSED.value
                    return result
            else:
                # Fallback: use a simple pattern
                final_key = f"{mapping.template}/{period}/{company}/{doc_type}/{emission_date}_{original_key.split('/')[-1]}"

            # Step 7: Build the sidecar
            confidence = 1.0  # Will be set by ML classifier
            sidecar = build_sidecar(
                sha256=sha256,
                original_key=original_key,
                current_key=final_key,
                status=SidecarStatus.PROCESSED,
                template_name=mapping.template,
                emission_date=emission_date,
                period=period,
                emitting_company=company,
                document_type=doc_type,
                confidence=confidence,
                optional_fields={},
                extracted_text=extracted_text,
            )

            # Add processing event
            sidecar.add_event(
                "classified",
                {
                    "template": mapping.template,
                    "company": company,
                    "document_type": doc_type,
                    "emission_date": emission_date,
                    "period": period,
                    "confidence": confidence,
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

        Args:
            text: The extracted text from the PDF.

        Returns:
            Tuple of (date_string, error_message).
            If successful, error_message is None.
        """
        patterns = self.registry.get_date_extraction_patterns()
        
        if not patterns:
            # Fallback: try to find any date-like pattern
            return self._extract_emission_date_fallback(text)

        # Try each pattern in order
        for idx, pattern in enumerate(patterns):
            matches = pattern.compiled.finditer(text)
            for match in matches:
                # Get the first date-like match
                groups = match.groups()
                date_str = self._reconstruct_date(groups, pattern.pattern)
                if date_str:
                    # Validate it looks like a date
                    if self._is_valid_date_string(date_str):
                        return (date_str, None)

        # If no patterns matched, try fallback
        return self._extract_emission_date_fallback(text)

    def _reconstruct_date(self, groups: tuple[str, ...], pattern: str) -> str | None:
        """Reconstruct a date string from regex match groups.

        Args:
            groups: The regex match groups.
            pattern: The original pattern string.

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
        For now, it returns dummy values.

        Args:
            text: The extracted text.

        Returns:
            Tuple of (company, document_type, errors).
        """
        # TODO: Implement actual ML-based extraction
        # For now, return dummy values
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
        self._patterns = registry.get_date_extraction_patterns()

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
            match = pattern.compiled.search(text)
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
            matches = pattern.compiled.finditer(text)
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
