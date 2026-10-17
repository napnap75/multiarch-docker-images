"""Regression tests for staged ML-assisted mapping resolution."""

import unittest

from app.config import TemplateRegistry


class StagedMappingTests(unittest.TestCase):
    def setUp(self):
        self.registry = TemplateRegistry()
        self.registry.load_from_content(
            """
            {
              "companies": ["Example Energy"],
              "document_types": ["invoice"],
              "additional_fields": {
                "accounts": ["ACCOUNT-A", "ACCOUNT-B"]
              },
              "period_formats": {
                "monthly": {"granularity": "month", "format": "{year}-{month}", "offset": "current"},
                "yearly": {"granularity": "year", "format": "{year}", "offset": "current"}
              },
              "mappings": [
                {
                  "match": {},
                  "set": {
                    "template": "default",
                    "period_format": "monthly"
                  }
                },
                {
                  "match": {
                    "company": "Example Energy",
                    "document_type": "invoice"
                  },
                  "set": {
                    "additional_fields": ["accounts"]
                  }
                },
                {
                  "match": {
                    "company": "Example Energy",
                    "document_type": "invoice",
                    "additional_fields": {"accounts": "ACCOUNT-B"}
                  },
                  "set": {
                    "template": "special_invoice",
                    "period_format": "yearly"
                  }
                }
              ],
              "templates": {
                "default": {"key_pattern": "{emitting_company}/{period}/{original_filename}"},
                "special_invoice": {"key_pattern": "{emitting_company}/{period}/{original_filename}"}
              }
            }
            """
        )

    def test_groups_are_resolved_before_value_dependent_template_mapping(self):
        groups = self.registry.get_additional_field_groups("Example Energy", "invoice")
        self.assertEqual(groups, ["accounts"])

        resolved = self.registry.resolve_mapping(
            "Example Energy", "invoice", {"accounts": "ACCOUNT-B"}
        )
        self.assertEqual(resolved["template"], "special_invoice")
        self.assertEqual(resolved["period_format"], "yearly")

    def test_nonmatching_optional_value_keeps_default_mapping(self):
        resolved = self.registry.resolve_mapping(
            "Example Energy", "invoice", {"accounts": "ACCOUNT-A"}
        )
        self.assertEqual(resolved["template"], "default")
        self.assertEqual(resolved["period_format"], "monthly")


if __name__ == "__main__":
    unittest.main()
