"""Configuration module for Automatic PDF Renamer.

Provides environment variable parsing and JSONC configuration loading.
Supports the new mapping-based configuration structure where:
- companies: predefined list of valid companies
- document_types: predefined list of valid document types
- additional_fields: predefined value groups for ML extraction
- date_extraction: ordered list of regex strings for finding emission date
- period_formats: named configurations with granularity, format template, offset
- mappings: ordered list of rules with match criteria and set actions
  - match: can contain company, document_type, both, or neither (empty = matches all)
  - set: can contain template, period_format, additional_fields
  - Rules are processed in order, later matches override earlier ones
- templates: template definitions with key patterns
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class S3Config:
    """S3 storage configuration."""

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket_name: str
    region: str = "us-east-1"

    @classmethod
    def from_env(cls) -> "S3Config":
        """Create S3Config from environment variables."""
        return cls(
            endpoint_url=os.environ.get("S3_ENDPOINT_URL", ""),
            access_key=os.environ.get("S3_ACCESS_KEY_ID", ""),
            secret_key=os.environ.get("S3_SECRET_ACCESS_KEY", ""),
            bucket_name=os.environ.get("S3_BUCKET_NAME", ""),
            region=os.environ.get("S3_REGION", "us-east-1"),
        )

    def validate(self) -> None:
        """Validate that all required fields are set."""
        if not self.endpoint_url:
            raise ValueError("S3_ENDPOINT_URL environment variable is required")
        if not self.access_key:
            raise ValueError("S3_ACCESS_KEY_ID environment variable is required")
        if not self.secret_key:
            raise ValueError("S3_SECRET_ACCESS_KEY environment variable is required")
        if not self.bucket_name:
            raise ValueError("S3_BUCKET_NAME environment variable is required")


@dataclass
class PollerConfig:
    """Inbox poller configuration."""

    interval_seconds: int = 60
    inbox_prefix: str = "inbox/"
    pending_prefix: str = "pending/"
    files_prefix: str = "files/"

    @classmethod
    def from_env(cls) -> "PollerConfig":
        """Create PollerConfig from environment variables."""
        return cls(
            interval_seconds=int(os.environ.get("POLLER_INTERVAL", "60")),
            inbox_prefix=os.environ.get("INBOX_PREFIX", "inbox/"),
            pending_prefix=os.environ.get("PENDING_PREFIX", "pending/"),
            files_prefix=os.environ.get("FILES_PREFIX", "files/"),
        )


@dataclass
class ClassifierConfig:
    """Classifier configuration."""

    confidence_threshold: float = 0.8
    model_dir: str = "./models"

    @classmethod
    def from_env(cls) -> "ClassifierConfig":
        """Create ClassifierConfig from environment variables."""
        return cls(
            confidence_threshold=float(os.environ.get("CONFIDENCE_THRESHOLD", "0.8")),
            model_dir=os.environ.get("MODEL_DIR", "./models"),
        )


@dataclass
class PeriodFormatConfig:
    """Period format configuration."""

    granularity: str
    format: str
    offset: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PeriodFormatConfig":
        """Create PeriodFormatConfig from dict."""
        return cls(
            granularity=data.get("granularity", "month"),
            format=data.get("format", ""),
            offset=data.get("offset", "current"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "granularity": self.granularity,
            "format": self.format,
            "offset": self.offset,
        }


@dataclass
class MappingMatch:
    """Match criteria for a mapping rule.
    
    Can contain company, document_type, both, or neither (empty = matches all).
    """

    company: str | None = None
    document_type: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MappingMatch":
        """Create MappingMatch from dict."""
        if data is None:
            return cls()
        return cls(
            company=data.get("company"),
            document_type=data.get("document_type"),
        )

    def matches(self, company: str, document_type: str) -> bool:
        """Check if this match criteria matches the given company and document type.
        
        Empty match (both None) matches everything.
        company=None in match means match any company.
        document_type=None in match means match any document type.
        """
        company_match = self.company is None or (isinstance(self.company, list) and company in self.company) or self.company == company
        doc_type_match = self.document_type is None or (isinstance(self.document_type, list) and document_type in self.document_type) or self.document_type == document_type
        return company_match and doc_type_match


@dataclass
class MappingSet:
    """Fields to set for a mapping rule.
    
    Can contain template, period_format, and/or additional_fields.
    """

    template: str | None = None
    period_format: str | None = None
    additional_fields: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MappingSet":
        """Create MappingSet from dict."""
        if data is None:
            return cls()
        return cls(
            template=data.get("template"),
            period_format=data.get("period_format"),
            additional_fields=data.get("additional_fields", []),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.template is not None:
            result["template"] = self.template
        if self.period_format is not None:
            result["period_format"] = self.period_format
        if self.additional_fields:
            result["additional_fields"] = self.additional_fields
        return result


@dataclass
class MappingRule:
    """A mapping rule with match criteria and set actions.
    
    Rules are processed in order. For each document, all matching rules
    are applied in order, with later matches overriding earlier ones.
    """

    match: MappingMatch = field(default_factory=MappingMatch)
    set: MappingSet = field(default_factory=MappingSet)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MappingRule":
        """Create MappingRule from dict."""
        return cls(
            match=MappingMatch.from_dict(data.get("match", {})),
            set=MappingSet.from_dict(data.get("set", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        match_dict = self.match.to_dict() if hasattr(self.match, 'to_dict') else {}
        if match_dict:
            result["match"] = match_dict
        set_dict = self.set.to_dict()
        if set_dict:
            result["set"] = set_dict
        return result

    def matches(self, company: str, document_type: str) -> bool:
        """Check if this rule matches the given company and document type."""
        return self.match.matches(company, document_type)


@dataclass
class TemplateConfig:
    """Template configuration with key pattern."""

    name: str
    key_pattern: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "TemplateConfig":
        """Create TemplateConfig from dict."""
        return cls(
            name=name,
            key_pattern=data.get("key_pattern", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"key_pattern": self.key_pattern}


@dataclass
class AppConfig:
    """Main application configuration."""

    storage_backend: str = "s3"
    local_storage_dir: str = "./storage"
    s3: S3Config = field(default_factory=S3Config)
    poller: PollerConfig = field(default_factory=PollerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    debug: bool = False
    sqlite_path: str = "./renamer.db"

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Create AppConfig from environment variables."""
        debug = os.environ.get("DEBUG", "").lower() in ("true", "1", "yes")
        storage_backend = os.environ.get("APP_STORAGE_BACKEND", os.environ.get("STORAGE_BACKEND", "s3")).lower()

        config = cls(
            storage_backend=storage_backend,
            local_storage_dir=os.environ.get("LOCAL_STORAGE_DIR", "./storage"),
            s3=S3Config.from_env(),
            poller=PollerConfig.from_env(),
            classifier=ClassifierConfig.from_env(),
            debug=debug,
            sqlite_path=os.environ.get("SQLITE_PATH", "./renamer.db"),
        )

        if config.storage_backend not in {"s3", "file"}:
            raise ValueError("STORAGE_BACKEND must be either 's3' or 'file'")

        return config

    def validate(self) -> None:
        """Validate the configuration."""
        if self.storage_backend == "s3":
            self.s3.validate()
        elif self.storage_backend == "file":
            if not self.local_storage_dir:
                raise ValueError("LOCAL_STORAGE_DIR is required when STORAGE_BACKEND=file")
        else:
            raise ValueError("STORAGE_BACKEND must be either 's3' or 'file'")


