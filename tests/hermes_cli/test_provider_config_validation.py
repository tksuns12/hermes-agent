"""Tests for providers config entry validation and normalization.

Covers Issue #9332: camelCase keys silently ignored, non-URL strings
accepted as base_url, and unknown keys go unreported.
"""

import logging
from unittest.mock import patch

import pytest

from hermes_cli.config import _normalize_custom_provider_entry, logger


class TestNormalizeCustomProviderEntry:
    """Tests for _normalize_custom_provider_entry validation."""

    def test_valid_entry_snake_case(self):
        """Standard snake_case entry should normalize correctly."""
        entry = {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="myhost")
        assert result is not None
        assert result["name"] == "myhost"
        assert result["base_url"] == "https://api.example.com/v1"
        assert result["api_key"] == "sk-test-key"

    def test_camel_case_api_key_mapped(self):
        """camelCase apiKey should be auto-mapped to api_key."""
        entry = {
            "base_url": "https://api.example.com/v1",
            "apiKey": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="myhost")
        assert result is not None
        assert result["api_key"] == "sk-test-key"

    def test_camel_case_base_url_mapped(self):
        """camelCase baseUrl should be auto-mapped to base_url."""
        entry = {
            "baseUrl": "https://api.example.com/v1",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="myhost")
        assert result is not None
        assert result["base_url"] == "https://api.example.com/v1"

    def test_non_url_api_field_rejected(self):
        """Non-URL string in 'api' field should be skipped with a warning."""
        entry = {
            "api": "openai-reverse-proxy",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="nvidia")
        # Should return None because no valid URL was found
        assert result is None

    def test_valid_url_in_api_field_accepted(self):
        """Valid URL in 'api' field should still be accepted."""
        entry = {
            "api": "https://integrate.api.nvidia.com/v1",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="nvidia")
        assert result is not None
        assert result["base_url"] == "https://integrate.api.nvidia.com/v1"

    def test_base_url_preferred_over_api(self):
        """base_url should be checked before api field."""
        entry = {
            "base_url": "https://correct.example.com/v1",
            "api": "https://wrong.example.com/v1",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is not None
        assert result["base_url"] == "https://correct.example.com/v1"

    def test_camel_case_mapping_does_not_mutate_input(self):
        """Normalization should not mutate caller-owned config dicts."""
        entry = {
            "baseUrl": "https://api.example.com/v1",
            "apiKey": "sk-test-key",
        }

        result = _normalize_custom_provider_entry(entry, provider_key="test")

        assert result is not None
        assert result["base_url"] == "https://api.example.com/v1"
        assert result["api_key"] == "sk-test-key"
        assert "base_url" not in entry
        assert "api_key" not in entry

    def test_unknown_keys_logged(self, caplog):
        """Unknown config keys should produce a warning."""
        entry = {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-key",
            "unknownField": "value",
            "anotherBad": 42,
        }
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is not None
        assert any("unknown config keys" in r.message.lower() for r in caplog.records)

    def test_camel_case_warning_logged(self, caplog):
        """camelCase alias mapping should produce a warning."""
        entry = {
            "baseUrl": "https://api.example.com/v1",
            "apiKey": "sk-test-key",
        }
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is not None
        camel_warnings = [r for r in caplog.records if "camelcase" in r.message.lower() or "auto-mapped" in r.message.lower()]
        assert len(camel_warnings) >= 1

    def test_warnings_emit_via_module_logger(self):
        """Normalization warnings should go through the module logger."""
        entry = {
            "baseUrl": "https://api.example.com/v1",
            "api_key": "sk-test-key",
            "unknownField": "value",
        }

        with patch.object(logger, "warning") as warning_mock:
            result = _normalize_custom_provider_entry(entry, provider_key="test")

        assert result is not None
        assert warning_mock.call_count >= 2
        logged_messages = [call.args[0] for call in warning_mock.call_args_list]
        assert any("camelCase key" in message for message in logged_messages)
        assert any("unknown config keys ignored" in message for message in logged_messages)

    def test_snake_case_takes_precedence_over_camel(self):
        """If both snake_case and camelCase exist, snake_case wins."""
        entry = {
            "api_key": "snake-key",
            "apiKey": "camel-key",
            "base_url": "https://api.example.com/v1",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is not None
        assert result["api_key"] == "snake-key"

    def test_non_dict_returns_none(self):
        """Non-dict entry should return None."""
        assert _normalize_custom_provider_entry("not-a-dict") is None
        assert _normalize_custom_provider_entry(42) is None
        assert _normalize_custom_provider_entry(None) is None

    def test_no_url_returns_none(self):
        """Entry with no valid URL in any field should return None."""
        entry = {
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is None

    def test_no_name_returns_none(self):
        """Entry with no name and no provider_key should return None."""
        entry = {
            "base_url": "https://api.example.com/v1",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="")
        assert result is None
