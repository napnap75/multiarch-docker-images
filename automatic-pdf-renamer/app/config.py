"""Configuration module for Automatic PDF Renamer.

Provides environment variable parsing and template YAML loading.
Supports the new mapping-based configuration structure where:
- companies: predefined list of valid companies
- document_types: predefined list of valid document types
- mappings: (company, document_type) -> template + period rule
- templates: template definitions with key patterns
- date_extraction: regex patterns for finding emission date in text
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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
class PaperlessConfig:
    """Paperless-ngx API configuration."""

    url: str
    token: str
    timeout: int = 30

    @classmethod
    def from_env(cls) -> "PaperlessConfig":
        """Create PaperlessConfig from environment variables."""
        return cls(
            url=os.environ.get("PAPERLESS_URL", ""),
            token=os.environ.get("PAPERLESS_TOKEN", ""),
            timeout=int(os.environ.get("PAPERLESS_TIMEOUT", "30")),
        )

    def validate(self) -> None:
        """Validate that all required fields are set."""
        if not self.url:
            raise ValueError("PAPERLESS_URL environment variable is required")
        if not self.token:
            raise ValueError("PAPERLESS_TOKEN environment variable is required")


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
class PeriodRule:
    """Period rule for mapping-based configuration."""

    granularity: str = "month"
    offset: str = "current"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PeriodRule":
        """Create PeriodRule from dict."""
        if data is None:
            return cls()
        return cls(
            granularity=data.get("granularity", "month"),
            offset=data.get("offset", "current"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"granularity": self.granularity, "offset": self.offset}


@dataclass
class DateExtractionPattern:
    """Date extraction regex pattern for finding emission date in text."""

    pattern: str
    description: str = ""
    compiled: Any = field(default=None, repr=False)

    def __post_init__(self):
        self.compiled = re.compile(self.pattern)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DateExtractionPattern":
        """Create DateExtractionPattern from dict."""
        return cls(
            pattern=data["pattern"],
            description=data.get("description", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"pattern": self.pattern, "description": self.description}


@dataclass
class CompanyTypeMapping:
    """Mapping of (company, document_type) to template and period rule."""

    company: str
    document_type: str
    template: str
    period: PeriodRule = field(default_factory=PeriodRule)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompanyTypeMapping":
        """Create CompanyTypeMapping from dict."""
        return cls(
            company=data["company"],
            document_type=data["document_type"],
            template=data["template"],
            period=PeriodRule.from_dict(data.get("period")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "document_type": self.document_type,
            "template": self.template,
            "period": self.period.to_dict(),
        }

    def matches(self, company: str, document_type: str) -> bool:
        """Check if this mapping matches the given company and document type."""
        return self.company == company and self.document_type == document_type


@dataclass
class TemplateConfig:
    """Template configuration with key pattern."""

    name: str
    key_pattern: str = ""
    # document_family removed as per user request

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

    s3: S3Config = field(default_factory=S3Config)
    paperless: PaperlessConfig | None = None
    poller: PollerConfig = field(default_factory=PollerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    debug: bool = False
    sqlite_path: str = "./renamer.db"

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Create AppConfig from environment variables."""
        debug = os.environ.get("DEBUG", "").lower() in ("true", "1", "yes")

        config = cls(
            s3=S3Config.from_env(),
            paperless=None,  # Only needed for import script
            poller=PollerConfig.from_env(),
            classifier=ClassifierConfig.from_env(),
            debug=debug,
            sqlite_path=os.environ.get("SQLITE_PATH", "./renamer.db"),
        )

        # Only load paperless config if PAPERLESS_URL is set
        if os.environ.get("PAPERLESS_URL"):
            config.paperless = PaperlessConfig.from_env()

        return config

    def validate(self) -> None:
        """Validate the configuration."""
        self.s3.validate()
        if self.paperless:
            self.paperless.validate()


@dataclass
class LegacyTemplateConfig:
    """Legacy template configuration from YAML (for backward compatibility)."""

    name: str
    document_family: str = ""
    document_types: list[str] = field(default_factory=list)
    period: dict[str, Any] = field(default_factory=dict)
    optional_fields: dict[str, Any] = field(default_factory=dict)
    key_pattern: str = ""
    target_prefix: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "LegacyTemplateConfig":
        """Create a LegacyTemplateConfig from a YAML dict."""
        return cls(
            name=name,
            document_family=data.get("document_family", name),
            document_types=data.get("document_types", []),
            period=data.get("period", {}),
            optional_fields=data.get("optional_fields", {}),
            key_pattern=data.get("key_pattern", ""),
            target_prefix=data.get("target_prefix", ""),
        )


