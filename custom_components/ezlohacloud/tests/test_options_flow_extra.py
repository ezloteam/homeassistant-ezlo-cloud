"""Additional options-flow tests: polling, redirect handlers, helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.ezlohacloud.const import (
    DOMAIN,
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_TRIALING,
)
from homeassistant.components.ezlohacloud.options_flow import EzloOptionsFlowHandler
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.network import NoURLAvailableError

from tests.common import MockConfigEntry


@pytest.fixture
async def configured_entry(hass: HomeAssistant) -> ConfigEntry:
    """Create the empty config entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def handler(
    hass: HomeAssistant, configured_entry: ConfigEntry
) -> EzloOptionsFlowHandler:
    """Build a handler bound to hass + the fixture entry."""
    h = EzloOptionsFlowHandler(configured_entry)
    h.hass = hass
    return h


# ── _get_abort_placeholders ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "trial_ends_at", "expected_phrase"),
    [
        (SUBSCRIPTION_TRIALING, "2099-01-01T00:00:00Z", "free trial"),
        (SUBSCRIPTION_TRIALING, None, "free trial"),
        ("internal", None, "Internal user"),
        ("partner_trial", "2099-01-01T00:00:00Z", "Partner trial"),
        ("partner_trial", None, "Partner trial active"),
        ("partner_trial_expired", None, "partner trial has expired"),
        (SUBSCRIPTION_ACTIVE, None, "subscription is active"),
        ("past_due", None, "last payment failed"),
        ("canceled", None, "was canceled"),
        ("", None, ""),
    ],
)
def test_get_abort_placeholders_trial_text(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
    status: str,
    trial_ends_at: str | None,
    expected_phrase: str,
) -> None:
    """Abort placeholders carry state-specific trial_info text."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "subscription_status": status,
            "trial_ends_at": trial_ends_at,
        },
    )
    placeholders = handler._get_abort_placeholders()
    assert expected_phrase.lower() in placeholders["trial_info"].lower()
    # No cloud URL configured → "Not yet available"
    assert placeholders["cloud_url"] == "Not yet available"


def test_get_abort_placeholders_with_cloud_url(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """When server_name + subdomain exist, cloud_url is built."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "server_name": "frp.ezlo.com",
            "subdomain": "hash1",
        },
    )
    placeholders = handler._get_abort_placeholders()
    assert placeholders["cloud_url"] == "https://hash1.frp.ezlo.com"


# ── _get_base_url ────────────────────────────────────────────────────


def test_get_base_url_prefers_external(handler: EzloOptionsFlowHandler) -> None:
    """When an external URL is available, it is used."""
    with patch(
        "homeassistant.components.ezlohacloud.options_flow.get_url",
        return_value="https://external.example.com",
    ):
        assert handler._get_base_url() == "https://external.example.com"


def test_get_base_url_falls_back_to_current_request(
    handler: EzloOptionsFlowHandler,
) -> None:
    """If external isn't available, falls back to the current request URL."""

    def _side_effect(hass, **kwargs):
        if kwargs.get("allow_internal") is False:
            raise NoURLAvailableError
        if kwargs.get("require_current_request"):
            return "http://192.168.1.5:8123"
        raise NoURLAvailableError

    with patch(
        "homeassistant.components.ezlohacloud.options_flow.get_url",
        side_effect=_side_effect,
    ):
        assert handler._get_base_url() == "http://192.168.1.5:8123"


def test_get_base_url_final_fallback(handler: EzloOptionsFlowHandler) -> None:
    """If no URL can be resolved at all, falls back to homeassistant.local."""
    with patch(
        "homeassistant.components.ezlohacloud.options_flow.get_url",
        side_effect=NoURLAvailableError,
    ):
        assert handler._get_base_url() == "http://homeassistant.local:8123"


# ── _handle_successful_login error handling ──────────────────────────


async def test_handle_successful_login_frp_failure_still_logs_in(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """If FRP config/start fails, login state is still persisted."""
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.fetch_and_update_frp_config",
            AsyncMock(side_effect=OSError("network down")),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.start_frpc",
            AsyncMock(),
        ),
    ):
        await handler._handle_successful_login(
            "jwt",
            {"uuid": "u", "username": "user", "email": "e", "ezlo_id": 1},
            tunnel_token="tt",
            subscription_status=SUBSCRIPTION_ACTIVE,
        )

    # Even though FRP failed, the user is recorded as logged in
    assert configured_entry.data["is_logged_in"] is True
    assert configured_entry.data["auth_token"] == "jwt"


