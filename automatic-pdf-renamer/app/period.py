"""Period computation module for Automatic PDF Renamer.

Provides period parsing from filenames and computation from emission dates
per template rules (PRD section 4.1, FR4.2, FR4.3).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from dateutil.relativedelta import relativedelta


class PeriodGranularity(Enum):
    """Granularity of period computation (PRD section 4.1)."""

    DAY = "day"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class PeriodOffset(Enum):
    """Offset for period computation (PRD section 4.1)."""

    CURRENT = "current"
    NEXT = "next"


@dataclass
class PeriodRule:
    """Template period rule for period computation."""

    granularity: PeriodGranularity
    offset: PeriodOffset = PeriodOffset.CURRENT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PeriodRule":
        """Create a PeriodRule from a dict."""
        return cls(
            granularity=PeriodGranularity(data.get("granularity", "month")),
            offset=PeriodOffset(data.get("offset", "current")),
        )


# French month names mapping
_FRENCH_MONTHS = {
    "janvier": 1,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    # English fallbacks
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# Month name to full French name for output
_FRENCH_MONTH_NAMES = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril", 5: "mai", 6: "juin",
    7: "juillet", 8: "août", 9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre",
}

# Month abbreviation mapping
_MONTH_ABBR = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

# Quarter pattern - matches Q3 2025, T3 2025, 3 2025, Q3-2025, trimestre 3 2025, 3T2025, etc.
_QUARTER_PATTERN = re.compile(
    r"(?:Q|T|trimestre|quarter)?[\s\-]?(\d{1,2})[\s\-]?(?:Q|T|trimestre|quarter)?[\s\-]?(\d{4})\b|\b(\d{1,2})[TQ]?(\d{4})\b",
    re.IGNORECASE,
)

# Year pattern
_YEAR_PATTERN = re.compile(r"\b(\d{4})\b")

# Month-year patterns (various formats)
_MONTH_YEAR_PATTERNS = [
    # "août 2025", "August 2025" - French/English month name + year
    re.compile(
        rf"\b([{''.join(_FRENCH_MONTHS.keys())}]+)\s+(\d{{4}})\b",
        re.IGNORECASE,
    ),
    # "2025-08", "2025/08", "08-2025"
    re.compile(r"(\d{4})[-/](\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[-/](\d{4})\b"),
    # "Aug 2025", "Aug-2025"
    re.compile(r"\b([A-Za-z]{3})[-\s]?(\d{4})\b"),
    # "2025 08", "08 2025"
    re.compile(r"\b(\d{4})\s+(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})\s+(\d{4})\b"),
]

# Date pattern (YYYY-MM-DD, DD/MM/YYYY, etc.)
_DATE_PATTERN = re.compile(
    r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})|(\d{1,2})[-/](\d{1,2})[-/](\d{4})"
)


def _parse_french_month(month_str: str) -> int | None:
    """Parse a French or English month name to month number."""
    month_lower = month_str.lower().strip()
    return _FRENCH_MONTHS.get(month_lower)


def _parse_month_abbr(abbr: str) -> int | None:
    """Parse a 3-letter month abbreviation to month number."""
    abbr_upper = abbr.upper()
    for num, name in _MONTH_ABBR.items():
        if name.upper() == abbr_upper:
            return num
    return None


def parse_period_from_filename(filename: str) -> str | None:
    """Parse a period string from a filename.

    Tries to extract period from the filename, looking at various positions.
    Supports formats:
    - Month-year: "août 2025", "August 2025", "Aug 2025", "2025-08"
    - Quarter: "Q3 2025", "T3 2025", "3 2025", "Q3-2025"
    - Year: "2025"
    - Date: "2025-08-31", "31/08/2025"

    Args:
        filename: The filename to parse (without path).

    Returns:
        Parsed period string (e.g., "août 2025", "Q3 2025", "2025"), or None.
    """
    # Get the filename without path
    basename = filename.split("/")[-1].split("\\")[-1]
    # Remove extension
    name = basename.rsplit(".", 1)[0]

    # First, try to find quarter patterns anywhere in the filename
    quarter_match = _QUARTER_PATTERN.search(name)
    if quarter_match:
        # Handle different group patterns
        groups = quarter_match.groups()
        quarter_num = None
        year = None
        for g in groups:
            if g and g.isdigit() and len(g) <= 2:
                q = int(g)
                if 1 <= q <= 4 and quarter_num is None:
                    quarter_num = q
            elif g and g.isdigit() and len(g) == 4:
                year = int(g)
        if quarter_num and year:
            return format_quarter(quarter_num, year)

    # Try to find month-year patterns anywhere in the filename
    for pattern in _MONTH_YEAR_PATTERNS:
        match = pattern.search(name)
        if match:
            groups = match.groups()
            year = None
            month = None
            
            for g in groups:
                if g and g.isdigit() and len(g) == 4:
                    year = int(g)
                elif g and g.isdigit() and len(g) <= 2:
                    if month is None:
                        month = int(g)
                elif g:
                    # Try as month name (French or English)
                    parsed = _parse_french_month(g)
                    if parsed:
                        month = parsed
                    else:
                        parsed = _parse_month_abbr(g)
                        if parsed:
                            month = parsed
            
            # If we found a year and a month, return it
            if year is not None and month is not None:
                return format_month_year(month, year)

    # Try to find date patterns (YYYY-MM-DD, DD/MM/YYYY, etc.)
    date_match = _DATE_PATTERN.search(name)
    if date_match:
        groups = date_match.groups()
        year = None
        month = None
        day = None
        for g in groups:
            if g and len(g) == 4:
                year = int(g)
            elif g and len(g) <= 2:
                if day is None:
                    day = int(g)
                else:
                    month = int(g)
        if year and month:
            return format_month_year(month, year)
        elif year:
            return str(year)

    # Try simple year pattern anywhere
    year_match = _YEAR_PATTERN.search(name)
    if year_match:
        return year_match.group(1)

    return None


def compute_period_from_date(
    emission_date: str | date,
    rule: PeriodRule,
) -> str:
    """Compute period from emission date and template rule.

    Args:
        emission_date: The document emission date (ISO format YYYY-MM-DD or date object).
        rule: PeriodRule with granularity and offset.

    Returns:
        Human-readable period string.
    """
    if isinstance(emission_date, str):
        # Parse ISO date
        try:
            dt = datetime.fromisoformat(emission_date).date()
        except ValueError:
            # Try other formats
            for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"]:
                try:
                    dt = datetime.strptime(emission_date, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"Cannot parse emission_date: {emission_date}")
    else:
        dt = emission_date

    if rule.granularity == PeriodGranularity.DAY:
        return f"{dt.day} {_FRENCH_MONTH_NAMES[dt.month]} {dt.year}"

    elif rule.granularity == PeriodGranularity.MONTH:
        if rule.offset == PeriodOffset.CURRENT:
            return format_month_year(dt.month, dt.year)
        else:  # NEXT
            next_month = dt + relativedelta(months=1)
            return format_month_year(next_month.month, next_month.year)

    elif rule.granularity == PeriodGranularity.QUARTER:
        quarter = (dt.month - 1) // 3 + 1
        if rule.offset == PeriodOffset.CURRENT:
            return format_quarter(quarter, dt.year)
        else:  # NEXT
            next_quarter = quarter + 1
            next_year = dt.year
            if next_quarter > 4:
                next_quarter = 1
                next_year += 1
            return format_quarter(next_quarter, next_year)

    elif rule.granularity == PeriodGranularity.YEAR:
        if rule.offset == PeriodOffset.CURRENT:
            return str(dt.year)
        else:  # NEXT
            return str(dt.year + 1)

    else:
        raise ValueError(f"Unknown granularity: {rule.granularity}")


def format_month_year(month: int, year: int, use_french: bool = True) -> str:
    """Format month and year as a human-readable string.

    Uses French month names as per PRD by default.

    Args:
        month: Month number (1-12).
        year: Year number.
        use_french: If True, use French month names; otherwise use English.

    Returns:
        Formatted string (e.g., "août 2025" or "August 2025").
    """
    if month < 1 or month > 12:
        raise ValueError(f"Invalid month: {month}")
    if use_french:
        month_name = _FRENCH_MONTH_NAMES[month]
    else:
        month_name = _MONTH_ABBR[month]
    return f"{month_name} {year}"


def format_quarter(quarter: int, year: int) -> str:
    """Format quarter and year as a human-readable string.

    Args:
        quarter: Quarter number (1-4).
        year: Year number.

    Returns:
        Formatted string (e.g., "Q3 2025").
    """
    if quarter < 1 or quarter > 4:
        raise ValueError(f"Invalid quarter: {quarter}")
    return f"Q{quarter} {year}"


def get_period(
    filename: str | None = None,
    emission_date: str | date | None = None,
    rule: PeriodRule | None = None,
) -> tuple[str, bool]:
    """Get the period for a document, trying filename first, then fallback to rule.

    This is the main entry point for period computation.

    Args:
        filename: The document filename (optional).
        emission_date: The document emission date (optional).
        rule: The template period rule (optional, required if emission_date provided).

    Returns:
        Tuple of (period_string, is_from_filename).
        If period is parsed from filename, is_from_filename is True.
        If computed from rule, is_from_filename is False.

    Raises:
        ValueError: If no period can be determined and no fallback is possible.
    """
    # Try to parse from filename first
    if filename:
        period = parse_period_from_filename(filename)
        if period:
            return (period, True)

    # Fallback to rule-based computation
    if emission_date and rule:
        period = compute_period_from_date(emission_date, rule)
        return (period, False)

    # If we have emission_date but no rule, use month-year as fallback
    if emission_date:
        if isinstance(emission_date, str):
            try:
                dt = datetime.fromisoformat(emission_date).date()
            except ValueError:
                for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"]:
                    try:
                        dt = datetime.strptime(emission_date, fmt).date()
                        break
                    except ValueError:
                        continue
                else:
                    raise ValueError(f"Cannot parse emission_date: {emission_date}")
        else:
            dt = emission_date
        return (format_month_year(dt.month, dt.year), False)

    raise ValueError("Cannot determine period: no filename, emission_date, or rule provided")


def validate_period_consistency(
    period: str,
    emission_date: str | date,
    rule: PeriodRule,
) -> tuple[bool, str | None]:
    """Validate that a period is consistent with emission date and template rule.

    Args:
        period: The period string to validate.
        emission_date: The document emission date.
        rule: The template period rule.

    Returns:
        Tuple of (is_consistent, error_message).
        If consistent, error_message is None.
    """
    if isinstance(emission_date, str):
        try:
            dt = datetime.fromisoformat(emission_date).date()
        except ValueError:
            for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"]:
                try:
                    dt = datetime.strptime(emission_date, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                return (False, f"Cannot parse emission_date: {emission_date}")
    else:
        dt = emission_date

    # Compute expected period from rule
    expected_period = compute_period_from_date(dt, rule)

    # Normalize both periods for comparison
    # This is a simple comparison; may need enhancement for different formats
    if period.lower() == expected_period.lower():
        return (True, None)

    # Try to parse both and compare
    try:
        parsed = _parse_period_string(period)
        expected_parsed = _parse_period_string(expected_period)
        if parsed == expected_parsed:
            return (True, None)
    except ValueError:
        pass

    return (
        False,
        f"Period '{period}' is inconsistent with emission_date '{emission_date}' "
        f"and rule {rule.granularity.value}/{rule.offset.value}. "
        f"Expected: {expected_period}",
    )


def _parse_period_string(period: str) -> tuple[int, int, int | None]:
    """Parse a period string into (year, month, quarter) tuple.

    Returns:
        (year, month, quarter) where month and quarter may be None.
    """
    period_lower = period.lower()

    # Try year only
    year_match = _YEAR_PATTERN.search(period_lower)
    if year_match:
        year = int(year_match.group(1))
        return (year, None, None)

    # Try quarter
    quarter_match = _QUARTER_PATTERN.search(period_lower)
    if quarter_match:
        quarter = int(quarter_match.group(1))
        year = int(quarter_match.group(2))
        return (year, None, quarter)

    # Try month-year
    for pattern in _MONTH_YEAR_PATTERNS:
        match = pattern.search(period_lower)
        if match:
            groups = match.groups()
            year = None
            month = None
            for g in groups:
                if g and g.isdigit() and len(g) == 4:
                    year = int(g)
                elif g and g.isdigit() and len(g) <= 2:
                    month = int(g)
                elif g:
                    parsed_month = _parse_french_month(g)
                    if parsed_month:
                        month = parsed_month
                    else:
                        parsed_abbr = _parse_month_abbr(g)
                        if parsed_abbr:
                            month = parsed_abbr

            if year and month:
                return (year, month, None)

    raise ValueError(f"Cannot parse period string: {period}")
