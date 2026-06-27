"""Tests for the diagnostics dump."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezlohacloud.const import DOMAIN, FRPC_VERSION
from custom_components.ezlohacloud.diagnostics import (
    _read_binary_version,
    async_get_config_entry_diagnostics,
)
from custom_components.ezlohacloud.models import EzloRuntimeData


async def test_diagnostics_redacts_secrets_and_reports_runtime_state(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Diagnostics returns runtime state plus a redacted copy of entry data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            "auth_token": "super-secret-jwt",
            "tunnel_token": "tt",
            "user": {
                "uuid": "secret-uuid",
                "username": "alice",
                "email": "alice@example.com",
                "ezlo_id": 99,
            },
            "subscription_status": "active",
        },
    )
    entry.add_to_hass(hass)

    runtime = EzloRuntimeData()
    process = MagicMock()
    process.returncode = None
    process.pid = 4242
    runtime.process = process
    runtime.is_connected = True
    entry.runtime_data = runtime

    fake_binary = tmp_path / "frpc"
    fake_config = tmp_path / "frpc.toml"
    fake_binary.write_bytes(b"")
    fake_config.write_text("dummy")

    with (
        patch(
            "custom_components.ezlohacloud.diagnostics.get_frp_binary_path",
            return_value=fake_binary,
        ),
        patch(
            "custom_components.ezlohacloud.diagnostics.get_frp_config_path",
            return_value=fake_config,
        ),
        patch(
            "custom_components.ezlohacloud.diagnostics._read_binary_version",
            AsyncMock(return_value=f"frpc v{FRPC_VERSION}"),
        ),
        patch(
            "custom_components.ezlohacloud.diagnostics.is_trusted_proxy_configured",
            return_value=True,
        ),
    ):
        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["expected_frpc_version"] == FRPC_VERSION
    assert diag["installed_frpc_version"] == f"frpc v{FRPC_VERSION}"
    assert diag["binary_exists"] is True
    assert diag["config_exists"] is True
    assert diag["process_running"] is True
    assert diag["process_pid"] == 4242
    assert diag["is_connected"] is True
    assert diag["trusted_proxy_configured"] is True

    # All secret-bearing fields are redacted
    entry_data = diag["entry"]
    assert entry_data["auth_token"] == "**REDACTED**"
    assert entry_data["tunnel_token"] == "**REDACTED**"
    assert entry_data["user"]["uuid"] == "**REDACTED**"
    assert entry_data["user"]["email"] == "**REDACTED**"
    assert entry_data["user"]["username"] == "**REDACTED**"
    # Non-secret fields pass through
    assert entry_data["subscription_status"] == "active"


async def test_read_binary_version_missing_file(tmp_path: Path) -> None:
    """_read_binary_version returns None when the binary doesn't exist."""
    assert await _read_binary_version(tmp_path / "frpc") is None


async def test_read_binary_version_reads_version_string(tmp_path: Path) -> None:
    """Reads --version output when the binary is present."""
    binary = tmp_path / "frpc"
    binary.write_bytes(b"")
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"frpc v0.61.0\n", b""))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await _read_binary_version(binary) == "frpc v0.61.0"


async def test_read_binary_version_oserror_returns_none(tmp_path: Path) -> None:
    """OSError spawning the subprocess returns None."""
    binary = tmp_path / "frpc"
    binary.write_bytes(b"")
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=OSError("Exec format error")),
    ):
        assert await _read_binary_version(binary) is None


async def test_read_binary_version_timeout_returns_none(tmp_path: Path) -> None:
    """A hanging subprocess (wait_for timeout) returns None."""
    import asyncio

    binary = tmp_path / "frpc"
    binary.write_bytes(b"")
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.kill = MagicMock()
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch(
            "custom_components.ezlohacloud.diagnostics.asyncio.wait_for",
            AsyncMock(side_effect=asyncio.TimeoutError),
        ),
    ):
        assert await _read_binary_version(binary) is None
    proc.kill.assert_called_once()
