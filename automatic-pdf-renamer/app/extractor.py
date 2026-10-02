"""Text extraction module for Automatic PDF Renamer.

Provides PDF text extraction using PyMuPDF (PRD FR2.1, FR2.2).
This is the same extraction used by both the import script and the service.
"""

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Minimum text length threshold (PRD FR2.3)
MIN_TEXT_LENGTH = 10


class TextExtractionError(Exception):
    """Error raised when text extraction fails."""
    pass


def extract_first_page_text(pdf_bytes: bytes) -> str:
    """Extract text from the first page of a PDF.

    Uses PyMuPDF for fast text extraction (PRD stack decision).
    Extracts only the first page as per FR2.2.

    Args:
        pdf_bytes: The PDF file content as bytes.

    Returns:
        The extracted text from the first page.

    Raises:
        TextExtractionError: If extraction fails or PDF is invalid.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise TextExtractionError(
            "PyMuPDF is required for text extraction. Install with: pip install pymupdf"
        )

    try:
        # Open PDF from bytes
        doc = fitz.open(stream=pdf_bytes)

        if len(doc) == 0:
            logger.warning("PDF has no pages")
            return ""

        # Extract text from first page only (FR2.2)
        first_page = doc[0]
        text = first_page.get_text()

        doc.close()
        return text

    except Exception as e:
        logger.error(f"Failed to extract text from PDF: {e}")
        raise TextExtractionError(f"Text extraction failed: {e}") from e


def extract_first_page_text_from_file(filepath: str) -> str:
    """Extract text from the first page of a PDF file.

    Args:
        filepath: Path to the PDF file.

    Returns:
        The extracted text from the first page.

    Raises:
        TextExtractionError: If extraction fails or PDF is invalid.
        FileNotFoundError: If the file does not exist.
    """
    try:
        with open(filepath, "rb") as f:
            pdf_bytes = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"PDF file not found: {filepath}")

    return extract_first_page_text(pdf_bytes)


def is_text_empty(text: str, min_length: int = MIN_TEXT_LENGTH) -> bool:
    """Check if extracted text is effectively empty.

    Considers text empty if:
    - It's an empty string
    - It's only whitespace
    - It's shorter than min_length characters

    Args:
        text: The text to check.
        min_length: Minimum length threshold (default: 10).

    Returns:
        True if text is considered empty, False otherwise.
    """
    if not text:
        return True
    if not text.strip():
        return True
    if len(text.strip()) < min_length:
        return True
    return False


def extract_and_validate(pdf_bytes: bytes, min_length: int = MIN_TEXT_LENGTH) -> tuple[str, bool]:
    """Extract first page text and check if it's valid.

    Args:
        pdf_bytes: The PDF file content as bytes.
        min_length: Minimum text length threshold.

    Returns:
        Tuple of (extracted_text, is_valid).
        is_valid is False if text is empty or below min_length.

    Raises:
        TextExtractionError: If extraction fails.
    """
    text = extract_first_page_text(pdf_bytes)
    is_valid = not is_text_empty(text, min_length)
    return (text, is_valid)


class TextExtractor:
    """Text extractor class for dependency injection.

    Wraps the extraction functions to allow for mocking in tests
    and potential future swapping of extraction libraries.
    """

    def __init__(self, min_length: int = MIN_TEXT_LENGTH):
        """Initialize the text extractor.

        Args:
            min_length: Minimum text length threshold.
        """
        self.min_length = min_length

    def extract(self, pdf_bytes: bytes) -> str:
        """Extract first page text from PDF bytes.

        Args:
            pdf_bytes: The PDF file content as bytes.

        Returns:
            The extracted text.
        """
        return extract_first_page_text(pdf_bytes)

    def extract_and_validate(self, pdf_bytes: bytes) -> tuple[str, bool]:
        """Extract and validate text.

        Args:
            pdf_bytes: The PDF file content as bytes.

        Returns:
            Tuple of (text, is_valid).
        """
        return extract_and_validate(pdf_bytes, self.min_length)

    def is_empty(self, text: str) -> bool:
        """Check if text is empty.

        Args:
            text: The text to check.

        Returns:
            True if text is considered empty.
        """
        return is_text_empty(text, self.min_length)
