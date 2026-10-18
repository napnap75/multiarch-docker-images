"""Processing pipeline module for Automatic PDF Renamer.

Implements the document processing workflow:
1. Extract text from every page using PyMuPDF
2. Extract emission date in parallel with the initial text-derived fields
3. Identify emitting company and document type from text, validated against configured lists
   - Unknown values must route to review
4. Resolve applicable additional-field groups from (company, document_type), then extract their values from text
   - Mappings use match/set structure: match criteria (company, document_type, both, or neither)
   - Rules are processed in order, later matches override earlier ones
   - Empty match {} matches all documents (default rule)
5. After first-stage extraction, resolve template and period_format from configuration
6. Calculate period from emission_date and period_format, then render the final storage key
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
from .extractor import extract_document_text, is_text_empty, TextExtractionError
from .period import (
    format_period_from_emission_date,
)
from .sidecar import Sidecar, SidecarStatus, build_sidecar
from .ml import LocalMLPipeline, ModelUnavailableError, AmbiguousPredictionError

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
        ml_pipeline: LocalMLPipeline | None = None,
    ):
        """Initialize the document processor.

        Args:
            registry: TemplateRegistry with configuration.
            min_text_length: Minimum text length for valid extraction.
        """
        self.registry = registry
        self.min_text_length = min_text_length
        self.ml_pipeline = ml_pipeline or LocalMLPipeline()

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
            # Step 1: Extract document text
            if extracted_text is None:
                extracted_text = self._extract_text(pdf_bytes)
            
            if extracted_text is None:
                result.errors.append("Failed to extract text from PDF")
                result.status = SidecarStatus.PENDING.value
                return result

            # Step 1: Predict emitting_company and document_type from the full text.
            try:
                initial_fields = self.ml_pipeline.extract_initial_fields(extracted_text)
                emitting_company = initial_fields["emitting_company"][0]
                doc_type = initial_fields["document_type"][0]
                initial_confidence = min(value[1] for value in initial_fields.values())
            except (ModelUnavailableError, AmbiguousPredictionError) as exc:
                result.errors.append(str(exc))
                result.status = SidecarStatus.PENDING.value
                return result

            # Step 2: Extract the emission date using configured regexes only.
            emission_date, date_error = self._extract_emission_date(extracted_text)
            if date_error:
                result.errors.append(date_error)
                result.status = SidecarStatus.PENDING.value
                return result
            result.emission_date = emission_date

            result.company = emitting_company
            result.document_type = doc_type
            if not self.registry.is_valid_company(emitting_company):
                result.errors.append(
                    f"Unknown emitting company '{emitting_company}'. "
                    f"Valid companies: {', '.join(self.registry.get_companies()[:10])}"
                )
                result.status = SidecarStatus.PENDING.value
                return result
            if not self.registry.is_valid_document_type(doc_type):
                result.errors.append(
                    f"Unknown document type '{doc_type}'. "
                    f"Valid types: {', '.join(self.registry.get_document_types()[:10])}"
                )
                result.status = SidecarStatus.PENDING.value
                return result

            # Step 4: Determine the applicable additional-field groups from the
            # company/document-type pair, then extract their values from the text.
            # Template and period format are deliberately not consumed until this
            # first-stage field extraction has completed.
            try:
                additional_field_groups, groups_confidence = self.ml_pipeline.predict_additional_groups(emitting_company, doc_type)
            except (ModelUnavailableError, AmbiguousPredictionError) as exc:
                result.errors.append(str(exc))
                result.status = SidecarStatus.PENDING.value
                return result

            optional_fields: dict[str, str] = {}
            field_confidences = [initial_confidence, groups_confidence]
            if additional_field_groups:
                try:
                    extracted_additional_fields = self.ml_pipeline.predict_additional_values(
                        additional_field_groups, extracted_text
                    )
                except (ModelUnavailableError, AmbiguousPredictionError) as exc:
                    result.errors.append(str(exc))
                    result.status = SidecarStatus.PENDING.value
                    return result
                for field_name, (field_value, field_confidence) in extracted_additional_fields.items():
                    optional_fields[field_name] = field_value
                    field_confidences.append(field_confidence)

            # Template and period format are independent ML predictions, not
            # deterministic mapping lookups. Configuration only validates that
            # predicted identifiers can be executed downstream.
            try:
                template_name, template_confidence, period_format, period_confidence = (
                    self.ml_pipeline.predict_template_and_period_format(emitting_company, doc_type, optional_fields)
                )
            except (ModelUnavailableError, AmbiguousPredictionError) as exc:
                result.errors.append(str(exc))
                result.status = SidecarStatus.PENDING.value
                return result
            field_confidences.extend([template_confidence, period_confidence])
            if not template_name:
                result.errors.append(
                    f"No template resolved for company='{company}', "
                    f"document_type='{doc_type}'. Routing to review."
                )
                result.status = SidecarStatus.PENDING.value
                return result
            if not period_format:
                result.errors.append(
                    f"No period_format resolved for company='{company}', "
                    f"document_type='{doc_type}'. Routing to review."
                )
                result.status = SidecarStatus.PENDING.value
                return result

            result.template_name = template_name
            result.period_format = period_format

            # Step 5: Calculate period using period_format configuration
            if not period_format:
                result.errors.append(
                    f"No period_format resolved for company='{company}', "
                    f"document_type='{doc_type}'. Routing to review."
                )
                result.status = SidecarStatus.PENDING.value
                return result
            
            period_format_config = self.registry.get_period_format_config(period_format)
            if period_format_config is None:
                result.errors.append(
                    f"Period format '{period_format}' not found in configuration"
                )
                result.status = SidecarStatus.PENDING.value
                return result
            
            try:
                period = format_period_from_emission_date(
                    emission_date,
                    period_format_config.to_dict()
                )
                result.period = period
            except Exception as e:
                result.errors.append(f"Failed to compute period: {e}")
                result.status = SidecarStatus.PENDING.value
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
                    result.status = SidecarStatus.PENDING.value
                    return result
            else:
                # Fallback: use a simple pattern
                final_key = f"{template_name}/{period}/{company}/{doc_type}/{emission_date}_{original_key.split('/')[-1]}"

            # Step 7: Build the sidecar
            confidence = min(field_confidences)
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
        """Extract text from every page of the PDF.

        Args:
            pdf_bytes: The PDF content as bytes.

        Returns:
            Extracted text, or None if extraction fails.
        """
        try:
            text = extract_document_text(pdf_bytes)
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
        """Predict emitting company and document type with local ML models."""
        try:
            fields = self.ml_pipeline.extract_initial_fields(text)
            return fields["emitting_company"][0], fields["document_type"][0], []
        except (ModelUnavailableError, AmbiguousPredictionError) as exc:
            return "", "", [str(exc)]

    def _extract_field_value(self, text: str, group_name: str) -> tuple[str, str | None]:
        """Predict an additional-field value from text using a local ML model."""
        try:
            value, _confidence = self.ml_pipeline.predict_additional_value(group_name, text)
            return value, None
        except (ModelUnavailableError, AmbiguousPredictionError) as exc:
            return "", str(exc)