class TemplateRegistry:
    """Registry for template configurations.
    
    Supports the new mapping-based configuration with:
    - companies: list of valid company names
    - document_types: list of valid document type names
    - additional_fields: predefined value groups for ML extraction
    - date_extraction: ordered list of regex strings
    - period_formats: dict of named period format configurations
    - mappings: ordered list of MappingRule objects (match + set)
    - templates: template definitions with key patterns
    """

    def __init__(self):
        self._templates: dict[str, TemplateConfig] = {}
        self._companies: list[str] = []
        self._document_types: list[str] = []
        self._additional_fields: dict[str, list[str]] = {}
        self._period_formats: dict[str, PeriodFormatConfig] = {}
        self._mappings: list[MappingRule] = []
        self._date_extraction_patterns: list[str] = []
        self._date_extraction_compiled: list[re.Pattern] = []
        self._config_version: str = "1.0"
        self._is_new_format: bool = False

    def load_from_file(self, filepath: str) -> None:
        """Load configuration from a JSONC or YAML file.

        Supports both new mapping-based format (JSONC) and legacy template format (YAML).

        Args:
            filepath: Path to the configuration file.
        """
        path = Path(filepath)
        
        if not path.exists():
            logger.warning(f"Configuration file {filepath} does not exist")
            return

        # Check if this is a JSONC file
        if path.suffix.lower() in (".jsonc", ".json"):
            self._load_jsonc_format(path)
            self._is_new_format = True
        else:
            # Try to load as YAML (legacy)
            try:
                import yaml
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if data and isinstance(data, dict) and "mappings" in data:
                    self._load_new_format(data)
                    self._is_new_format = True
                else:
                    self._load_legacy_format(data)
                    self._is_new_format = False
            except ImportError:
                logger.error("PyYAML is not installed. Cannot load YAML configuration.")
                raise

    def _load_jsonc_format(self, path: Path) -> None:
        """Load configuration from a JSONC file.
        
        Strips // line comments and /* */ block comments, then parses as JSON.
        """
        logger.info(f"Loading JSONC configuration from {path}")
        
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        self.load_from_content(content)

    def load_from_content(self, content: str) -> None:
        """Load configuration from JSONC text."""
        # Strip comments from JSONC
        json_content = self._strip_jsonc_comments(content)
        
        # Parse JSON
        try:
            data = json.loads(json_content)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSONC configuration: {e}")
            raise

        if not isinstance(data, dict):
            raise ValueError("Configuration must be a JSON object.")

        self._load_new_format(data)
        self._is_new_format = True

    def _strip_jsonc_comments(self, content: str) -> str:
        """Strip // line comments and /* */ block comments from JSONC content.
        
        Args:
            content: The JSONC content with comments.
            
        Returns:
            Clean JSON string without comments.
        """
        # Remove /* */ block comments first
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        # Remove // line comments
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        # Remove leading/trailing whitespace from the result
        return content.strip()

    def _load_new_format(self, data: dict[str, Any]) -> None:
        """Load the new mapping-based configuration format."""
        logger.info(f"Loading new mapping-based configuration (version: {data.get('version', 'unknown')})")
        
        # Load version
        self._config_version = data.get("version", "1.0")
        
        # Load companies
        self._companies = data.get("companies", [])
        logger.info(f"Loaded {len(self._companies)} companies")
        
        # Load document_types
        self._document_types = data.get("document_types", [])
        logger.info(f"Loaded {len(self._document_types)} document types")
        
        # Load additional_fields
        additional_fields_data = data.get("additional_fields", {})
        self._additional_fields = {}
        for group_name, values in additional_fields_data.items():
            self._additional_fields[group_name] = list(values) if isinstance(values, list) else []
        logger.info(f"Loaded {len(self._additional_fields)} additional field groups")
        
        # Load period_formats
        period_formats_data = data.get("period_formats", {})
        self._period_formats = {}
        for name, config in period_formats_data.items():
            self._period_formats[name] = PeriodFormatConfig.from_dict(config)
            logger.info(f"Loaded period format: {name}")
        
        # Load date extraction patterns (simplified: list of regex strings)
        date_patterns_data = data.get("date_extraction", [])
        self._date_extraction_patterns = list(date_patterns_data) if isinstance(date_patterns_data, list) else []
        self._date_extraction_compiled = [re.compile(p) for p in self._date_extraction_patterns]
        logger.info(f"Loaded {len(self._date_extraction_patterns)} date extraction patterns")
        
        # Load mappings (new format: list of {match, set} objects)
        mappings_data = data.get("mappings", [])
        self._mappings = [
            MappingRule.from_dict(m) for m in mappings_data
        ]
        logger.info(f"Loaded {len(self._mappings)} mapping rules: {self._mappings}")
        
        # Load templates
        templates_data = data.get("templates", {})
        for name, config in templates_data.items():
            if isinstance(config, dict):
                template = TemplateConfig.from_dict(name, config)
                self._templates[name] = template
                logger.info(f"Loaded template: {name}")

    def _load_legacy_format(self, data: dict[str, Any]) -> None:
        """Load the legacy template-based configuration format."""
        logger.info("Loading legacy template-based configuration")
        
        if data and isinstance(data, dict):
            for name, config in data.items():
                if isinstance(config, dict):
                    template = TemplateConfig.from_dict(name, config)
                    self._templates[name] = template
                    logger.info(f"Loaded legacy template: {name}")

    def load_from_directory(self, dirpath: str) -> None:
        """Load templates from all JSONC or YAML files in a directory.

        Args:
            dirpath: Path to the directory containing configuration files.
        """
        path = Path(dirpath)
        if not path.exists():
            logger.warning(f"Configuration directory {dirpath} does not exist")
            return

        for config_file in path.glob("*.jsonc") + path.glob("*.json") + path.glob("*.yaml") + path.glob("*.yml"):
            self.load_from_file(str(config_file))

    def is_new_format(self) -> bool:
        """Check if the loaded configuration uses the new mapping-based format."""
        return self._is_new_format

    def get_companies(self) -> list[str]:
        """Get the list of valid companies.

        Returns:
            List of company names.
        """
        return self._companies

    def get_document_types(self) -> list[str]:
        """Get the list of valid document types.

        Returns:
            List of document type names.
        """
        return self._document_types

    def get_date_extraction_patterns(self) -> list[str]:
        """Get the list of date extraction regex pattern strings.

        Returns:
            List of regex pattern strings.
        """
        return self._date_extraction_patterns

    def get_date_extraction_compiled(self) -> list[re.Pattern]:
        """Get the list of compiled date extraction regex patterns.

        Returns:
            List of compiled regex Pattern objects.
        """
        return self._date_extraction_compiled

    def get_mapping_rules(self) -> list[MappingRule]:
        """Get all mapping rules.

        Returns:
            List of MappingRule objects.
        """
        return self._mappings

    def resolve_mapping(self, company: str, document_type: str) -> dict[str, Any]:
        """Resolve the mapping for a specific (company, document_type) tuple.
        
        Processes all rules in order. For each matching rule, applies the set fields.
        Later matches override earlier ones.
        
        Args:
            company: The company name.
            document_type: The document type.
        
        Returns:
            Dict with resolved fields: template, period_format, additional_fields.
            If no mapping found, returns empty dict.
        """
        result: dict[str, Any] = {}
        
        for rule in self._mappings:
            if rule.matches(company, document_type):
                # Apply set fields, overriding previous values
                if rule.set.template is not None:
                    result["template"] = rule.set.template
                if rule.set.period_format is not None:
                    result["period_format"] = rule.set.period_format
                if rule.set.additional_fields:
                    result["additional_fields"] = rule.set.additional_fields.copy()
        
        return result

    def get_template(self, name: str) -> TemplateConfig | None:
        """Get a template by name.

        Args:
            name: The template name.

        Returns:
            The TemplateConfig, or None if not found.
        """
        return self._templates.get(name)

    def list_all_templates(self) -> list[str]:
        """List all template names.

        Returns:
            List of template names.
        """
        return list(self._templates.keys())

    def is_valid_company(self, company: str) -> bool:
        """Check if a company is in the predefined list.

        Args:
            company: The company name to check.

        Returns:
            True if valid, False otherwise.
        """
        return company in self._companies

    def is_valid_document_type(self, document_type: str) -> bool:
        """Check if a document type is in the predefined list.

        Args:
            document_type: The document type to check.

        Returns:
            True if valid, False otherwise.
        """
        return document_type in self._document_types

    def get_additional_fields(self) -> dict[str, list[str]]:
        """Get the dictionary of additional field groups with their possible values.

        Returns:
            Dict mapping field group names to list of possible values.
        """
        return self._additional_fields

    def get_additional_field_values(self, group_name: str) -> list[str]:
        """Get the list of possible values for a specific additional field group.

        Args:
            group_name: The name of the additional field group.

        Returns:
            List of possible values, or empty list if group not found.
        """
        return self._additional_fields.get(group_name, [])

    def get_period_format_config(self, format_name: str) -> PeriodFormatConfig | None:
        """Get the period format configuration by name.

        Args:
            format_name: The name of the period format.

        Returns:
            PeriodFormatConfig if found, None otherwise.
        """
        return self._period_formats.get(format_name)

    def list_period_formats(self) -> list[str]:
        """List configured named period formats."""
        return list(self._period_formats.keys())

    def get_key_pattern(self, template_name: str) -> str | None:
        """Get the key pattern for a template.

        Args:
            template_name: The template name.

        Returns:
            Key pattern string, or None if not found.
        """
        template = self.get_template(template_name)
        if template:
            return template.key_pattern
        return None


def load_config() -> AppConfig:
    """Load application configuration from environment variables.

    Returns:
        AppConfig instance.
    """
    return AppConfig.from_env()


def load_templates(template_path: str | None = None) -> TemplateRegistry:
    """Load template configurations.

    Args:
        template_path: Path to JSONC file or directory. If None, uses TEMPLATE_PATH env var
        or defaults to ./config.jsonc at the root.

    Returns:
        TemplateRegistry with loaded templates.
    """
    registry = TemplateRegistry()

    if template_path is None:
        template_path = os.environ.get("TEMPLATE_PATH", "./config.jsonc")

    if not template_path:
        logger.warning("No template path configured")
        return registry

    path = Path(template_path)
    if path.is_file():
        registry.load_from_file(str(path))
    elif path.is_dir():
        registry.load_from_directory(str(path))
    else:
        logger.warning(f"Template path {template_path} does not exist")

    return registry
