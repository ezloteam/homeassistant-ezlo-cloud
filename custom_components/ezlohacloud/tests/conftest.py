"""Shared pytest fixtures for the Ezlo HA Cloud test suite."""

from __future__ import annotations

from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Auto-enable Home Assistant's custom-component loader for every test.

    Required by pytest-homeassistant-custom-component so that
    ``custom_components.ezlohacloud`` is discoverable by HA's config-entry
    machinery (manifest.json, config_flow, translations, etc.).
    """
    yield
