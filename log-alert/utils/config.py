"""Configuration loading and validation utilities."""

import json
import jsonschema
import logging
import os
import sys
from typing import Dict, Any

logger = logging.getLogger("log-alert")


def load_config(config_path: str) -> Dict[str, Any]:
    """Load the configuration from a JSON file and validate it with JSON Schema."""
    try:
        with open(config_path, 'r') as config_file:
            # read JSON first
            config = json.load(config_file)
            # Perform schema validation if jsonschema is available
            validate_config_with_schema(config)
            # Update config to load env variable where required
            return update_config_from_env(config)
    except FileNotFoundError:
        logger.error(f"Error: Configuration file '{config_path}' not found.")
        sys.exit(1)
    except json.JSONDecodeError:
        logger.error(f"Error: Invalid JSON in configuration file '{config_path}'.")
        sys.exit(1)


def validate_config_with_schema(config: Dict[str, Any]) -> None:
    """Validate a loaded config dict against config.schema.json if jsonschema is installed."""
    # Find the schema file relative to the log-alert.py location
    schema_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.schema.json')
    try:
        with open(schema_path, 'r') as sf:
            schema = json.load(sf)
        jsonschema.validate(instance=config, schema=schema)
    except FileNotFoundError:
        logger.error(f"Schema file '{schema_path}' not found")
    except jsonschema.exceptions.ValidationError as e:
        logger.error(f"Configuration validation error: {e.message}")
        logger.error("Detailed error:", e)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error while validating configuration: {e}")
        sys.exit(1)


def update_config_from_env(config: Dict[str, Any]) -> Dict[str, Any]:
    """Update config values from environment variables if specified."""
    for key, value in list(config.items()):
        if isinstance(value, dict):
            config[key] = update_config_from_env(value)
        elif isinstance(value, list):
            config[key] = [update_config_from_env(item) for item in value]
        elif isinstance(value, str) and key.endswith("-from-env"):
            new_key = key[:-9]  # Remove '-from-env'
            config[new_key] = value.format_map(os.environ)
            del config[key]
            return update_config_from_env(config) # re-evaluate in case of nested env vars
    return config
