"""Ezlo HA Cloud integration for Home Assistant."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.httpx_client import get_async_client

from .const import (
    CONF_API_URI,
    DEFAULT_API_URI,
    DOMAIN,
    FRPC_SHA256,
    FRPC_VERSION,
    ISSUE_TRUSTED_PROXIES_RESTART,
    SubscriptionStatus,
)
from .exceptions import (
    EzloAuthError,
    EzloError,
    EzloSubscriptionExpiredError,
    FrpcChecksumError,
    FrpcInstallError,
    FrpcUnsupportedArchitectureError,
)
from .frp_helpers import (
    fetch_and_update_frp_config,
    get_frp_binary_path,
    start_frpc,
    stop_frpc,
)
from .models import EzloConfigEntry, EzloRuntimeData
from .utils import is_trusted_proxy_configured

_LOGGER = logging.getLogger(__name__)

# Mapping from `platform.machine()` to the architecture string used in
# upstream frpc release filenames (frp_<ver>_linux_<arch>.tar.gz).
# Keep aligned with FRPC_SHA256 — entries here without a matching checksum
# will fail the install with FrpcChecksumError.
ARCH_MAP: dict[str, str] = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "x86_64": "amd64",
    "amd64": "amd64",
    "armv7l": "arm_hf",
    "armv6l": "arm",
    "armhf": "arm_hf",
    "arm": "arm",
}


async def async_setup_entry(hass: HomeAssistant, entry: EzloConfigEntry) -> bool:
    """Set up the FRPC client from a config entry."""
    runtime = EzloRuntimeData()
    entry.runtime_data = runtime

    config = entry.data
    token = config.get("auth_token")
    if not token:
        # Without credentials we can still register the entry so the options
        # flow can prompt the user for login. Surface a reauth flow.
        raise ConfigEntryAuthFailed("Ezlo Cloud HARC credentials missing")

    if config.get("payment_required"):
        raise ConfigEntryAuthFailed("Ezlo Cloud HARC subscription requires payment")

    user = config.get("user") or {}
    uuid = user.get("uuid")
    if not uuid:
        raise ConfigEntryAuthFailed("Ezlo Cloud HARC token is missing a user identifier")

    # Surface the trusted_proxies requirement as a repair issue rather than
    # mutating the user's configuration.yaml.
    if not await hass.async_add_executor_job(is_trusted_proxy_configured, hass):
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_TRUSTED_PROXIES_RESTART,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TRUSTED_PROXIES_RESTART,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_TRUSTED_PROXIES_RESTART)

    try:
        machine = await get_system_architecture()
        binary_path = await install_frpc(hass, FRPC_VERSION, machine)
    except FrpcUnsupportedArchitectureError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except FrpcInstallError as err:
        raise ConfigEntryNotReady(f"Failed to install FRPC: {err}") from err

    runtime.binary_path = Path(binary_path)

    api_uri = entry.data.get(CONF_API_URI) or DEFAULT_API_URI
    try:
        frp_info = await fetch_and_update_frp_config(
            hass=hass, uuid=uuid, token=token, api_uri=api_uri
        )
    except EzloAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except EzloSubscriptionExpiredError as err:
        new_data = dict(entry.data)
        new_data["subscription_status"] = SubscriptionStatus.CANCELED.value
        new_data["payment_required"] = True
        hass.config_entries.async_update_entry(entry, data=new_data)
        raise ConfigEntryAuthFailed(str(err)) from err
    except EzloError as err:
        raise ConfigEntryNotReady(str(err)) from err

    new_data = dict(entry.data)
    if frp_info.get("server_name"):
        new_data["server_name"] = frp_info["server_name"]
    if frp_info.get("subdomain"):
        new_data["subdomain"] = frp_info["subdomain"]
    hass.config_entries.async_update_entry(entry, data=new_data)

    try:
        await start_frpc(hass=hass, config_entry=entry)
    except EzloError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.async_on_unload(_make_stop_listener(hass, entry))
    return True


def _make_stop_listener(
    hass: HomeAssistant, entry: EzloConfigEntry
) -> CALLBACK_TYPE:
    """Register a homeassistant_stop listener that tears down FRPC."""

    async def _on_stop(_event: object) -> None:
        await stop_frpc(hass, entry)

    return hass.bus.async_listen_once("homeassistant_stop", _on_stop)


async def install_frpc(hass: HomeAssistant, version: str, machine: str) -> str:
    """Install the FRPC binary for the given version and architecture."""
    bin_dir = get_frp_binary_path(hass).parent
    binary_path = bin_dir / "frpc"

    await hass.async_add_executor_job(_ensure_bin_dir, bin_dir)

    if await check_binary_current(binary_path, version):
        _LOGGER.debug("Using existing FRPC binary v%s", version)
        return str(binary_path)

    expected_sha256 = FRPC_SHA256.get(machine)
    if expected_sha256 is None:
        raise FrpcChecksumError(
            f"No SHA-256 hash pinned for architecture {machine!r}"
        )

    _LOGGER.info("Installing FRPC v%s for %s architecture", version, machine)

    url = (
        "https://github.com/fatedier/frp/releases/download/"
        f"v{version}/frp_{version}_linux_{machine}.tar.gz"
    )
    client = get_async_client(hass)

    with tempfile.TemporaryDirectory() as temp_dir:
        tar_path = Path(temp_dir) / "frpc.tar.gz"
        hasher = hashlib.sha256()
        try:
            async with client.stream(
                "GET", url, timeout=60, follow_redirects=True
            ) as response:
                response.raise_for_status()

                def _write_chunk(chunk: bytes) -> None:
                    with open(tar_path, "ab") as fh:
                        fh.write(chunk)

                async for chunk in response.aiter_bytes(chunk_size=8192):
                    hasher.update(chunk)
                    await hass.async_add_executor_job(_write_chunk, chunk)
        except httpx.HTTPError as err:
            raise FrpcInstallError(f"Download failed: {err}") from err

        actual_sha256 = hasher.hexdigest()
        if actual_sha256 != expected_sha256:
            raise FrpcChecksumError(
                f"FRPC tarball SHA-256 mismatch: expected {expected_sha256}, "
                f"got {actual_sha256}"
            )

        await hass.async_add_executor_job(
            _extract_frpc_binary, tar_path, binary_path, temp_dir
        )

    _LOGGER.info("Successfully installed FRPC to %s", binary_path)
    return str(binary_path)


def _ensure_bin_dir(bin_dir: Path) -> None:
    """Create ``bin_dir`` (and parents) idempotently."""
    bin_dir.mkdir(parents=True, exist_ok=True)


def _extract_frpc_binary(tar_path: Path, binary_path: Path, temp_dir: str) -> None:
    """Extract the frpc binary from the downloaded tarball."""
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


async def check_binary_current(binary_path: Path, version: str) -> bool:
    """Check whether the installed binary matches the required version."""
    if not binary_path.is_file():
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary_path),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as err:
        _LOGGER.debug("Version check spawn failed: %s", err)
        return False

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return False

    return version in stdout.decode(errors="replace").strip()


async def get_system_architecture() -> str:
    """Return the mapped FRPC architecture for the current host."""
    arch = platform.machine().lower()
    mapped = ARCH_MAP.get(arch)
    if not mapped:
        raise FrpcUnsupportedArchitectureError(arch, sorted(set(ARCH_MAP.values())))
    return mapped


async def async_unload_entry(hass: HomeAssistant, entry: EzloConfigEntry) -> bool:
    """Unload the FRPC client and cancel pending tasks."""
    runtime = entry.runtime_data
    if runtime.payment_poll_task is not None:
        runtime.payment_poll_task.cancel()
        runtime.payment_poll_task = None
    await stop_frpc(hass, entry)
    return True


__all__ = [
    "ARCH_MAP",
    "async_setup_entry",
    "async_unload_entry",
    "check_binary_current",
    "get_system_architecture",
    "install_frpc",
]
