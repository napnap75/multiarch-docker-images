"""Period computation module for Automatic PDF Renamer.

Provides period parsing from filenames and computation from emission dates
per template rules (PRD section 4.1, FR4.2, FR4.3).

Supports custom period format templates with placeholders:
- {year}, {year_next}, {year_previous}
- {month}, {month_name}, {month_abbr}
- {day}
- {quarter}, {semester}, {week}
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
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    SEMESTER = "semester"
    YEAR = "year"


class PeriodOffset(Enum):
    """Offset for period computation (PRD section 4.1)."""

    CURRENT = "current"
    PREVIOUS = "previous"
    FOLLOWING = "following"


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
    "Janvier": 1,
    "F\u00e9vrier": 2,
    "Mars": 3,
    "Avril": 4,
    "Mai": 5,
    "Juin": 6,
    "Juillet": 7,
    "Ao\u00fbt": 8,
    "Septembre": 9,
    "Octobre": 10,
    "Novembre": 11,
    "D\u00e9cembre": 12,
    # English fallbacks
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}

# Month name to full French name for output
_FRENCH_MONTH_NAMES = {
    "01": "Janvier", "02": "F\u00e9vrier", "03": "Mars", "04": "Avril", "05": "Mai", "06": "Juin",
    "07": "Juillet", "08": "Ao\u00fbt", "09": "Septembre", "10": "Octobre", "11": "Novembre", "12": "D\u00e9cembre",
}

# Month abbreviation mapping
_MONTH_ABBR = {
    "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
    "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
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
    # "ao\u00fbt 2025", "August 2025" - French/English month name + year
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
    - Month-year: "ao\u00fbt 2025", "August 2025", "Aug 2025", "2025-08"
    - Quarter: "Q3 2025", "T3 2025", "3 2025", "Q3-2025"
    - Year: "2025"
    - Date: "2025-08-31", "31/08/2025"

    Args:
        filename: The filename to parse (without path).

    Returns:
        Parsed period string (e.g., "ao\u00fbt 2025", "Q3 2025", "2025"), or None.
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


def _parse_date_string(date_str: str) -> date:
    """Parse a date string into a date object.
    
    Supports various formats: YYYY-MM-DD, DD/MM/YYYY, MM/DD/YYYY, etc.
    
    Args:
        date_str: The date string to parse.
        
    Returns:
        date object.
        
    Raises:
        ValueError: If the date string cannot be parsed.
    """
    if isinstance(date_str, date):
        return date_str
    
    # Try ISO format first
    try:
        return datetime.fromisoformat(date_str).date()
    except ValueError:
        pass
    
    # Try other common formats
    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%Y-%m",
        "%m-%Y",
        "%Y",
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    
    # Try to parse month name + year (e.g., "August 2025", "ao\u00fbt 2025")
    month_year_match = re.match(
        rf"^([{''.join(_FRENCH_MONTHS.keys())}]+)\s+(\d{{4}})$",
        date_str.strip(),
        re.IGNORECASE
    )
    if month_year_match:
        month_name = month_year_match.group(1)
        year = int(month_year_match.group(2))
        month = _parse_french_month(month_name)
        if month:
            return date(year, month, 1)
    
    # Try year only
    if re.match(r"^\d{4}$", date_str):
        return date(int(date_str), 1, 1)
    
    raise ValueError(f"Cannot parse date string: {date_str}")


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
    dt = _parse_date_string(emission_date)

    if rule.granularity == PeriodGranularity.DAY:
        return f"{dt.day:02d} {_FRENCH_MONTH_NAMES[dt.month]} {dt.year:04d}"

    elif rule.granularity == PeriodGranularity.MONTH:
        if rule.offset == PeriodOffset.CURRENT:
            return format_month_year(dt.month, dt.year)
        elif rule.offset == PeriodOffset.PREVIOUS:
            prev_month = dt + relativedelta(months=-1)
            return format_month_year(prev_month.month, prev_month.year)
        else:  # FOLLOWING
            next_month = dt + relativedelta(months=1)
            return format_month_year(next_month.month, next_month.year)

    elif rule.granularity == PeriodGranularity.QUARTER:
        quarter = (dt.month - 1) // 3 + 1
        if rule.offset == PeriodOffset.CURRENT:
            return format_quarter(quarter, dt.year)
        elif rule.offset == PeriodOffset.PREVIOUS:
            prev_quarter = quarter - 1
            prev_year = dt.year
            if prev_quarter < 1:
                prev_quarter = 4
                prev_year -= 1
            return format_quarter(prev_quarter, prev_year)
        else:  # FOLLOWING
            next_quarter = quarter + 1
            next_year = dt.year
            if next_quarter > 4:
                next_quarter = 1
                next_year += 1
            return format_quarter(next_quarter, next_year)

    elif rule.granularity == PeriodGranularity.SEMESTER:
        semester = (dt.month - 1) // 6 + 1
        if rule.offset == PeriodOffset.CURRENT:
            return f"S{semester} {dt.year:04d}"
        elif rule.offset == PeriodOffset.PREVIOUS:
            prev_semester = semester - 1
            prev_year = dt.year
            if prev_semester < 1:
                prev_semester = 2
                prev_year -= 1
            return f"S{prev_semester} {prev_year}"
        else:  # FOLLOWING
            next_semester = semester + 1
            next_year = dt.year
            if next_semester > 2:
                next_semester = 1
                next_year += 1
            return f"S{next_semester} {next_year:04d}"

    elif rule.granularity == PeriodGranularity.WEEK:
        # ISO week number
        iso_year, iso_week, _ = dt.isocalendar()
        if rule.offset == PeriodOffset.CURRENT:
            return f"Week {iso_week:02d} {iso_year:04d}"
        elif rule.offset == PeriodOffset.PREVIOUS:
            prev_date = dt - relativedelta(weeks=1)
            prev_year, prev_week, _ = prev_date.isocalendar()
            return f"Week {prev_week:02d} {prev_year:04d}"
        else:  # FOLLOWING
            next_date = dt + relativedelta(weeks=1)
            next_year, next_week, _ = next_date.isocalendar()
            return f"Week {next_week:02d} {next_year:04d}"

    elif rule.granularity == PeriodGranularity.YEAR:
        if rule.offset == PeriodOffset.CURRENT:
            return str(dt.year)
        elif rule.offset == PeriodOffset.PREVIOUS:
            return str(dt.year - 1)
        else:  # FOLLOWING
            return str(dt.year + 1)

    else:
        raise ValueError(f"Unknown granularity: {rule.granularity}")


def format_period_from_emission_date(
    emission_date: str | date,
    period_format: dict[str, Any],
) -> str:
    """Format period from emission date using custom period format configuration.
    
    The period_format dict contains:
    - granularity: day, week, month, quarter, semester, year
    - format: Custom template string with placeholders
    - offset: current, previous, following
    
    Supported placeholders in format template:
    - {year}: Current year
    - {year_next}: Next year
    - {year_previous}: Previous year
    - {month}: Month number (1-12)
    - {month_name}: Full month name (French)
    - {month_abbr}: 3-letter month abbreviation
    - {day}: Day of month (1-31)
    - {quarter}: Quarter number (1-4)
    - {semester}: Semester number (1-2)
    - {week}: ISO week number (1-53)
    
    Args:
        emission_date: The document emission date (string or date object).
        period_format: Dict with granularity, format, and offset keys.
        
    Returns:
        Formatted period string with placeholders replaced.
        
    Raises:
        ValueError: If granularity is unknown or format template is invalid.
    """
    dt = _parse_date_string(emission_date)
    
    granularity = period_format.get("granularity", "month")
    format_template = period_format.get("format", "")
    offset = period_format.get("offset", "current")
    
    # Determine the base date based on offset
    if offset == "previous":
        # Date is at the end of the period, so we need the previous period
        if granularity == "day":
            base_date = dt - relativedelta(days=1)
        elif granularity == "week":
            base_date = dt - relativedelta(weeks=1)
        elif granularity == "month":
            base_date = dt - relativedelta(months=1)
        elif granularity == "quarter":
            quarter = (dt.month - 1) // 3 + 1
            if quarter == 1:
                base_date = date(dt.year - 1, 12, 1)
            else:
                base_date = date(dt.year, (quarter - 1) * 3, 1)
        elif granularity == "semester":
            semester = (dt.month - 1) // 6 + 1
            if semester == 1:
                base_date = date(dt.year - 1, 12, 1)
            else:
                base_date = date(dt.year, 6, 1)
        elif granularity == "year":
            base_date = date(dt.year - 1, 1, 1)
        else:
            base_date = dt
    elif offset == "following":
        # Date is at the beginning of the period, so we need the following period
        if granularity == "day":
            base_date = dt + relativedelta(days=1)
        elif granularity == "week":
            base_date = dt + relativedelta(weeks=1)
        elif granularity == "month":
            base_date = dt + relativedelta(months=1)
        elif granularity == "quarter":
            quarter = (dt.month - 1) // 3 + 1
            if quarter == 4:
                base_date = date(dt.year + 1, 1, 1)
            else:
                base_date = date(dt.year, (quarter + 1) * 3, 1)
        elif granularity == "semester":
            semester = (dt.month - 1) // 6 + 1
            if semester == 2:
                base_date = date(dt.year + 1, 1, 1)
            else:
                base_date = date(dt.year, 6, 1)
        elif granularity == "year":
            base_date = date(dt.year + 1, 1, 1)
        else:
            base_date = dt
    else:  # current
        # Date is in the middle of the period, use the date as-is
        base_date = dt
    
    # Calculate period values based on granularity
    period_values: dict[str, Any] = {}
    
    if granularity == "day":
        period_values["year"] = f"{base_date.year:04d}"
        period_values["month"] = f"{base_date.month:02d}"
        period_values["month_name"] = _FRENCH_MONTH_NAMES[f"{base_date.month:02d}"]
        period_values["month_abbr"] = _MONTH_ABBR[f"{base_date.month:02d}"]
        period_values["day"] = f"{base_date.day:02d}"
        period_values["quarter"] = (base_date.month - 1) // 3 + 1
        period_values["semester"] = (base_date.month - 1) // 6 + 1
        _, period_values["week"], _ = base_date.isocalendar()
    
    elif granularity == "week":
        _, period_values["week"], _ = base_date.isocalendar()
        period_values["year"] = f"{base_date.year:04d}"
        period_values["month"] = f"{base_date.month:02d}"
        period_values["month_name"] = _FRENCH_MONTH_NAMES[f"{base_date.month:02d}"]
        period_values["month_abbr"] = _MONTH_ABBR[f"{base_date.month:02d}"]
        period_values["day"] = f"{base_date.day:02d}"
        period_values["quarter"] = (base_date.month - 1) // 3 + 1
        period_values["semester"] = (base_date.month - 1) // 6 + 1
    
    elif granularity == "month":
        period_values["year"] = f"{base_date.year:04d}"
        period_values["month"] = f"{base_date.month:02d}"
        period_values["month_name"] = _FRENCH_MONTH_NAMES[f"{base_date.month:02d}"]
        period_values["month_abbr"] = _MONTH_ABBR[f"{base_date.month:02d}"]
        period_values["day"] = f"{1:02d}"  # Default to first day of month
        period_values["quarter"] = (base_date.month - 1) // 3 + 1
        period_values["semester"] = (base_date.month - 1) // 6 + 1
        _, period_values["week"], _ = base_date.isocalendar()
    
    elif granularity == "quarter":
        quarter = (base_date.month - 1) // 3 + 1
        period_values["quarter"] = quarter
        period_values["year"] = f"{base_date.year:04d}"
        period_values["month"] = f"{(quarter - 1) * 3 + 1:02d}"  # First month of quarter
        period_values["month_name"] = _FRENCH_MONTH_NAMES[f"{period_values['month']}"]
        period_values["month_abbr"] = _MONTH_ABBR[f"{period_values['month']}"]
        period_values["day"] = f"{1:02d}"
        period_values["semester"] = (quarter - 1) // 2 + 1
        _, period_values["week"], _ = base_date.isocalendar()
    
    elif granularity == "semester":
        semester = (base_date.month - 1) // 6 + 1
        period_values["semester"] = semester
        period_values["year"] = f"{base_date.year:04d}"
        period_values["month"] = f"{(semester - 1) * 6 + 1:02d}"  # First month of semester
        period_values["month_name"] = _FRENCH_MONTH_NAMES[f"{period_values['month']}"]
        period_values["month_abbr"] = _MONTH_ABBR[f"{period_values['month']}"]
        period_values["day"] = f"{1:02d}"
        period_values["quarter"] = (period_values["month"] - 1) // 3 + 1
        _, period_values["week"], _ = base_date.isocalendar()
    
    elif granularity == "year":
        period_values["year"] = f"{base_date.year:04d}"
        period_values["month"] = f"{1:02d}"
        period_values["month_name"] = "janvier"
        period_values["month_abbr"] = "Jan"
        period_values["day"] = f"{1:02d}"
        period_values["quarter"] = 1
        period_values["semester"] = 1
        _, period_values["week"], _ = base_date.isocalendar()
    
    else:
        raise ValueError(f"Unknown granularity: {granularity}")
    
    # Add year_next and year_previous
    period_values["year_next"] = f"{int(period_values['year']) + 1:04d}"
    period_values["year_previous"] = f"{int(period_values['year']) - 1:04d}"
    
    # Format the template
    try:
        return format_template.format(**period_values)
    except KeyError as e:
        missing = str(e).strip("'")
        raise ValueError(f"Unknown placeholder '{missing}' in format template: {format_template}")


def format_month_year(month: int, year: int, use_french: bool = True) -> str:
    """Format month and year as a human-readable string.

    Uses French month names as per PRD by default.

    Args:
        month: Month number (1-12).
        year: Year number.
        use_french: If True, use French month names; otherwise use English.

    Returns:
        Formatted string (e.g., "ao\u00fbt 2025" or "August 2025").
    """
    if month < 1 or month > 12:
        raise ValueError(f"Invalid month: {month}")
    if use_french:
        month_name = _FRENCH_MONTH_NAMES[month]
    else:
        month_name = _MONTH_ABBR[month]
    return f"{month_name} {year:04d}"


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
    return f"Q{quarter} {year:04d}"


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
        dt = _parse_date_string(emission_date)
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
    dt = _parse_date_string(emission_date)

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
