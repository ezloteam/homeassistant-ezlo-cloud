"""Utility helpers for Ezlo HA Cloud integration."""

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

TRUSTED_PROXY_BLOCK = """
# Added by Ezlo HA Cloud integration for frpc reverse proxy
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
"""

TRUSTED_PROXY_ENTRY = "    - 127.0.0.1"


def _get_config_path(hass: HomeAssistant) -> Path:
    """Return the path to configuration.yaml."""
    return Path(hass.config.config_dir) / "configuration.yaml"


def _needs_trusted_proxy(config_text: str) -> str | None:
    """Check if configuration.yaml needs trusted proxy config.

    Returns None if already configured, or the updated config text if changes
    are needed.
    """
    # Check if use_x_forwarded_for and 127.0.0.1 trusted proxy are already set
    has_forwarded = (
        "use_x_forwarded_for: true" in config_text
        or "use_x_forwarded_for: True" in config_text
    )
    has_trusted = "127.0.0.1" in config_text and "trusted_proxies" in config_text

    if has_forwarded and has_trusted:
        return None

    # Check if trusted_proxies key exists but just missing 127.0.0.1
    has_trusted_key = "trusted_proxies" in config_text

    lines = config_text.splitlines(keepends=True)
    new_lines = []
    i = 0
    found_http = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Find top-level "http:" block (not indented, not commented)
        if (
            stripped == "http:"
            and not line[0].isspace()
            and not stripped.startswith("#")
        ):
            found_http = True
            new_lines.append(line)
            i += 1

            # Walk through existing http block lines (indented or blank)
            inserted_proxy = False
            while i < len(lines) and (lines[i].strip() == "" or lines[i][0].isspace()):
                current = lines[i]
                new_lines.append(current)

                # If trusted_proxies exists, add 127.0.0.1 after the last
                # "    - x.x.x.x" entry under it
                if (
                    not inserted_proxy
                    and has_trusted_key
                    and "trusted_proxies" in current.strip()
                ):
                    i += 1
                    # Collect existing proxy entries
                    while i < len(lines) and lines[i].strip().startswith("- "):
                        new_lines.append(lines[i])
                        i += 1
                    # Append 127.0.0.1 after existing entries
                    new_lines.append("    - 127.0.0.1\n")
                    inserted_proxy = True
                    continue

                i += 1

            # Add missing entries at the end of the http block
            if not has_forwarded:
                new_lines.append("  use_x_forwarded_for: true\n")
            if not has_trusted_key:
                # No trusted_proxies key at all — add the whole section
                new_lines.append("  trusted_proxies:\n")
                new_lines.append("    - 127.0.0.1\n")

            continue

        new_lines.append(line)
        i += 1

    # No http: block found at all — append the whole block
    if not found_http:
        new_lines.append(TRUSTED_PROXY_BLOCK)

    return "".join(new_lines)


def ensure_trusted_proxy_config(hass: HomeAssistant) -> bool:
    """Ensure configuration.yaml has trusted proxy settings for frpc.

    Returns True if changes were made (restart needed).
    """
    config_path = _get_config_path(hass)

    if not config_path.is_file():
        _LOGGER.warning("Configuration file not found at %s", config_path)
        return False

    config_text = config_path.read_text(encoding="utf-8")
    updated = _needs_trusted_proxy(config_text)

    if updated is None:
        _LOGGER.debug("Trusted proxy config already present in configuration.yaml")
        return False

    # Write updated config
    config_path.write_text(updated, encoding="utf-8")
    _LOGGER.info(
        "Updated configuration.yaml with trusted proxy settings for frpc. "
        "A restart is required for changes to take effect"
    )
    return True