async def test_handle_successful_login_persists_server_details(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Server name / subdomain from FRP config are written to the entry."""
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.fetch_and_update_frp_config",
            AsyncMock(
                return_value={"server_name": "frp.ezlo.com", "subdomain": "hash1"}
            ),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.start_frpc",
            AsyncMock(),
        ),
    ):
        await handler._handle_successful_login(
            "jwt",
            {"uuid": "u", "username": "user", "email": "e", "ezlo_id": 1},
            tunnel_token="tt",
            subscription_status=SUBSCRIPTION_TRIALING,
            trial_ends_at="2026-06-01T00:00:00Z",
        )

    assert configured_entry.data["server_name"] == "frp.ezlo.com"
    assert configured_entry.data["subdomain"] == "hash1"
    assert configured_entry.data["subscription_status"] == SUBSCRIPTION_TRIALING


# ── _poll_payment_and_login ──────────────────────────────────────────


async def test_poll_payment_completes_on_active(
    hass: HomeAssistant, handler: EzloOptionsFlowHandler
) -> None:
    """Polling completes login as soon as the subscription becomes active."""
    status_responses = [
        {"success": False, "error": "http_404"},  # webhook not processed yet
        {"success": True, "status": SUBSCRIPTION_ACTIVE, "is_active": True},
    ]
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.asyncio.sleep",
            AsyncMock(),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.get_subscription_status",
            AsyncMock(side_effect=status_responses),
        ),
        patch.object(handler, "_handle_successful_login", AsyncMock()) as handle_login,
    ):
        await handler._poll_payment_and_login(
            "user-uuid",
            "jwt",
            "tt",
            {"uuid": "user-uuid", "username": "u", "email": "e", "ezlo_id": 1},
        )

    handle_login.assert_awaited_once()
    # Completed with the active status
    assert handle_login.await_args.kwargs["subscription_status"] == (
        SUBSCRIPTION_ACTIVE
    )


async def test_poll_payment_times_out(
    hass: HomeAssistant, handler: EzloOptionsFlowHandler
) -> None:
    """If the subscription never activates, polling exits without login."""
    # Patch attempts down to a tiny number via timeout math isn't exposed,
    # so just always return not-yet-active and cap sleep to no-op. We rely on
    # the loop returning after exhausting attempts; to keep the test fast we
    # make get_subscription_status raise StopAsyncIteration-like exhaustion by
    # returning a fixed not-active payload and limiting the range via patching.
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.asyncio.sleep",
            AsyncMock(),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.get_subscription_status",
            AsyncMock(
                return_value={
                    "success": True,
                    "status": "incomplete",
                    "is_active": False,
                }
            ),
        ),
        patch.object(handler, "_handle_successful_login", AsyncMock()) as handle_login,
        # Shrink the loop: 1 attempt only
        patch(
            "homeassistant.components.ezlohacloud.options_flow.range",
            return_value=range(1),
        ),
    ):
        await handler._poll_payment_and_login(
            "user-uuid",
            "jwt",
            "tt",
            {"uuid": "user-uuid"},
        )

    handle_login.assert_not_awaited()


# ── async_step_stripe_finish / async_step_redirecting ────────────────


async def test_stripe_finish_marks_active(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """stripe_finish flips subscription_status to active."""
    await handler.async_step_stripe_finish()
    assert configured_entry.data["subscription_status"] == SUBSCRIPTION_ACTIVE


async def test_redirecting_when_not_logged_in(
    hass: HomeAssistant, handler: EzloOptionsFlowHandler
) -> None:
    """If not logged in yet, redirecting aborts with stripe_redirect_finished."""
    result = await handler.async_step_redirecting()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "stripe_redirect_finished"


async def test_redirecting_when_active(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """If logged in and active, aborts with subscription_activated."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "subscription_status": SUBSCRIPTION_ACTIVE,
        },
    )
    result = await handler.async_step_redirecting()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "subscription_activated"


async def test_redirecting_when_logged_in_trialing(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """If logged in but not active, aborts with login_successful."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "subscription_status": SUBSCRIPTION_TRIALING,
        },
    )
    result = await handler.async_step_redirecting()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "login_successful"


# ── force logout ─────────────────────────────────────────────────────


async def test_force_logout_clears_state(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """force_logout clears login state and aborts with session_expired."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={**configured_entry.data, "is_logged_in": True, "auth_token": "jwt"},
    )
    result = await handler.async_step_force_logout()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "session_expired"
    assert configured_entry.data["is_logged_in"] is False
