"""Utility helpers for the Ezlo HA Cloud integration."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    ISSUE_TRIAL_ENDING_SOON,
    ISSUE_TRIAL_EXPIRED,
    TRIAL_ENDING_SOON_DAYS,
    SubscriptionStatus,
)

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


def compute_trial_days(trial_ends_at: str | None) -> int | None:
    """Compute remaining trial days from an ISO datetime string."""
    if not trial_ends_at:
        return None
    try:
        end_dt = datetime.fromisoformat(trial_ends_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    now = datetime.now(tz=end_dt.tzinfo)
    remaining = (end_dt - now).days
    return max(remaining, 0)


@callback
def update_trial_issues(
    hass: HomeAssistant,
    status: str | None,
    trial_ends_at: str | None,
    subscribe_url: str | None,
) -> None:
    """Keep the free-trial repair issues in step with the subscription state.

    These issues are how the integration *prompts* the user: Home Assistant
    surfaces them under Settings → Repairs with a badge. ``trial_ending_soon``
    appears once ``TRIAL_ENDING_SOON_DAYS`` or fewer remain, ``trial_expired``
    once the backend reports the trial is over; each links to the central
    subscribe flow through the issue's Learn more button when the URL is
    known. Any other state clears both.
    """
    days = compute_trial_days(trial_ends_at)
    ending_soon = (
        status == SubscriptionStatus.TRIALING
        and days is not None
        and days <= TRIAL_ENDING_SOON_DAYS
    )
    expired = status == SubscriptionStatus.TRIAL_EXPIRED

    if ending_soon:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_TRIAL_ENDING_SOON,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TRIAL_ENDING_SOON,
            translation_placeholders={"days": str(days)},
            learn_more_url=subscribe_url or None,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_TRIAL_ENDING_SOON)

    if expired:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_TRIAL_EXPIRED,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_TRIAL_EXPIRED,
            learn_more_url=subscribe_url or None,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_TRIAL_EXPIRED)


@callback
def clear_trial_issues(hass: HomeAssistant) -> None:
    """Remove both free-trial repair issues (logout / no credentials)."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_TRIAL_ENDING_SOON)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_TRIAL_EXPIRED)
