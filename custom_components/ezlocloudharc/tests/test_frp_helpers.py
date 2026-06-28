"""Tests for the FRP helper functions (frp_helpers.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezlocloudharc.const import DOMAIN
from custom_components.ezlocloudharc.exceptions import (
    EzloApiUnreachableError,
    EzloSubscriptionExpiredError,
    FrpcSetupError,
)
from custom_components.ezlocloudharc.frp_helpers import (
    fetch_and_update_frp_config,
    start_frpc,
    stop_frpc,
)
from custom_components.ezlocloudharc.models import EzloRuntimeData

USER_UUID = "f960d12e-4ccb-4f0a-b37f-0abee2cd9717"
API_TOKEN = "api-jwt-token"
TUNNEL_TOKEN = "tunnel-token-12345"

SERVER_CONFIG_RESPONSE: dict[str, Any] = {
    "serverConfig": {
        "serverName": "connect-dev.harc.cloud",
        "serverAddr": "152.42.152.93",
        "serverPort": 7000,
        "auth": {"token": TUNNEL_TOKEN},
        "proxies": [
            {
                "name": "proxy-tcp-tunnel-user1",
                "type": "http",
                "localPort": 8123,
                "subdomain": f"abc123:{TUNNEL_TOKEN}.connect-dev.harc.cloud",
            }
        ],
    }
}


def _mock_aiohttp_session(
    *,
    json_data: dict[str, Any] | None = None,
    status: int = 200,
    raise_for_status_exc: Exception | None = None,
) -> MagicMock:
    """Build a mock aiohttp.ClientSession.get() result usable in async-with."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data or {})
    if raise_for_status_exc is not None:
        response.raise_for_status = MagicMock(side_effect=raise_for_status_exc)
    else:
        response.raise_for_status = MagicMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


# ── fetch_and_update_frp_config ─────────────────────────────────────