class TemplateRegistry:
    """Registry for template configurations.
    
    Supports both the new mapping-based configuration and legacy template config.
    The new structure includes:
    - companies: list of valid company names
    - document_types: list of valid document type names
    - mappings: list of (company, document_type) -> template + period mappings
    - templates: template definitions with key patterns
    - date_extraction: list of regex patterns for finding emission date
    """

    def __init__(self):
        self._templates: dict[str, TemplateConfig] = {}
        self._legacy_templates: dict[str, LegacyTemplateConfig] = {}
        self._companies: list[str] = []
        self._document_types: list[str] = []
        self._mappings: list[CompanyTypeMapping] = []
        self._date_extraction_patterns: list[DateExtractionPattern] = []
        self._config_version: str = "1.0"
        self._is_new_format: bool = False

    def load_from_file(self, filepath: str) -> None:
        """Load configuration from a YAML file.

        Supports both new mapping-based format and legacy template format.

        Args:
            filepath: Path to the YAML file.
        """
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or not isinstance(data, dict):
            logger.warning(f"No configuration found in {filepath}")
            return

        # Check if this is the new format (has mappings key)
        if "mappings" in data:
            self._load_new_format(data)
            self._is_new_format = True
        else:
            # Legacy format - load as templates
            self._load_legacy_format(data)
            self._is_new_format = False

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
        
        # Load date extraction patterns
        date_patterns_data = data.get("date_extraction", [])
        self._date_extraction_patterns = [
            DateExtractionPattern.from_dict(p) for p in date_patterns_data
        ]
        logger.info(f"Loaded {len(self._date_extraction_patterns)} date extraction patterns")
        
        # Load mappings
        mappings_data = data.get("mappings", [])
        self._mappings = [
            CompanyTypeMapping.from_dict(m) for m in mappings_data
        ]
        logger.info(f"Loaded {len(self._mappings)} company-type mappings")
        
        # Load templates
        templates_data = data.get("templates", {})
        for name, config in templates_data.items():
            template = TemplateConfig.from_dict(name, config)
            self._templates[name] = template
            logger.info(f"Loaded template: {name}")

    def _load_legacy_format(self, data: dict[str, Any]) -> None:
        """Load the legacy template-based configuration format."""
        logger.info("Loading legacy template-based configuration")
        
        for name, config in data.items():
            template = LegacyTemplateConfig.from_dict(name, config)
            self._legacy_templates[name] = template
            logger.info(f"Loaded legacy template: {name}")

    def load_from_directory(self, dirpath: str) -> None:
        """Load templates from all YAML files in a directory.

        Args:
            dirpath: Path to the directory containing YAML files.
        """
        path = Path(dirpath)
        if not path.exists():
            logger.warning(f"Configuration directory {dirpath} does not exist")
            return

        for yaml_file in path.glob("*.yaml") + path.glob("*.yml"):
            self.load_from_file(str(yaml_file))

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

    def get_date_extraction_patterns(self) -> list[DateExtractionPattern]:
        """Get the list of date extraction regex patterns.

        Returns:
            List of DateExtractionPattern objects.
        """
        return self._date_extraction_patterns

    def get_mapping(self, company: str, document_type: str) -> CompanyTypeMapping | None:
        """Get the mapping for a specific (company, document_type) tuple.

        Args:
            company: The company name.
            document_type: The document type.

        Returns:
            CompanyTypeMapping if found, None otherwise.
        """
        for mapping in self._mappings:
            if mapping.matches(company, document_type):
                return mapping
        return None

    def get_template(self, name: str) -> TemplateConfig | None:
        """Get a template by name.

        Args:
            name: The template name.

        Returns:
            The TemplateConfig, or None if not found.
        """
        return self._templates.get(name)

    def get_legacy_template(self, name: str) -> LegacyTemplateConfig | None:
        """Get a legacy template by name.

        Args:
            name: The template name.

        Returns:
            The LegacyTemplateConfig, or None if not found.
        """
        return self._legacy_templates.get(name)

    def list_all_templates(self) -> list[str]:
        """List all template names.

        Returns:
            List of template names.
        """
        return list(self._templates.keys())

    def list_all_mappings(self) -> list[CompanyTypeMapping]:
        """List all company-type mappings.

        Returns:
            List of CompanyTypeMapping objects.
        """
        return self._mappings

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

    def get_period_rule(self, template_name: str) -> "PeriodRule" | None:
        """Get the period rule for a template.

        In the new format, period rules come from mappings, not templates.
        This method is kept for backward compatibility.

        Args:
            template_name: The template name.

        Returns:
            PeriodRule from the first matching mapping, or None if not found.
        """
        # In new format, period rules are in mappings, not templates
        # For backward compatibility, look for a mapping with this template
        for mapping in self._mappings:
            if mapping.template == template_name:
                return PeriodRule(
                    granularity=mapping.period.granularity,
                    offset=mapping.period.offset,
                )
        return None

    def get_period_rule_for_mapping(
        self, company: str, document_type: str
    ) -> "PeriodRule" | None:
        """Get the period rule for a specific (company, document_type) mapping.

        Args:
            company: The company name.
            document_type: The document type.

        Returns:
            PeriodRule if mapping found, None otherwise.
        """
        mapping = self.get_mapping(company, document_type)
        if mapping:
            return PeriodRule(
                granularity=mapping.period.granularity,
                offset=mapping.period.offset,
            )
        return None

    def get_template_for_mapping(
        self, company: str, document_type: str
    ) -> str | None:
        """Get the template name for a specific (company, document_type) mapping.

        Args:
            company: The company name.
            document_type: The document type.

        Returns:
            Template name if mapping found, None otherwise.
        """
        mapping = self.get_mapping(company, document_type)
        if mapping:
            return mapping.template
        return None

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
        template_path: Path to YAML file or directory. If None, uses TEMPLATE_PATH env var.

    Returns:
        TemplateRegistry with loaded templates.
    """
    registry = TemplateRegistry()

    if template_path is None:
        template_path = os.environ.get("TEMPLATE_PATH", "./config/templates.yaml")

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


def load_config() -> AppConfig:
    """Load application configuration from environment variables.

    Returns:
        AppConfig instance.
    """
    return AppConfig.from_env()
