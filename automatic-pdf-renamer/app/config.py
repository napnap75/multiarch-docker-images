"""Configuration module for Automatic PDF Renamer.

Provides environment variable parsing and template YAML loading.
"""

import logging
import os
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
class TemplateConfig:
    """Template configuration from YAML."""

    name: str
    document_family: str
    document_types: list[str]
    period: dict[str, Any]
    optional_fields: dict[str, Any] = field(default_factory=dict)
    key_pattern: str = ""
    target_prefix: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "TemplateConfig":
        """Create a TemplateConfig from a YAML dict."""
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
    """Registry for template configurations."""

    def __init__(self):
        self._templates: dict[str, TemplateConfig] = {}

    def load_from_file(self, filepath: str) -> None:
        """Load templates from a YAML file.

        Args:
            filepath: Path to the YAML file.
        """
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or not isinstance(data, dict):
            logger.warning(f"No templates found in {filepath}")
            return

        for name, config in data.items():
            template = TemplateConfig.from_dict(name, config)
            self._templates[name] = template
            logger.info(f"Loaded template: {name}")

    def load_from_directory(self, dirpath: str) -> None:
        """Load templates from all YAML files in a directory.

        Args:
            dirpath: Path to the directory containing YAML files.
        """
        path = Path(dirpath)
        if not path.exists():
            logger.warning(f"Template directory {dirpath} does not exist")
            return

        for yaml_file in path.glob("*.yaml") + path.glob("*.yml"):
            self.load_from_file(str(yaml_file))

    def get(self, name: str) -> TemplateConfig | None:
        """Get a template by name.

        Args:
            name: The template name.

        Returns:
            The TemplateConfig, or None if not found.
        """
        return self._templates.get(name)

    def list_all(self) -> list[str]:
        """List all template names.

        Returns:
            List of template names.
        """
        return list(self._templates.keys())

    def get_period_rule(self, template_name: str) -> "PeriodRule" | None:
        """Get the period rule for a template.

        Args:
            template_name: The template name.

        Returns:
            PeriodRule, or None if not configured.
        """
        from .period import PeriodRule, PeriodGranularity, PeriodOffset

        template = self.get(template_name)
        if not template or not template.period:
            return None

        return PeriodRule(
            granularity=PeriodGranularity(template.period.get("granularity", "month")),
            offset=PeriodOffset(template.period.get("offset", "current")),
        )


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
        template_path = os.environ.get("TEMPLATE_PATH", "./templates.yaml")

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
