"""Tests for the FRP helper functions (frp_helpers.py)."""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from homeassistant.components.ezlohacloud.api import SubscriptionExpiredError
from homeassistant.components.ezlohacloud.const import DOMAIN
from homeassistant.components.ezlohacloud.frp_helpers import (
    async_unload_entry,
    fetch_and_update_frp_config,
    start_frpc,
    stop_frpc,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

USER_UUID = "f960d12e-4ccb-4f0a-b37f-0abee2cd9717"
API_TOKEN = "api-jwt-token"
TUNNEL_TOKEN = "tunnel-token-12345"

SERVER_CONFIG_RESPONSE = {
    "serverConfig": {
        "serverName": "frp-dev.harc.cloud",
        "serverAddr": "152.42.152.93",
        "serverPort": 7000,
        "auth": {"token": TUNNEL_TOKEN},
        "proxies": [
            {
                "name": "proxy-tcp-tunnel-user1",
                "type": "http",
                "localPort": 8123,
                "subdomain": f"abc123:{TUNNEL_TOKEN}.frp-dev.harc.cloud",
            }
        ],
    }
}


def _make_aiohttp_session_mock(
    *, json_data: dict | None = None, error: Exception | None = None
) -> MagicMock:
    """Build a mock aiohttp.ClientSession that acts as an async context manager.

    Both `ClientSession()` and `session.get(...)` are async context managers.
    Tests use this to replace `aiohttp.ClientSession` end-to-end.
    """
    response = MagicMock()
    response.json = AsyncMock(return_value=json_data or {})
    if error is not None:
        response.raise_for_status = MagicMock(side_effect=error)
    else:
        response.raise_for_status = MagicMock()

    # session.get(...) returns an async context manager yielding the response
    get_ctx = MagicMock()
    get_ctx.__aenter__ = AsyncMock(return_value=response)
    get_ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=get_ctx)

    # ClientSession() returns an async context manager yielding the session
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=session_ctx)


# ── fetch_and_update_frp_config ─────────────────────────────────────


async def test_fetch_and_update_frp_config_writes_toml(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Successful fetch writes a properly-formatted frpc.toml."""
    config_path = tmp_path / "config" / "frpc.toml"

    with (
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.aiohttp.ClientSession",
            _make_aiohttp_session_mock(json_data=SERVER_CONFIG_RESPONSE),
        ),
    ):
        result = await fetch_and_update_frp_config(hass, USER_UUID, API_TOKEN)

    assert config_path.is_file()
    contents = config_path.read_text()
    # Server fields
    assert 'serverAddr = "152.42.152.93"' in contents
    assert "serverPort = 7000" in contents
    # Tunnel token written as bare dotted key
    assert f'metadatas.token = "{TUNNEL_TOKEN}"' in contents
    assert '"metadatas.token"' not in contents  # not quoted
    # Subdomain split at colon — only the hash part
    assert 'subdomain = "abc123"' in contents
    assert f"{TUNNEL_TOKEN}.frp-dev" not in contents

    # Returned info
    assert result["server_name"] == "frp-dev.harc.cloud"
    assert result["subdomain"] == "abc123"


async def test_fetch_and_update_frp_config_subscription_expired(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """HTTP 402 raises SubscriptionExpiredError."""
    config_path = tmp_path / "config" / "frpc.toml"
    error_402 = aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=402,
        message="subscription_required",
    )

    with (
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.aiohttp.ClientSession",
            _make_aiohttp_session_mock(error=error_402),
        ),
        pytest.raises(SubscriptionExpiredError),
    ):
        await fetch_and_update_frp_config(hass, USER_UUID, API_TOKEN)


async def test_fetch_and_update_frp_config_http_error_non_402(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Non-402 HTTP errors re-raise the original ClientResponseError."""
    config_path = tmp_path / "config" / "frpc.toml"
    error_401 = aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=401,
        message="unauthorized",
    )

    with (
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.aiohttp.ClientSession",
            _make_aiohttp_session_mock(error=error_401),
        ),
        pytest.raises(aiohttp.ClientResponseError),
    ):
        await fetch_and_update_frp_config(hass, USER_UUID, API_TOKEN)


