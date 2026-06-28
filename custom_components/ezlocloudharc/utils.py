"""Utility helpers for the Ezlo HA Cloud integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _get_config_path(hass: HomeAssistant) -> Path:
    """Return the path to ``configuration.yaml``."""
    return Path(hass.config.config_dir) / "configuration.yaml"


def is_trusted_proxy_configured(hass: HomeAssistant) -> bool:
    """Return True if the ``http.trusted_proxies`` block already lists 127.0.0.1.

    This is intentionally a *detection* helper; the integration must not
    modify the user's configuration.yaml. When the block is missing the
    integration raises a repair issue with the snippet to copy.
    """
    config_path = _get_config_path(hass)
    if not config_path.is_file():
        _LOGGER.debug("configuration.yaml not found at %s", config_path)
        return False

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as err:
        _LOGGER.debug("Could not read configuration.yaml: %s", err)
        return False

    has_forwarded = (
        "use_x_forwarded_for: true" in text
        or "use_x_forwarded_for: True" in text
    )
    has_trusted = "127.0.0.1" in text and "trusted_proxies" in text
    return has_forwarded and has_trusted
