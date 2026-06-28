"""Tests for the trusted-proxy detection helper."""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.ezlocloudharc.utils import is_trusted_proxy_configured


def test_is_trusted_proxy_configured_missing_file(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Returns False if configuration.yaml does not exist."""
    hass.config.config_dir = str(tmp_path)
    assert is_trusted_proxy_configured(hass) is False


def test_is_trusted_proxy_configured_lowercase_true(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Returns True when forwarded:true + trusted_proxies 127.0.0.1 are present."""
    config_file = tmp_path / "configuration.yaml"
    config_file.write_text(
        "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 127.0.0.1\n"
    )
    hass.config.config_dir = str(tmp_path)
    assert is_trusted_proxy_configured(hass) is True


def test_is_trusted_proxy_configured_capital_true(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Accepts `True` capitalization for use_x_forwarded_for."""
    config_file = tmp_path / "configuration.yaml"
    config_file.write_text(
        "http:\n  use_x_forwarded_for: True\n  trusted_proxies:\n    - 127.0.0.1\n"
    )
    hass.config.config_dir = str(tmp_path)
    assert is_trusted_proxy_configured(hass) is True


def test_is_trusted_proxy_configured_missing_forwarded(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Returns False when trusted_proxies is present but x_forwarded_for isn't."""
    config_file = tmp_path / "configuration.yaml"
    config_file.write_text(
        "http:\n  trusted_proxies:\n    - 127.0.0.1\n"
    )
    hass.config.config_dir = str(tmp_path)
    assert is_trusted_proxy_configured(hass) is False


def test_is_trusted_proxy_configured_missing_localhost(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Returns False when trusted_proxies omits 127.0.0.1."""
    config_file = tmp_path / "configuration.yaml"
    config_file.write_text(
        "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 10.0.0.1\n"
    )
    hass.config.config_dir = str(tmp_path)
    assert is_trusted_proxy_configured(hass) is False


def test_is_trusted_proxy_configured_empty_file(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Returns False for an empty configuration.yaml."""
    config_file = tmp_path / "configuration.yaml"
    config_file.write_text("")
    hass.config.config_dir = str(tmp_path)
    assert is_trusted_proxy_configured(hass) is False


def test_is_trusted_proxy_configured_oserror_reading(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """OSError while reading configuration.yaml is treated as not-configured."""
    from unittest.mock import patch

    config_file = tmp_path / "configuration.yaml"
    config_file.write_text("anything")
    hass.config.config_dir = str(tmp_path)

    with patch.object(type(config_file), "read_text", side_effect=OSError("denied")):
        assert is_trusted_proxy_configured(hass) is False
