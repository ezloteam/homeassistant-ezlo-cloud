"""Tests for top-level setup/unload in __init__.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezlocloudharc import (
    async_setup_entry,
    async_unload_entry,
    get_system_architecture,
)
from custom_components.ezlocloudharc.const import DOMAIN, SubscriptionStatus
from custom_components.ezlocloudharc.exceptions import (
    EzloSubscriptionExpiredError,
    FrpcInstallError,
    FrpcUnsupportedArchitectureError,
)
from custom_components.ezlocloudharc.models import EzloRuntimeData

USER_UUID = "f960d12e-4ccb-4f0a-b37f-0abee2cd9717"


def _entry(**data: object) -> MockConfigEntry:
    """Build a MockConfigEntry with the given data merged onto a sane baseline."""
    base = {
        "auth_token": "jwt",
        "user": {"uuid": USER_UUID},
    }
    base.update(data)
    return MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=base)


# ── get_system_architecture ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "amd64"),
        ("aarch64", "arm64"),
        ("armv7l", "arm_hf"),
        ("armv6l", "arm"),
        ("X86_64", "amd64"),  # case-insensitive
    ],
)
async def test_get_system_architecture_known(machine: str, expected: str) -> None:
    """Known architectures resolve to the expected mapping."""
    with patch(
        "custom_components.ezlocloudharc.platform.machine",
        return_value=machine,
    ):
        assert await get_system_architecture() == expected


async def test_get_system_architecture_unsupported() -> None:
    """Unsupported architecture raises FrpcUnsupportedArchitectureError."""
    with (
        patch(
            "custom_components.ezlocloudharc.platform.machine",
            return_value="riscv64",
        ),
        pytest.raises(FrpcUnsupportedArchitectureError),
    ):
        await get_system_architecture()


# ── async_setup_entry: early-exit paths ──────────────────────────────


async def test_setup_entry_missing_token_loads_idle(
    hass: HomeAssistant,
) -> None:
    """A logged-out entry (no auth_token) loads idle so the flows can prompt login.

    It must NOT raise (no forced reauth) and must NOT start frpc.
    """
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezlocloudharc.start_frpc", AsyncMock()
    ) as start_frpc:
        result = await async_setup_entry(hass, entry)

    assert result is True
    start_frpc.assert_not_awaited()


async def test_setup_entry_payment_required_loads_idle_no_tunnel(
    hass: HomeAssistant,
) -> None:
    """A payment_required entry loads successfully and idles (no reauth, no tunnel).

    Authentication succeeded; the user just isn't subscribed. Setup must return
    True (credentials stay saved, no reauth prompt) and must NOT start frpc.
    """
    entry = _entry(payment_required=True)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezlocloudharc.start_frpc", AsyncMock()
    ) as start_frpc:
        result = await async_setup_entry(hass, entry)

    assert result is True
    start_frpc.assert_not_awaited()


async def test_setup_entry_missing_uuid_loads_idle(
    hass: HomeAssistant,
) -> None:
    """A token without a user uuid can't tunnel — loads idle (no raise, no frpc)."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={"auth_token": "jwt", "user": {}}
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezlocloudharc.start_frpc", AsyncMock()
    ) as start_frpc:
        result = await async_setup_entry(hass, entry)

    assert result is True
    start_frpc.assert_not_awaited()


# ── async_setup_entry: install / fetch failures ──────────────────────


async def test_setup_entry_unsupported_arch_raises_not_ready(
    hass: HomeAssistant,
) -> None:
    """FrpcUnsupportedArchitectureError becomes ConfigEntryNotReady."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ezlocloudharc.get_system_architecture",
            AsyncMock(side_effect=FrpcUnsupportedArchitectureError("riscv", ["amd64"])),
        ),
        patch(
            "custom_components.ezlocloudharc.is_trusted_proxy_configured",
            return_value=True,
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)


async def test_setup_entry_install_error_raises_not_ready(
    hass: HomeAssistant,
) -> None:
    """A failed FRPC install surfaces as ConfigEntryNotReady."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ezlocloudharc.get_system_architecture",
            AsyncMock(return_value="amd64"),
        ),
        patch(
            "custom_components.ezlocloudharc.install_frpc",
            AsyncMock(side_effect=FrpcInstallError("checksum fail")),
        ),
        patch(
            "custom_components.ezlocloudharc.is_trusted_proxy_configured",
            return_value=True,
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)


async def test_setup_entry_subscription_expired_writes_canceled_and_idles(
    hass: HomeAssistant,
) -> None:
    """A runtime 402 marks the entry canceled + idles (no tunnel, no reauth)."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ezlocloudharc.get_system_architecture",
            AsyncMock(return_value="amd64"),
        ),
        patch(
            "custom_components.ezlocloudharc.install_frpc",
            AsyncMock(return_value="/fake/bin/frpc"),
        ),
        patch(
            "custom_components.ezlocloudharc.fetch_and_update_frp_config",
            AsyncMock(side_effect=EzloSubscriptionExpiredError("expired")),
        ),
        patch(
            "custom_components.ezlocloudharc.is_trusted_proxy_configured",
            return_value=True,
        ),
        patch(
            "custom_components.ezlocloudharc.start_frpc", AsyncMock()
        ) as start_frpc,
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    start_frpc.assert_not_awaited()
    assert entry.data["subscription_status"] == SubscriptionStatus.CANCELED.value
    assert entry.data["payment_required"] is True


# ── async_setup_entry: happy path ────────────────────────────────────


async def test_setup_entry_success(hass: HomeAssistant, tmp_path: Path) -> None:
    """A complete happy path attaches runtime_data and starts frpc."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ezlocloudharc.get_system_architecture",
            AsyncMock(return_value="amd64"),
        ),
        patch(
            "custom_components.ezlocloudharc.install_frpc",
            AsyncMock(return_value=str(tmp_path / "frpc")),
        ),
        patch(
            "custom_components.ezlocloudharc.fetch_and_update_frp_config",
            AsyncMock(
                return_value={
                    "server_name": "connect.harc.cloud",
                    "subdomain": "abc",
                }
            ),
        ),
        patch(
            "custom_components.ezlocloudharc.start_frpc", AsyncMock()
        ) as start,
        patch(
            "custom_components.ezlocloudharc.is_trusted_proxy_configured",
            return_value=True,
        ),
    ):
        ok = await async_setup_entry(hass, entry)

    assert ok is True
    assert isinstance(entry.runtime_data, EzloRuntimeData)
    assert entry.runtime_data.binary_path == tmp_path / "frpc"
    start.assert_awaited_once()
    # FRP info persisted to entry data
    assert entry.data["server_name"] == "connect.harc.cloud"
    assert entry.data["subdomain"] == "abc"


# ── async_unload_entry ───────────────────────────────────────────────


async def test_unload_entry_cancels_pending_poll_and_stops_frpc(
    hass: HomeAssistant,
) -> None:
    """async_unload_entry cancels the polling task and stops frpc."""
    entry = _entry()
    entry.add_to_hass(hass)

    runtime = EzloRuntimeData()
    poll_task = MagicMock()  # the unload reads `.cancel()` on it
    runtime.payment_poll_task = poll_task
    entry.runtime_data = runtime

    with patch(
        "custom_components.ezlocloudharc.stop_frpc", AsyncMock()
    ) as stop:
        assert await async_unload_entry(hass, entry) is True

    poll_task.cancel.assert_called_once()
    assert runtime.payment_poll_task is None
    stop.assert_awaited_once()
