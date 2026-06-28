"""Typed containers for Ezlo HA Cloud config entry and runtime data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from homeassistant.config_entries import ConfigEntry


class EzloUserData(TypedDict, total=False):
    """Authenticated user attributes stored on the config entry."""

    uuid: str
    username: str
    email: str
    ezlo_id: int | str


class EzloConfigData(TypedDict, total=False):
    """Shape of `ConfigEntry.data` for this integration."""

    auth_token: str | None
    tunnel_token: str | None
    user: EzloUserData
    is_logged_in: bool
    subscription_status: str | None
    trial_ends_at: str | None
    payment_required: bool
    server_name: str
    subdomain: str
    api_uri: str


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
    integration_config: dict[str, str] | None = None
    subscription_cache: tuple[float, dict[str, str | bool]] | None = None
    is_connected: bool = False
    last_unavailable_logged: bool = False
    unsub_callbacks: list[object] = field(default_factory=list)


type EzloConfigEntry = ConfigEntry[EzloRuntimeData]