async def test_fetch_and_update_frp_config_writes_toml(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Successful fetch writes a properly-formatted frpc.toml."""
    config_path = tmp_path / "config" / "frpc.toml"
    session = _mock_aiohttp_session(json_data=SERVER_CONFIG_RESPONSE)

    with (
        patch(
            "custom_components.ezlocloudharc.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "custom_components.ezlocloudharc.frp_helpers.async_get_clientsession",
            return_value=session,
        ),
    ):
        result = await fetch_and_update_frp_config(hass, USER_UUID, API_TOKEN)

    assert config_path.is_file()
    contents = config_path.read_text()
    assert 'serverAddr = "152.42.152.93"' in contents
    assert "serverPort = 7000" in contents
    # Tunnel token written as a bare dotted key, not a quoted one
    assert f'metadatas.token = "{TUNNEL_TOKEN}"' in contents
    assert '"metadatas.token"' not in contents
    # Subdomain split at colon — only the hash portion is preserved
    assert 'subdomain = "abc123"' in contents
    assert f"{TUNNEL_TOKEN}.connect-dev" not in contents

    assert result["server_name"] == "connect-dev.harc.cloud"
    assert result["subdomain"] == "abc123"
    # 0600 perms because the file carries a bearer token
    assert (config_path.stat().st_mode & 0o777) == 0o600


async def test_fetch_and_update_frp_config_subscription_expired(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """HTTP 402 raises EzloSubscriptionExpiredError."""
    config_path = tmp_path / "config" / "frpc.toml"
    session = _mock_aiohttp_session(status=402)

    with (
        patch(
            "custom_components.ezlocloudharc.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "custom_components.ezlocloudharc.frp_helpers.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(EzloSubscriptionExpiredError),
    ):
        await fetch_and_update_frp_config(hass, USER_UUID, API_TOKEN)


async def test_fetch_and_update_frp_config_other_http_error_unreachable(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Non-402 HTTP errors raise EzloApiUnreachableError."""
    config_path = tmp_path / "config" / "frpc.toml"
    error_401 = aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=401,
        message="unauthorized",
    )
    session = _mock_aiohttp_session(raise_for_status_exc=error_401)

    with (
        patch(
            "custom_components.ezlocloudharc.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "custom_components.ezlocloudharc.frp_helpers.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(EzloApiUnreachableError),
    ):
        await fetch_and_update_frp_config(hass, USER_UUID, API_TOKEN)


async def test_fetch_and_update_frp_config_no_tunnel_token(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """When the server response omits auth.token, no metadatas line is written."""
    config_path = tmp_path / "config" / "frpc.toml"
    no_auth_response: dict[str, Any] = {
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
    session = _mock_aiohttp_session(json_data=no_auth_response)

    with (
        patch(
            "custom_components.ezlocloudharc.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "custom_components.ezlocloudharc.frp_helpers.async_get_clientsession",
            return_value=session,
        ),
    ):
        await fetch_and_update_frp_config(hass, USER_UUID, API_TOKEN)

    contents = config_path.read_text()
    assert "metadatas.token" not in contents
    assert 'subdomain = "abc"' in contents


async def test_fetch_and_update_frp_config_uses_api_uri_override(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """fetch_and_update_frp_config(api_uri=...) hits the override host."""
    config_path = tmp_path / "config" / "frpc.toml"
    dev_api = "https://api-dev.harc.cloud"
    session = _mock_aiohttp_session(json_data=SERVER_CONFIG_RESPONSE)

    with (
        patch(
            "custom_components.ezlocloudharc.frp_helpers.get_frp_config_path",
            return_value=config_path,
        ),
        patch(
            "custom_components.ezlocloudharc.frp_helpers.async_get_clientsession",
            return_value=session,
        ),
    ):
        await fetch_and_update_frp_config(
            hass, USER_UUID, API_TOKEN, api_uri=dev_api
        )

    called_url = session.get.call_args.args[0]
    assert called_url == f"{dev_api}/api/user/{USER_UUID}/server-config"


# ── start_frpc / stop_frpc ──────────────────────────────────────────


async def test_start_frpc_records_process_and_marks_connected(
    hass: HomeAssistant,
) -> None:
    """start_frpc stashes the process on runtime_data and marks connected."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    runtime = EzloRuntimeData()
    entry.runtime_data = runtime

    process = MagicMock()
    process.pid = 1234
    process.wait = AsyncMock(return_value=0)

    with patch(
        "custom_components.ezlocloudharc.frp_helpers.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    ):
        await start_frpc(hass, entry)

    assert runtime.process is process
    assert runtime.is_connected is True
    assert runtime.watchdog_task is not None
    runtime.watchdog_task.cancel()


async def test_start_frpc_spawn_failure_raises_and_resets_runtime(
    hass: HomeAssistant,
) -> None:
    """An OSError from create_subprocess_exec raises FrpcSetupError."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    runtime = EzloRuntimeData()
    runtime.is_connected = True  # ensure we observe it being reset
    entry.runtime_data = runtime

    with (
        patch(
            "custom_components.ezlocloudharc.frp_helpers.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=OSError("binary not found")),
        ),
        pytest.raises(FrpcSetupError),
    ):
        await start_frpc(hass, entry)

    assert runtime.process is None
    assert runtime.is_connected is False


async def test_stop_frpc_terminates_running_process(hass: HomeAssistant) -> None:
    """stop_frpc terminates a running process and clears runtime state."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    runtime = EzloRuntimeData()
    entry.runtime_data = runtime

    process = MagicMock()
    process.pid = 999
    process.returncode = None  # still running
    process.terminate = MagicMock()
    process.wait = AsyncMock(return_value=0)
    runtime.process = process
    runtime.is_connected = True

    await stop_frpc(hass, entry)

    process.terminate.assert_called_once()
    assert runtime.process is None
    assert runtime.is_connected is False


async def test_stop_frpc_force_kills_on_timeout(hass: HomeAssistant) -> None:
    """If terminate hangs past the 5s wait_for, stop_frpc calls kill()."""
    import asyncio

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    runtime = EzloRuntimeData()
    entry.runtime_data = runtime

    process = MagicMock()
    process.pid = 1
    process.returncode = None
    process.terminate = MagicMock()
    process.kill = MagicMock()
    process.wait = AsyncMock(return_value=0)
    runtime.process = process

    with patch(
        "custom_components.ezlocloudharc.frp_helpers.asyncio.wait_for",
        AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        await stop_frpc(hass, entry)

    process.terminate.assert_called_once()
    process.kill.assert_called_once()


async def test_stop_frpc_noop_when_no_process(hass: HomeAssistant) -> None:
    """stop_frpc with no process attached is a no-op (no exception)."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    runtime = EzloRuntimeData()
    entry.runtime_data = runtime

    await stop_frpc(hass, entry)
    assert runtime.process is None


# ── Regression guard: legacy hostnames must not appear in shipped code ──


def test_no_legacy_frp_endpoints_in_shipped_code() -> None:
    """Shipped code must not hard-code any retired FRP wildcard host.

    All FRP server details (serverAddr, serverPort, serverName, proxy
    subdomains) come from harc-api at runtime via
    fetch_and_update_frp_config. Any hard-coded reference to a retired
    host is a bug — harc-api is the single source of truth.

    Currently retired (in chronological order):
    - frp-plugin(-dev).ezlo.com — pre-HARC FRP namespace.
    - frp(-dev).harc.cloud — interim wildcard, replaced by connect(-dev).
    """
    pkg_root = Path(__file__).resolve().parent.parent
    forbidden = (
        "frp-plugin.ezlo.com",
        "frp-plugin-dev.ezlo.com",
        "frp.harc.cloud",
        "frp-dev.harc.cloud",
    )
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
