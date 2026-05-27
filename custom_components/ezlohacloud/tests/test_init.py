"""Tests for the top-level integration setup/unload + helpers in __init__.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.ezlohacloud import (
    async_setup_entry,
    async_unload_entry,
    get_config_data,
    get_config_entry,
    setup_frpc_configuration,
)
from homeassistant.components.ezlohacloud.api import SubscriptionExpiredError
from homeassistant.components.ezlohacloud.const import DOMAIN, SUBSCRIPTION_CANCELED
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from tests.common import MockConfigEntry

# ── get_config_entry / get_config_data ──────────────────────────────


def test_get_config_entry_raises_when_no_entry(hass: HomeAssistant) -> None:
    """Without any config entry, get_config_entry raises ValueError."""
    with pytest.raises(ValueError, match="No config entry found"):
        get_config_entry(hass)


def test_get_config_data_returns_mutable_dict(hass: HomeAssistant) -> None:
    """get_config_data returns a regular dict (not a frozen MappingProxy)."""
    entry = MockConfigEntry(domain=DOMAIN, data={"auth_token": "jwt"}, unique_id=DOMAIN)
    entry.add_to_hass(hass)

    data = get_config_data(hass)
    assert data == {"auth_token": "jwt"}
    # Verify it's mutable (not entry.data's MappingProxyType)
    data["new_key"] = "value"


# ── async_setup_entry ───────────────────────────────────────────────


async def test_async_setup_entry_no_token_returns_true(
    hass: HomeAssistant,
) -> None:
    """Entry without auth_token still registers (so options flow is reachable)."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await async_setup_entry(hass, entry) is True
    # runtime_data was initialized to empty dict
    assert entry.runtime_data == {}


async def test_async_setup_entry_with_token_installs_and_configures(
    hass: HomeAssistant,
) -> None:
    """A logged-in entry installs the binary and runs configuration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_token": "jwt",
            "user": {"uuid": "user-uuid"},
            "is_logged_in": True,
        },
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.ezlohacloud.get_system_architecture",
            AsyncMock(return_value="amd64"),
        ),
        patch(
            "homeassistant.components.ezlohacloud.install_frpc",
            AsyncMock(return_value="/fake/bin/frpc"),
        ),
        patch(
            "homeassistant.components.ezlohacloud.setup_frpc_configuration",
            AsyncMock(return_value=True),
        ) as setup_config,
    ):
        assert await async_setup_entry(hass, entry) is True
    setup_config.assert_awaited_once()


async def test_async_setup_entry_raises_config_entry_not_ready(
    hass: HomeAssistant,
) -> None:
    """Any unexpected exception during setup becomes ConfigEntryNotReady."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"auth_token": "jwt", "user": {"uuid": "user-uuid"}},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.ezlohacloud.get_system_architecture",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)


# ── setup_frpc_configuration ────────────────────────────────────────


async def test_setup_frpc_configuration_no_token(hass: HomeAssistant) -> None:
    """Missing token returns False without touching FRP."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)

    with patch(
        "homeassistant.components.ezlohacloud.fetch_and_update_frp_config",
        AsyncMock(),
    ) as fetch:
        assert await setup_frpc_configuration(hass, entry, "/fake/bin/frpc") is False
    fetch.assert_not_awaited()


async def test_setup_frpc_configuration_success(hass: HomeAssistant) -> None:
    """Happy path runs trusted-proxy update, fetches config, and starts frpc."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"auth_token": "jwt", "user": {"uuid": "user-uuid"}},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.ezlohacloud.ensure_trusted_proxy_config",
            return_value=False,
        ),
        patch(
            "homeassistant.components.ezlohacloud.fetch_and_update_frp_config",
            AsyncMock(),
        ) as fetch,
        patch("homeassistant.components.ezlohacloud.start_frpc", AsyncMock()) as start,
    ):
        assert await setup_frpc_configuration(hass, entry, "/fake/bin/frpc") is True

    fetch.assert_awaited_once()
    start.assert_awaited_once()


async def test_setup_frpc_configuration_subscription_expired(
    hass: HomeAssistant,
) -> None:
    """SubscriptionExpiredError sets canceled state and returns True (don't fail)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"auth_token": "jwt", "user": {"uuid": "user-uuid"}},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.ezlohacloud.ensure_trusted_proxy_config",
            return_value=False,
        ),
        patch(
            "homeassistant.components.ezlohacloud.fetch_and_update_frp_config",
            AsyncMock(side_effect=SubscriptionExpiredError("expired")),
        ),
    ):
        assert await setup_frpc_configuration(hass, entry, "/fake/bin/frpc") is True

    assert entry.data["subscription_status"] == SUBSCRIPTION_CANCELED
    assert entry.data["payment_required"] is True


async def test_setup_frpc_configuration_oserror(hass: HomeAssistant) -> None:
    """Generic OSError during fetch returns False."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"auth_token": "jwt", "user": {"uuid": "user-uuid"}},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.ezlohacloud.ensure_trusted_proxy_config",
            return_value=False,
        ),
        patch(
            "homeassistant.components.ezlohacloud.fetch_and_update_frp_config",
            AsyncMock(side_effect=OSError("disk full")),
        ),
    ):
        assert await setup_frpc_configuration(hass, entry, "/fake/bin/frpc") is False


# ── async_unload_entry ──────────────────────────────────────────────


async def test_async_unload_entry_calls_stop_frpc(hass: HomeAssistant) -> None:
    """Unloading the entry delegates to stop_frpc."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)

    with patch("homeassistant.components.ezlohacloud.stop_frpc", AsyncMock()) as stop:
        assert await async_unload_entry(hass, entry) is True
    stop.assert_awaited_once_with(hass, entry)
