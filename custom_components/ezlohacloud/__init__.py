"""Ezlo HA Cloud integration for Home Assistant."""

import asyncio
import logging
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile

import httpx

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import SubscriptionExpiredError
from .const import DOMAIN, SUBSCRIPTION_CANCELED
from .frp_helpers import fetch_and_update_frp_config, start_frpc, stop_frpc
from .utils import ensure_trusted_proxy_config

_LOGGER = logging.getLogger(__name__)

# Architecture mapping
ARCH_MAP = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "x86_64": "amd64",
    "amd64": "amd64",
    "armv7l": "arm",
    "armv6l": "arm",
    "armhf": "arm",
    "arm": "arm",
    "i386": "386",
    "i686": "386",
}


class FrpcInstallError(Exception):
    """Raised when FRPC installation fails."""


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up frpc client from a config entry."""
    config = entry.data
    entry.runtime_data = {}

    # Skip setup if auth_token is not yet available
    token = config.get("auth_token")
    if not token:
        _LOGGER.warning("Auth_token missing; skipping FRPC setup until login")
        return True  # Still allow integration to register and show up

    try:
        # Install/update frpc binary
        version = "0.61.0"  # config["frp_version"]
        machine = await get_system_architecture(hass)
        binary_path = await install_frpc(hass, version, machine)

        # Proceed with configuration setup
        return await setup_frpc_configuration(hass, entry, binary_path)

    except Exception as err:
        raise ConfigEntryNotReady(f"Failed to setup FRPC: {err}") from err


async def install_frpc(hass: HomeAssistant, version: str, machine: str) -> str:
    """Install FRPC binary for specific version and architecture."""
    integration_dir = Path(__file__).parent
    bin_dir = integration_dir / "bin"
    binary_path = bin_dir / "frpc"

    await hass.async_add_executor_job(
        lambda: bin_dir.mkdir(parents=True, exist_ok=True)
    )

    # Check if we need to update the binary
    if await check_binary_current(binary_path, version):
        _LOGGER.debug("Using existing FRPC binary v%s", version)
        return str(binary_path)

    _LOGGER.info("Installing FRPC v%s for %s architecture", version, machine)
    return await hass.async_add_executor_job(
        _sync_install_frpc, version, machine, binary_path
    )


def _sync_install_frpc(version: str, machine: str, binary_path: Path) -> str:
    """Executor for FRPC installation."""
    url = f"https://github.com/fatedier/frp/releases/download/v{version}/frp_{version}_linux_{machine}.tar.gz"

    with tempfile.TemporaryDirectory() as temp_dir:
        tar_path = Path(temp_dir) / "frpc.tar.gz"

        # Download frp release
        try:
            with httpx.stream(
                "GET", url, timeout=30, follow_redirects=True
            ) as response:
                response.raise_for_status()
                with open(tar_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
        except httpx.HTTPError as err:
            raise FrpcInstallError(f"Download failed: {err}") from err

        # Extract and install binary
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                members = [m for m in tar.getmembers() if m.name.endswith("/frpc")]
                if not members:
                    raise FrpcInstallError("No frpc binary found in release package")
                tar.extract(members[0], path=temp_dir, filter="data")

            extracted_bin = Path(temp_dir) / members[0].name
            shutil.copy(extracted_bin, binary_path)
            binary_path.chmod(0o755)
        except tarfile.TarError as err:
            raise FrpcInstallError(f"Extraction failed: {err}") from err

    _LOGGER.info("Successfully installed FRPC to %s", binary_path)
    return str(binary_path)


async def check_binary_current(binary_path: Path, version: str) -> bool:
    """Check if existing binary matches required version."""
    if not binary_path.is_file():
        return False

    try:
        # Create async subprocess
        proc = await asyncio.create_subprocess_exec(
            str(binary_path),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wait for process to complete with timeout
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return False

    except (OSError, subprocess.SubprocessError) as err:
        _LOGGER.debug("Version check error: %s", err)
        return False
    else:
        output = stdout.decode().strip()
        return version in output


async def get_system_architecture(hass: HomeAssistant) -> str:
    """Determine system architecture with Home Assistant compatibility."""
    arch = platform.machine().lower()
    mapped = ARCH_MAP.get(arch)
    if not mapped:
        raise FrpcInstallError(
            f"Unsupported architecture: {arch}. "
            f"Supported: {', '.join(sorted(ARCH_MAP))}"
        )
    return mapped


async def setup_frpc_configuration(
    hass: HomeAssistant,
    entry: ConfigEntry,
    binary_path: str,
) -> bool:
    """Configure and start FRPC client."""

    auth_data = get_config_data(hass)
    token = auth_data.get("auth_token")
    user = auth_data.get("user", {})
    uuid = user.get("uuid")
    is_logged_in = auth_data.get("is_logged_in", False)

    if not token or not uuid:
        if is_logged_in:
            _LOGGER.warning("Logged in but missing token or uuid; cannot start FRPC")
        return False

    try:
        # Ensure HA is configured to trust frpc's reverse proxy from localhost
        config_changed = await hass.async_add_executor_job(
            ensure_trusted_proxy_config, hass
        )
        if config_changed:
            _LOGGER.warning(
                "Updated configuration.yaml with trusted proxy settings. "
                "Please restart Home Assistant for remote access to work"
            )

        await fetch_and_update_frp_config(hass=hass, uuid=uuid, token=token)
        await start_frpc(hass=hass, config_entry=entry)
    except SubscriptionExpiredError:
        _LOGGER.warning(
            "Subscription expired — skipping FRPC setup. "
            "Please subscribe via the integration options"
        )
        new_data = entry.data.copy()
        new_data["subscription_status"] = SUBSCRIPTION_CANCELED
        new_data["payment_required"] = True
        hass.config_entries.async_update_entry(entry, data=new_data)
        return True  # Don't fail the entry, let user fix via options
    except (OSError, ValueError, RuntimeError) as err:
        _LOGGER.error("Configuration failed: %s", err)
        return False

    return True


def get_config_entry(hass: HomeAssistant) -> ConfigEntry:
    """Get the first config entry for this integration."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ValueError("No config entry found")
    return entries[0]


def get_config_data(hass: HomeAssistant) -> dict:
    """Get the configuration data dictionary."""
    entry = get_config_entry(hass)
    return dict(entry.data)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload frpc client."""
    await stop_frpc(hass, entry)
    return True
