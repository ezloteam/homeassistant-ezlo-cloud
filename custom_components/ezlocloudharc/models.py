"""Typed containers for Ezlo HA Cloud config entry and runtime data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

from homeassistant.config_entries import ConfigEntry


class EzloUserData(TypedDict, total=False):
    """Authenticated user attributes stored on the config entry."""

    uuid: str
    username: str
    email: str
    ezlo_id: int | str


class EzloAppData(TypedDict):
    """The Ezlo app connection stored on the entry (see ezlo_app.py).

    Holds identifiers only — the access token itself lives in Home Assistant's
    auth store and on the platform, never on the entry.
    """

    ha_user_id: str
    refresh_token_id: str
    pk_account: int
    serial: str
    ha_url: str
    # The property the hub was added to. Absent on connections made before the
    # property picker, or against a platform that does not report it.
    property_id: NotRequired[str]
    property_name: NotRequired[str]


class EzloConfigData(TypedDict, total=False):
    """Shape of `ConfigEntry.data` for this integration."""

    auth_token: str | None
    tunnel_token: str | None
    user: EzloUserData
    is_logged_in: bool
    subscription_status: str | None
    trial_ends_at: str | None
    payment_required: bool
    # Central subscribe URL from the backend — present while payment is required
    # and during a free trial (subscribe early). Feeds the repair issues' Learn
    # more button. Redacted in diagnostics (embeds the account email).
    subscribe_url: str | None
    server_name: str
    subdomain: str
    api_uri: str
    property_uri: str
    ezlo_app: EzloAppData


@dataclass
class EzloRuntimeData:
    """Runtime state for a single Ezlo HA Cloud config entry.

    Lives on `entry.runtime_data` for the lifetime of the entry. Holds the
    FRPC subprocess handle plus any caches the options flow needs to share
    with `async_setup_entry` without going through `hass.data`.
    """

    binary_path: Path | None = None
    config_path: Path | None = None
    process: asyncio.subprocess.Process | None = None
    watchdog_task: asyncio.Task[None] | None = None
    payment_poll_task: asyncio.Task[None] | None = None
    subscription_cache: tuple[float, dict[str, str | bool]] | None = None
    is_connected: bool = False
    last_unavailable_logged: bool = False


type EzloConfigEntry = ConfigEntry[EzloRuntimeData]
