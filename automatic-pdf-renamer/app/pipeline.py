"""Processing pipeline module for Automatic PDF Renamer.

Implements the ordered ML and deterministic processing workflow:
1. Predict emitting_company and document_type from extracted PDF text
2. Extract emission_date using configured date_extraction regexes only
3. Predict applicable additional-field groups from emitting_company and document_type
4. Extract additional-field values from the full text
5. Predict template_name and period_format from emitting_company, document_type, and additional fields
6. Calculate period and current_key from the final predictions and configuration
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
            company = emitting_company  # Compatibility alias for existing key-pattern placeholders.
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

            # Step 3: Determine the additional-field groups from emitting_company
            # and document_type; the period format is not predicted at this stage.
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

            # Step 6: Calculate period once using the final period-format configuration.
            
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

            # Step 6: Generate current_key from the template and final field values.
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
        """Return the earliest valid date matched by configured regexes only."""
        patterns = self.registry.get_date_extraction_compiled()
        matches: list[tuple[int, int, str]] = []
        for pattern_index, pattern in enumerate(patterns):
            for match in pattern.finditer(text):
                raw_value = self._reconstruct_date(match.groups()) or match.group(0)
                normalized = self._normalize_emission_date(raw_value)
                if normalized:
                    matches.append((match.start(), pattern_index, normalized))
        if not matches:
            return "", "Could not find a valid emission date matching configured date_extraction patterns"
        matches.sort(key=lambda item: (item[0], item[1]))
        return matches[0][2], None

    def _normalize_emission_date(self, value: str) -> str | None:
        """Normalize a regex-captured date to YYYY-MM-DD."""
        value = " ".join(value.strip().split()).casefold()
        french_months = {
            "janvier": "01", "fevrier": "02", "mars": "03", "avril": "04",
            "mai": "05", "juin": "06", "juillet": "07", "aout": "08",
            "septembre": "09", "octobre": "10", "novembre": "11", "decembre": "12",
        }
        for month_name, month_number in french_months.items():
            value = re.sub(rf"\b{month_name}\b", month_number, value)
        formats = (
            "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d",
            "%Y/%m", "%Y-%m", "%m/%Y", "%m-%Y",
            "%d %m %Y", "%Y %m %d", "%m %Y", "%Y%m%d", "%Y",
        )
        for date_format in formats:
            try:
                parsed = datetime.strptime(value, date_format)
                if date_format == "%Y" or date_format in {"%Y/%m", "%Y-%m", "%m/%Y", "%m-%Y"}:
                    parsed = parsed.replace(month=1 if date_format == "%Y" else parsed.month, day=1)
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None
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
