"""Ezlo HA Cloud FRPC tunnel helpers.

The tunnel is run as a child of the Home Assistant event loop via
``asyncio.create_subprocess_exec`` and its lifecycle is tracked on the
config entry's ``runtime_data`` (no ``hass.data`` use).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import aiohttp
from aiohttp import ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from tomlkit import aot, document, dumps, table

from .const import DEFAULT_API_URI
from .exceptions import (
    EzloApiUnexpectedResponseError,
    EzloApiUnreachableError,
    EzloAuthError,
    EzloSubscriptionExpiredError,
    FrpcSetupError,
)
from .models import EzloConfigEntry, EzloRuntimeData

_LOGGER = logging.getLogger(__name__)


def get_frp_config_dir(hass: HomeAssistant) -> Path:
    """Return the directory the FRPC config and binary live in.

    Uses ``hass.config.path`` so the data survives HACS upgrades and so the
    integration package directory stays read-only.
    """
    return Path(hass.config.path(".storage", "ezlocloudharc"))


def get_frp_config_path(hass: HomeAssistant) -> Path:
    """Return the frpc client config path."""
    return get_frp_config_dir(hass) / "frpc.toml"


def get_frp_binary_path(hass: HomeAssistant) -> Path:
    """Return the frpc client binary path."""
    return get_frp_config_dir(hass) / "bin" / "frpc"


async def fetch_and_update_frp_config(
    hass: HomeAssistant,
    uuid: str,
    token: str,
    *,
    api_uri: str = DEFAULT_API_URI,
) -> dict[str, str]:
    """Fetch the server-config from the backend and write the FRPC TOML.

    Returns a dict with `server_name` and `subdomain` for the first proxy.

    Raises:
        EzloSubscriptionExpiredError: backend returned 402.
        EzloApiUnreachableError: network failure.
        EzloApiUnexpectedResponseError: malformed payload.
    """
    config_path = get_frp_config_path(hass)
    session = async_get_clientsession(hass)

    try:
        async with session.get(
            f"{api_uri}/api/user/{uuid}/server-config",
            timeout=ClientTimeout(total=10),
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if response.status == 402:
                raise EzloSubscriptionExpiredError(
                    "Your subscription has expired. Please subscribe to continue."
                )
            # An expired/revoked token must trigger reauth, not an endless
            # ConfigEntryNotReady retry — surface it as an auth failure so
            # async_setup_entry raises ConfigEntryAuthFailed.
            if response.status in (401, 403):
                raise EzloAuthError(
                    f"server-config rejected the token (HTTP {response.status})"
                )
            response.raise_for_status()
            api_config = await response.json()
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            raise EzloAuthError(
                f"server-config rejected the token (HTTP {err.status})"
            ) from err
        raise EzloApiUnreachableError(f"server-config HTTP {err.status}") from err
    except aiohttp.ClientError as err:
        raise EzloApiUnreachableError(str(err)) from err
    except TimeoutError as err:
        raise EzloApiUnreachableError("server-config timed out") from err

    try:
        server_config = api_config["serverConfig"]
        proxies_in = server_config["proxies"]
        server_addr = server_config["serverAddr"]
        server_port = server_config["serverPort"]
    except (KeyError, TypeError) as err:
        raise EzloApiUnexpectedResponseError(f"server-config missing key: {err}") from err

    tunnel_token = server_config.get("auth", {}).get("token", "")
    if not tunnel_token:
        _LOGGER.warning("No tunnel token in server-config response")

    def _create_toml() -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        doc = document()
        doc.add("serverAddr", server_addr)
        doc.add("serverPort", server_port)

        proxies_table = aot()
        for proxy in proxies_in:
            proxy_table = table()
            proxy_table.add("name", proxy["name"])
            proxy_table.add("type", proxy["type"])
            proxy_table.add("localPort", proxy["localPort"])
            subdomain_raw = proxy["subdomain"]
            subdomain = (
                subdomain_raw.split(":")[0]
                if ":" in subdomain_raw
                else subdomain_raw
            )
            proxy_table.add("subdomain", subdomain)
            proxies_table.append(proxy_table)
        doc.add("proxies", proxies_table)

        # tomlkit quotes dotted keys ("metadatas.token"), but frp requires
        # the bare form. Inject the token line after serverPort.
        toml_text = dumps(doc)
        lines = toml_text.split("\n")
        if tunnel_token:
            meta_line = f'metadatas.token = "{tunnel_token}"'
            insert_idx = next(
                (
                    i + 1
                    for i, line in enumerate(lines)
                    if line.startswith("serverPort")
                ),
                2,
            )
            lines.insert(insert_idx, meta_line)
        # Restrict permissions because the file contains a bearer token.
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    await hass.async_add_executor_job(_create_toml)

    first_proxy = proxies_in[0] if proxies_in else {}
    subdomain_raw = first_proxy.get("subdomain", "")
    subdomain = subdomain_raw.split(":")[0] if ":" in subdomain_raw else subdomain_raw
    return {
        "server_name": server_config.get("serverName", ""),
        "subdomain": subdomain,
    }


# ── Process lifecycle ───────────────────────────────────────────────


async def start_frpc(hass: HomeAssistant, config_entry: EzloConfigEntry) -> None:
    """Start the FRPC client subprocess and a watchdog to monitor it.

    On success, ``entry.runtime_data.process`` and ``.is_connected`` are
    populated. On failure the process is None and is_connected is False.
    """
    binary_path = get_frp_binary_path(hass)
    config_path = get_frp_config_path(hass)

    _LOGGER.info("Starting FRPC: config=%s binary=%s", config_path, binary_path)

    runtime = config_entry.runtime_data
    try:
        process = await asyncio.create_subprocess_exec(
            str(binary_path),
            "-c",
            str(config_path),
        )
    except (OSError, ValueError) as err:
        _LOGGER.error("Failed to start FRPC: %s", err)
        runtime.process = None
        runtime.is_connected = False
        raise FrpcSetupError(str(err)) from err

    runtime.process = process
    runtime.config_path = config_path
    runtime.binary_path = binary_path
    runtime.is_connected = True
    runtime.last_unavailable_logged = False

    async def _watchdog() -> None:
        try:
            rc = await process.wait()
        except asyncio.CancelledError:
            return
        runtime.is_connected = False
        if not runtime.last_unavailable_logged:
            _LOGGER.warning("FRPC exited with code %s", rc)
            runtime.last_unavailable_logged = True

    runtime.watchdog_task = hass.loop.create_task(_watchdog())


async def stop_frpc(hass: HomeAssistant, config_entry: EzloConfigEntry) -> None:
    """Stop the FRPC client subprocess if running."""
    runtime: EzloRuntimeData | None = getattr(config_entry, "runtime_data", None)
    if runtime is None or runtime.process is None:
        _LOGGER.debug("FRPC not running for entry %s", config_entry.entry_id)
        return

    process = runtime.process
    _LOGGER.info("Stopping FRPC client (PID: %s)", process.pid)

    if runtime.watchdog_task is not None:
        runtime.watchdog_task.cancel()
        runtime.watchdog_task = None

    if process.returncode is None:
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                _LOGGER.warning("FRPC client forced shutdown")
                process.kill()
                await process.wait()
        except (ProcessLookupError, OSError) as err:
            _LOGGER.error("Error stopping FRPC: %s", err)

    runtime.process = None
    runtime.is_connected = False
    _LOGGER.debug("Cleaned up FRPC resources for entry %s", config_entry.entry_id)
