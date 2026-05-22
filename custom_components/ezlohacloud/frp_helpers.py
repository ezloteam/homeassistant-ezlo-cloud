"""Ezlo HA Cloud integration helpers for Home Assistant."""

import logging
from pathlib import Path
import subprocess

import aiohttp
from aiohttp import ClientTimeout
from tomlkit import aot, document, dumps, table

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import SubscriptionExpiredError
from .const import DOMAIN, EZLO_API_URI

_LOGGER = logging.getLogger(__name__)


def get_frp_config_path() -> Path:
    """Return the frp client config path."""
    return Path(__file__).parent / "config" / "frpc.toml"


def get_frp_binary_path() -> Path:
    """Return the frp client binary path."""
    return Path(__file__).parent / "bin" / "frpc"


async def fetch_and_update_frp_config(
    hass: HomeAssistant, uuid: str, token: str
) -> dict:
    """Fetch and update the frp client config.

    Returns a dict with server_addr and subdomain for the first proxy.
    """

    config_path = get_frp_config_path()

    try:
        # Fetch configuration from API
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{EZLO_API_URI}/api/user/{uuid}/server-config",
                timeout=ClientTimeout(total=10),
                headers={"Authorization": f"Bearer {token}"},
            ) as response,
        ):
            response.raise_for_status()
            api_config = await response.json()

        # Extract server configuration from nested structure
        server_config = api_config["serverConfig"]

        # Get tunnel token from server-config response (preferred)
        tunnel_token = server_config.get("auth", {}).get("token", "")
        _LOGGER.debug("Server-config auth section: %s", server_config.get("auth"))
        if not tunnel_token:
            _LOGGER.warning("No tunnel token in server-config response")

        # Create TOML document with tomlkit
        def _create_toml():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            doc = document()

            # Add server configuration
            doc.add("serverAddr", server_config["serverAddr"])
            doc.add("serverPort", server_config["serverPort"])

            # Create array-of-tables for proxies
            proxies = aot()
            for proxy in server_config["proxies"]:
                proxy_table = table()
                proxy_table.add("name", proxy["name"])
                proxy_table.add("type", proxy["type"])
                proxy_table.add("localPort", proxy["localPort"])
                # API returns "hash:server.domain" — only the hash is needed
                subdomain_raw = proxy["subdomain"]
                subdomain = (
                    subdomain_raw.split(":")[0]
                    if ":" in subdomain_raw
                    else subdomain_raw
                )
                proxy_table.add("subdomain", subdomain)
                proxies.append(proxy_table)

            doc.add("proxies", proxies)

            # Write to file
            # tomlkit quotes dotted keys ("metadatas.token"), but frp
            # requires the bare form: metadatas.token = "...".
            # So we inject it as a raw line after the server fields.
            toml_text = dumps(doc)
            lines = toml_text.split("\n")
            # Insert metadatas.token (tunnel token) after serverPort line
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
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

        await hass.async_add_executor_job(_create_toml)

    except KeyError as err:
        _LOGGER.error("Missing expected key in API response: %s", err)
        raise
    except aiohttp.ClientResponseError as err:
        if err.status == 402:
            _LOGGER.warning("Subscription expired — cannot fetch FRP config")
            raise SubscriptionExpiredError(
                "Your subscription has expired. Please subscribe to continue."
            ) from err
        _LOGGER.error("API request failed (HTTP %s): %s", err.status, err.message)
        raise
    except aiohttp.ClientError as err:
        _LOGGER.error("API request failed: %s", err)
        raise
    except Exception as err:
        _LOGGER.error("Configuration generation failed: %s", err)
        raise

    # Return connection details for the config entry
    first_proxy = server_config["proxies"][0] if server_config["proxies"] else {}
    subdomain_raw = first_proxy.get("subdomain", "")
    subdomain = subdomain_raw.split(":")[0] if ":" in subdomain_raw else subdomain_raw
    return {
        "server_name": server_config.get("serverName", ""),
        "subdomain": subdomain,
    }


async def start_frpc(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Start FRPC client service."""
    binary_path = get_frp_binary_path()
    config_path = get_frp_config_path()

    _LOGGER.info("Config: %s and binary path: %s", config_path, binary_path)

    try:
        process = await hass.async_add_executor_job(
            subprocess.Popen, [str(binary_path), "-c", str(config_path)]
        )
    except (OSError, subprocess.SubprocessError) as err:
        _LOGGER.error("Failed to start FRPC: %s", err)
        return

    # Store process reference using entry.entry_id
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[config_entry.entry_id] = {
        "process": process,
        "config_path": config_path,
        "binary_path": binary_path,
    }

    # Register shutdown listener only once per entry
    listener_key = f"{config_entry.entry_id}_shutdown_unsub"
    if listener_key not in domain_data:

        async def async_shutdown(_event):
            await async_unload_entry(hass, config_entry)

        domain_data[listener_key] = hass.bus.async_listen_once(
            "homeassistant_stop", async_shutdown
        )


async def stop_frpc(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Stop FRPC client process."""

    def _sync_stop(process: subprocess.Popen) -> None:
        """Terminate the process synchronously."""
        try:
            if process.poll() is None:  # Process is still running
                process.terminate()
                try:
                    process.wait(5)  # Wait 5 seconds for clean exit
                except subprocess.TimeoutExpired:
                    _LOGGER.warning("FRPC client forced shutdown")
                    process.kill()
                    process.wait()  # Cleanup zombies
        except (OSError, subprocess.SubprocessError) as err:
            _LOGGER.error("Error stopping FRPC: %s", err)

    # Get stored process reference
    if config_entry.entry_id not in hass.data.get(DOMAIN, {}):
        _LOGGER.warning("FRPC process not found for entry %s", config_entry.entry_id)
        return

    data = hass.data[DOMAIN].get(config_entry.entry_id)
    if not data or "process" not in data:
        return

    process = data["process"]
    _LOGGER.info("Stopping FRPC client (PID: %s)", process.pid)

    try:
        await hass.async_add_executor_job(_sync_stop, process)
    except (OSError, subprocess.SubprocessError) as err:
        _LOGGER.error("Failed to stop FRPC: %s", err)
    finally:
        # Cleanup data entry
        hass.data[DOMAIN].pop(config_entry.entry_id, None)
        _LOGGER.debug("Cleaned up FRPC resources for entry %s", config_entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload frpc client."""
    if entry.entry_id not in hass.data.get(DOMAIN, {}):
        return True

    data = hass.data[DOMAIN].pop(entry.entry_id)
    process = data["process"]

    try:
        process.terminate()
        await hass.async_add_executor_job(process.wait, 5)
    except subprocess.TimeoutExpired:
        _LOGGER.warning("FRPC client did not terminate gracefully, forcing exit")
        process.kill()
    except OSError as err:
        _LOGGER.error("Error stopping FRPC client: %s", err)

    return True
