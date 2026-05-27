"""Tests for the trusted-proxy configuration utility."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.ezlohacloud.utils import (
    _needs_trusted_proxy,
    ensure_trusted_proxy_config,
)
from homeassistant.core import HomeAssistant

# ── _needs_trusted_proxy (pure logic) ────────────────────────────────


def test_needs_trusted_proxy_already_configured() -> None:
    """No change needed when both forwarded + 127.0.0.1 are already present."""
    existing = (
        "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 127.0.0.1\n"
    )
    assert _needs_trusted_proxy(existing) is None


def test_needs_trusted_proxy_already_configured_capital_true() -> None:
    """Accept `True` capitalization for use_x_forwarded_for."""
    existing = (
        "http:\n  use_x_forwarded_for: True\n  trusted_proxies:\n    - 127.0.0.1\n"
    )
    assert _needs_trusted_proxy(existing) is None


def test_needs_trusted_proxy_empty_config() -> None:
    """Empty configuration.yaml gets the whole http block appended."""
    result = _needs_trusted_proxy("")
    assert result is not None
    assert "http:" in result
    assert "use_x_forwarded_for: true" in result
    assert "trusted_proxies:" in result
    assert "- 127.0.0.1" in result


def test_needs_trusted_proxy_no_http_block_appends_block() -> None:
    """When http: section doesn't exist, the block is appended at the end."""
    existing = "default_config:\n\nlogger:\n  default: info\n"
    result = _needs_trusted_proxy(existing)
    assert result is not None
    assert result.startswith(existing)
    assert "http:\n  use_x_forwarded_for: true" in result


def test_needs_trusted_proxy_http_exists_without_trusted_proxies() -> None:
    """Adds use_x_forwarded_for + trusted_proxies under an existing http: block."""
    existing = "http:\n  cors_allowed_origins:\n    - https://example.com\n"
    result = _needs_trusted_proxy(existing)
    assert result is not None
    assert "use_x_forwarded_for: true" in result
    assert "  trusted_proxies:\n" in result
    assert "    - 127.0.0.1\n" in result


def test_needs_trusted_proxy_trusted_proxies_missing_localhost() -> None:
    """Appends 127.0.0.1 to an existing trusted_proxies list missing it."""
    existing = (
        "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 192.168.1.0/24\n"
    )
    result = _needs_trusted_proxy(existing)
    assert result is not None
    # Existing entry preserved
    assert "- 192.168.1.0/24" in result
    # New entry added
    assert "- 127.0.0.1" in result


def test_needs_trusted_proxy_ignores_commented_http() -> None:
    """A commented http: line is not treated as the http block."""
    existing = "# http:\n# This is a comment, not a real block\n"
    result = _needs_trusted_proxy(existing)
    assert result is not None
    # Block should be appended at the end (no real http: block found)
    assert "http:\n  use_x_forwarded_for: true" in result


# ── ensure_trusted_proxy_config (file I/O) ──────────────────────────


def test_ensure_trusted_proxy_config_missing_file(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Returns False without writing anything if configuration.yaml is absent."""
    hass.config.config_dir = str(tmp_path)

    assert ensure_trusted_proxy_config(hass) is False
    assert not (tmp_path / "configuration.yaml").exists()


def test_ensure_trusted_proxy_config_no_change_needed(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Returns False and leaves the file untouched when already configured."""
    config_file = tmp_path / "configuration.yaml"
    original = (
        "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 127.0.0.1\n"
    )
    config_file.write_text(original)
    hass.config.config_dir = str(tmp_path)

    assert ensure_trusted_proxy_config(hass) is False
    assert config_file.read_text() == original


def test_ensure_trusted_proxy_config_writes_changes(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Returns True and writes the updated config to disk when changes apply."""
    config_file = tmp_path / "configuration.yaml"
    config_file.write_text("default_config:\n")
    hass.config.config_dir = str(tmp_path)

    assert ensure_trusted_proxy_config(hass) is True
    new_content = config_file.read_text()
    assert "default_config:" in new_content
    assert "http:" in new_content
    assert "- 127.0.0.1" in new_content


def test_ensure_trusted_proxy_config_is_idempotent(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Running twice doesn't add duplicate entries."""
    config_file = tmp_path / "configuration.yaml"
    config_file.write_text("default_config:\n")
    hass.config.config_dir = str(tmp_path)

    assert ensure_trusted_proxy_config(hass) is True
    first_content = config_file.read_text()

    # Second run sees the config is already correct and does nothing
    assert ensure_trusted_proxy_config(hass) is False
    assert config_file.read_text() == first_content
