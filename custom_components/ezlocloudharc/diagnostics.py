"""Diagnostics support for Ezlo HA Cloud."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import FRPC_VERSION
from .frp_helpers import get_frp_binary_path, get_frp_config_path
from .models import EzloConfigEntry
from .utils import is_trusted_proxy_configured

REDACT = {
    "auth_token",
    "tunnel_token",
    "email",
    "username",
    "password",
    "token",
    "uuid",
    "ezlo_id",
    # The central subscribe URLs embed the user's email as a query param.
    "checkout_url",
    "subscribe_url",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EzloConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a single config entry."""
    runtime = entry.runtime_data
    process_running = False
    pid: int | None = None
    if runtime.process is not None:
        process_running = runtime.process.returncode is None
        pid = runtime.process.pid

    binary_path = get_frp_binary_path(hass)
    config_path = get_frp_config_path(hass)

    binary_version = await _read_binary_version(binary_path)

    trusted_proxy_ok = await hass.async_add_executor_job(
        is_trusted_proxy_configured, hass
    )

    return {
        "entry": async_redact_data(dict(entry.data), REDACT),
        "expected_frpc_version": FRPC_VERSION,
        "installed_frpc_version": binary_version,
        "binary_path": str(binary_path),
        "binary_exists": binary_path.is_file(),
        "config_path": str(config_path),
        "config_exists": config_path.is_file(),
        "process_running": process_running,
        "process_pid": pid,
        "is_connected": runtime.is_connected,
        "trusted_proxy_configured": trusted_proxy_ok,
    }


async def _read_binary_version(binary_path: Path) -> str | None:
    """Return the installed FRPC version string, or None."""
    if not binary_path.is_file():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary_path),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return None
    except OSError:
        return None
    return stdout.decode(errors="replace").strip() or None