async def test_fetch_and_update_frp_config_missing_server_config_key(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Missing serverConfig key raises KeyError."""
    config_path = tmp_path / "config" / "frpc.toml"

    with (
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.aiohttp.ClientSession",
            _make_aiohttp_session_mock(json_data={"unexpected": "shape"}),
        ),
        pytest.raises(KeyError),
    ):
        await fetch_and_update_frp_config(hass, USER_UUID, API_TOKEN)


async def test_fetch_and_update_frp_config_no_tunnel_token(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """When the server response omits auth.token, no metadatas line is written."""
    config_path = tmp_path / "config" / "frpc.toml"
    no_auth_response = {
        "serverConfig": {
            "serverName": "x.example.com",
            "serverAddr": "1.2.3.4",
            "serverPort": 7000,
            "auth": {},
            "proxies": [
                {
                    "name": "p1",
                    "type": "http",
                    "localPort": 8123,
                    "subdomain": "abc",
                }
            ],
        }
    }

    with (
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "homeassistant.components.ezlohacloud.frp_helpers.aiohttp.ClientSession",
            _make_aiohttp_session_mock(json_data=no_auth_response),
        ),
    ):
        await fetch_and_update_frp_config(hass, USER_UUID, API_TOKEN)

    contents = config_path.read_text()
    assert "metadatas.token" not in contents
    assert 'subdomain = "abc"' in contents


# ── start_frpc ──────────────────────────────────────────────────────


async def test_start_frpc_success(hass: HomeAssistant) -> None:
    """start_frpc stores the spawned process under DOMAIN/entry_id."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry-xyz"
    process = MagicMock()
    process.pid = 1234

    with patch(
        "homeassistant.components.ezlohacloud.frp_helpers.subprocess.Popen",
        return_value=process,
    ):
        await start_frpc(hass, entry)

    assert hass.data[DOMAIN][entry.entry_id]["process"] is process
    # A shutdown listener was registered (we don't trigger it; just ensure key exists)
    assert f"{entry.entry_id}_shutdown_unsub" in hass.data[DOMAIN]


async def test_start_frpc_subprocess_failure(hass: HomeAssistant) -> None:
    """Popen failure is swallowed and no process is stored."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry-fail"

    with patch(
        "homeassistant.components.ezlohacloud.frp_helpers.subprocess.Popen",
        side_effect=OSError("binary not found"),
    ):
        await start_frpc(hass, entry)

    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None


# ── stop_frpc ───────────────────────────────────────────────────────


async def test_stop_frpc_terminates_running_process(hass: HomeAssistant) -> None:
    """stop_frpc calls terminate() on a running process and cleans up."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry-running"
    process = MagicMock()
    process.poll.return_value = None  # Still running
    process.pid = 999
    hass.data[DOMAIN] = {entry.entry_id: {"process": process}}

    await stop_frpc(hass, entry)

    process.terminate.assert_called_once()
    process.wait.assert_called()
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_stop_frpc_force_kills_on_timeout(hass: HomeAssistant) -> None:
    """If wait() times out, the process is killed."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry-stuck"
    process = MagicMock()
    process.poll.return_value = None
    process.pid = 999
    # First wait() raises TimeoutExpired, second wait() (cleanup) returns
    process.wait.side_effect = [subprocess.TimeoutExpired("frpc", 5), 0]
    hass.data[DOMAIN] = {entry.entry_id: {"process": process}}

    await stop_frpc(hass, entry)

    process.terminate.assert_called_once()
    process.kill.assert_called_once()


async def test_stop_frpc_no_process(hass: HomeAssistant) -> None:
    """Calling stop_frpc when no process is registered returns cleanly."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry-none"
    hass.data[DOMAIN] = {}  # No process stored

    # Should not raise
    await stop_frpc(hass, entry)


async def test_stop_frpc_data_without_process_key(hass: HomeAssistant) -> None:
    """Entry exists but has no `process` key — stop returns cleanly."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry-broken"
    hass.data[DOMAIN] = {entry.entry_id: {"other": "data"}}

    await stop_frpc(hass, entry)
    # Should not error, data may or may not be cleared
    # (current implementation early-returns without cleanup in this branch)


# ── async_unload_entry ──────────────────────────────────────────────


async def test_async_unload_entry_terminates_process(hass: HomeAssistant) -> None:
    """Unloading the entry terminates the frpc process and clears state."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry-unload"
    process = MagicMock()
    process.pid = 555
    hass.data[DOMAIN] = {entry.entry_id: {"process": process}}

    assert await async_unload_entry(hass, entry) is True

    process.terminate.assert_called_once()
    process.wait.assert_called()
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_async_unload_entry_no_data(hass: HomeAssistant) -> None:
    """Unloading without stored data returns True without error."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry-empty"
    hass.data[DOMAIN] = {}

    assert await async_unload_entry(hass, entry) is True


async def test_async_unload_entry_force_kill_on_timeout(hass: HomeAssistant) -> None:
    """If process doesn't exit on wait, it is force-killed."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry-stuck-unload"
    process = MagicMock()
    process.wait.side_effect = subprocess.TimeoutExpired("frpc", 5)
    hass.data[DOMAIN] = {entry.entry_id: {"process": process}}

    assert await async_unload_entry(hass, entry) is True

    process.terminate.assert_called_once()
    process.kill.assert_called_once()


# ── Regression guard ────────────────────────────────────────────────


def test_no_legacy_frp_endpoints_in_shipped_code() -> None:
    """Shipped code must not hard-code the retired frp-plugin*.ezlo.com hosts.

    All FRP server details (serverAddr, serverPort, serverName, proxy
    subdomains) come from harc-api at runtime via
    fetch_and_update_frp_config. Any hard-coded reference to the retired
    frp-plugin.ezlo.com / frp-plugin-dev.ezlo.com hosts is a bug, since
    harc-api now returns the *.harc.cloud convention.
    """
    pkg_root = Path(__file__).resolve().parent.parent
    forbidden = ("frp-plugin.ezlo.com", "frp-plugin-dev.ezlo.com")
    patterns = ("*.py", "*.json", "*.toml", "*.yaml", "*.yml")
    skip_dirs = {"tests", "config", "bin", "__pycache__"}

    offenders: list[str] = []
    for pattern in patterns:
        for path in pkg_root.rglob(pattern):
            rel = path.relative_to(pkg_root)
            if rel.parts and rel.parts[0] in skip_dirs:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for token in forbidden:
                if token in text:
                    offenders.append(f"{rel}: {token}")

    assert not offenders, (
        f"Legacy FRP hostnames found in shipped code: {offenders}"
    )
