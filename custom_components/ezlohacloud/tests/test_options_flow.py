"""Tests for the Ezlo HA Cloud options flow."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.ezlohacloud.const import (
    CONF_API_URI,
    DEFAULT_API_URI,
    DOMAIN,
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_CANCELED,
    SUBSCRIPTION_INTERNAL,
    SUBSCRIPTION_PARTNER_TRIAL,
    SUBSCRIPTION_PARTNER_TRIAL_EXPIRED,
    SUBSCRIPTION_PAST_DUE,
    SUBSCRIPTION_TRIALING,
)
from homeassistant.components.ezlohacloud.options_flow import (
    EzloOptionsFlowHandler,
    _compute_trial_days,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from tests.common import MockConfigEntry

# ── _compute_trial_days ─────────────────────────────────────────────


def test_compute_trial_days_future() -> None:
    """A future ISO datetime returns positive days remaining."""
    # Add a small buffer to avoid the integer-truncation off-by-one when
    # clock time advances between `datetime.now()` calls.
    future = (datetime.now(UTC) + timedelta(days=10, hours=1)).isoformat()
    assert _compute_trial_days(future) == 10


def test_compute_trial_days_past() -> None:
    """A past datetime returns 0 (never negative)."""
    past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    assert _compute_trial_days(past) == 0


def test_compute_trial_days_none() -> None:
    """None or empty string returns None."""
    assert _compute_trial_days(None) is None
    assert _compute_trial_days("") is None


def test_compute_trial_days_invalid() -> None:
    """Invalid datetime strings return None instead of raising."""
    assert _compute_trial_days("not-a-date") is None


def test_compute_trial_days_with_z_suffix() -> None:
    """RFC3339 'Z' suffix is handled."""
    future = (datetime.now(UTC) + timedelta(days=15, hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert _compute_trial_days(future) == 15


# ── Fixtures and helpers ────────────────────────────────────────────


@pytest.fixture
async def configured_entry(hass: HomeAssistant) -> ConfigEntry:
    """Set up an empty config entry the way the integration creates it."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def handler(configured_entry: ConfigEntry) -> EzloOptionsFlowHandler:
    """Build a handler bound to the fixture entry."""
    return EzloOptionsFlowHandler(configured_entry)


def _patch_login_side_effects():
    """Patch FRP/aiohttp side-effects triggered after successful login."""
    return [
        patch(
            "homeassistant.components.ezlohacloud.options_flow.fetch_and_update_frp_config",
            AsyncMock(return_value={"server_name": "x.ezlo.com", "subdomain": "abc"}),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.start_frpc",
            AsyncMock(),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-instance-uuid"),
        ),
    ]


# ── async_step_init ─────────────────────────────────────────────────


async def test_init_not_logged_in_shows_login_signup(
    hass: HomeAssistant, handler: EzloOptionsFlowHandler
) -> None:
    """When not logged in, the menu shows login + signup options."""
    handler.hass = hass
    result = await handler.async_step_init()
    assert result["type"] is FlowResultType.MENU
    assert "login" in result["menu_options"]
    assert "signup" in result["menu_options"]


async def test_init_logged_in_shows_cloud_status(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """When logged in with valid sub, the menu shows status options."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "subscription_status": SUBSCRIPTION_ACTIVE,
        },
    )
    handler.hass = hass
    result = await handler.async_step_init()
    assert result["type"] is FlowResultType.MENU
    assert "cloud_status" in result["menu_options"]
    assert "view_status" in result["menu_options"]
    assert "logout" in result["menu_options"]
    assert "subscribe" not in result["menu_options"]


@pytest.mark.parametrize(
    "status",
    [SUBSCRIPTION_PAST_DUE, SUBSCRIPTION_CANCELED, SUBSCRIPTION_PARTNER_TRIAL_EXPIRED],
)
async def test_init_logged_in_invalid_subscription_shows_resubscribe(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
    status: str,
) -> None:
    """Invalid subscription states surface a Resubscribe option."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "subscription_status": status,
        },
    )
    handler.hass = hass
    result = await handler.async_step_init()
    assert "subscribe" in result["menu_options"]


# ── async_step_login ────────────────────────────────────────────────


async def test_login_form_renders_on_first_call(
    hass: HomeAssistant, handler: EzloOptionsFlowHandler
) -> None:
    """No input → renders the login form with empty error placeholder."""
    handler.hass = hass
    with patch(
        "homeassistant.components.ezlohacloud.options_flow.async_get_instance_id",
        AsyncMock(return_value="ha-instance-uuid"),
    ):
        result = await handler.async_step_login()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "login"
    assert result["description_placeholders"] == {"error_detail": ""}


async def test_login_invalid_credentials_shows_error(
    hass: HomeAssistant, handler: EzloOptionsFlowHandler
) -> None:
    """Failed auth → renders form with error_detail populated."""
    handler.hass = hass
    auth_response = {"success": False, "error": "ezlo login failed", "data": None}
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.authenticate",
            AsyncMock(return_value=auth_response),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-instance-uuid"),
        ),
    ):
        result = await handler.async_step_login({"username": "u", "password": "p"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_credentials"}
    assert result["description_placeholders"]["error_detail"] == "ezlo login failed"


async def test_login_success_starts_frpc_and_aborts(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Successful login starts frpc and aborts with login_successful."""
    handler.hass = hass
    auth_response = {
        "success": True,
        "data": {
            "token": "jwt",
            "tunnel_token": "tt",
            "user": {
                "uuid": "user-uuid",
                "username": "user",
                "email": "u@x.com",
                "ezlo_id": 42,
            },
            "subscription_status": SUBSCRIPTION_ACTIVE,
            "is_trial": False,
            "payment_required": False,
            "trial_ends_at": None,
            "checkout_url": None,
        },
        "error": None,
    }
    patches = [
        patch(
            "homeassistant.components.ezlohacloud.options_flow.authenticate",
            AsyncMock(return_value=auth_response),
        ),
        *_patch_login_side_effects(),
    ]
    for p in patches:
        p.start()
    try:
        result = await handler.async_step_login({"username": "u", "password": "p"})
    finally:
        for p in patches:
            p.stop()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "login_successful"
    # Token and login state persisted
    assert configured_entry.data["auth_token"] == "jwt"
    assert configured_entry.data["tunnel_token"] == "tt"
    assert configured_entry.data["is_logged_in"] is True


async def test_login_payment_required_routes_to_subscribe(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """When payment_required=True, login routes through the subscribe step."""
    handler.hass = hass
    auth_response = {
        "success": True,
        "data": {
            "token": "jwt",
            "tunnel_token": "tt",
            "user": {
                "uuid": "user-uuid",
                "username": "user",
                "email": "u@x.com",
                "ezlo_id": 42,
            },
            "subscription_status": "",
            "is_trial": False,
            "payment_required": True,
            "trial_ends_at": None,
            "checkout_url": "https://checkout.stripe.com/abc",
        },
        "error": None,
    }
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.authenticate",
            AsyncMock(return_value=auth_response),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
    ):
        result = await handler.async_step_login({"username": "u", "password": "p"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "subscribe"
    assert (
        result["description_placeholders"]["url"] == "https://checkout.stripe.com/abc"
    )
    # Logged-in flag NOT set until trial activates
    assert configured_entry.data.get("is_logged_in") is not True


# ── async_step_signup ───────────────────────────────────────────────


async def test_signup_form_renders_on_first_call(
    hass: HomeAssistant, handler: EzloOptionsFlowHandler
) -> None:
    """No input → renders signup form with empty error detail."""
    handler.hass = hass
    result = await handler.async_step_signup()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "signup"
    assert result["description_placeholders"]["error_detail"] == ""


async def test_signup_failure_shows_backend_error(
    hass: HomeAssistant, handler: EzloOptionsFlowHandler
) -> None:
    """Backend signup failure surfaces the API's error message."""
    handler.hass = hass
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.signup",
            AsyncMock(
                return_value={"success": False, "error": "Username already exists"}
            ),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
    ):
        result = await handler.async_step_signup(
            {"username": "u", "email": "u@x.com", "password": "p"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "signup_failed"}
    assert (
        result["description_placeholders"]["error_detail"] == "Username already exists"
    )


async def test_signup_success_routes_to_subscribe(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Successful signup stores tokens and routes to subscribe with checkout_url."""
    handler.hass = hass
    # Build a real JWT-shaped token so decode succeeds
    import base64
    import json as json_mod

    payload = {"uuid": "new-user-uuid", "ezlo_user_id": 99}
    token = (
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(json_mod.dumps(payload).encode())
        .rstrip(b"=")
        .decode()
        + ".sig"
    )

    signup_response = {
        "success": True,
        "data": {
            "token": token,
            "tunnel_token": "tt",
            "trial_ends_at": "2026-06-01T00:00:00Z",
            "subscription_status": "",
            "is_trial": False,
            "payment_required": True,
            "checkout_url": "https://checkout.stripe.com/new",
        },
    }
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.signup",
            AsyncMock(return_value=signup_response),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
    ):
        result = await handler.async_step_signup(
            {"username": "new", "email": "n@x.com", "password": "p"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "subscribe"
    assert (
        result["description_placeholders"]["url"] == "https://checkout.stripe.com/new"
    )
    # User info stored but not yet logged in
    assert configured_entry.data["auth_token"] == token
    assert configured_entry.data.get("is_logged_in") is not True


# ── async_step_logout ───────────────────────────────────────────────


async def test_logout_clears_state_and_stops_frpc(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Logout clears tokens, calls stop_frpc, and aborts with logged_out."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "auth_token": "jwt",
            "tunnel_token": "tt",
        },
    )
    handler.hass = hass
    with patch(
        "homeassistant.components.ezlohacloud.options_flow.stop_frpc", AsyncMock()
    ) as stop:
        result = await handler.async_step_logout()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "logged_out"
    assert configured_entry.data["is_logged_in"] is False
    assert configured_entry.data["auth_token"] is None
    assert configured_entry.data["tunnel_token"] is None
    stop.assert_awaited_once()


# ── async_step_cloud_status ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected_phrase"),
    [
        (SUBSCRIPTION_TRIALING, "Free trial"),
        (SUBSCRIPTION_ACTIVE, "Subscription active"),
        (SUBSCRIPTION_PAST_DUE, "last payment failed"),
        (SUBSCRIPTION_CANCELED, "canceled"),
        (SUBSCRIPTION_INTERNAL, "Internal user"),
        (SUBSCRIPTION_PARTNER_TRIAL, "Partner trial"),
        (SUBSCRIPTION_PARTNER_TRIAL_EXPIRED, "partner trial has expired"),
    ],
)
async def test_cloud_status_renders_per_subscription_state(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
    status: str,
    expected_phrase: str,
) -> None:
    """The cloud_status menu description reflects the subscription state."""
    future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "user": {"username": "alice"},
            "subscription_status": status,
            "trial_ends_at": future,
            "server_name": "x.ezlo.com",
            "subdomain": "abc",
        },
    )
    handler.hass = hass
    result = await handler.async_step_cloud_status()
    assert result["type"] is FlowResultType.MENU
    assert (
        expected_phrase.lower()
        in result["description_placeholders"]["trial_info"].lower()
    )
    assert result["description_placeholders"]["username"] == "alice"
    assert result["description_placeholders"]["cloud_url"] == "https://abc.x.ezlo.com"


async def test_cloud_status_no_cloud_url(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Missing subdomain/server_name reports 'Not available'."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={**configured_entry.data, "is_logged_in": True, "user": {}},
    )
    handler.hass = hass
    result = await handler.async_step_cloud_status()
    assert result["description_placeholders"]["cloud_url"] == "Not available"
    assert result["description_placeholders"]["username"] == "Unknown"


# ── async_step_subscribe ────────────────────────────────────────────


async def test_subscribe_with_prebuilt_checkout_url(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """A pre-supplied checkout_url is shown directly (no extra Stripe call)."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": "user-uuid"},
            "auth_token": "jwt",
        },
    )
    handler.hass = hass
    with patch(
        "homeassistant.components.ezlohacloud.options_flow.create_stripe_session",
        AsyncMock(),
    ) as create_session:
        result = await handler.async_step_subscribe(
            checkout_url="https://prebuilt.example.com"
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "subscribe"
    assert result["description_placeholders"]["url"] == "https://prebuilt.example.com"
    # No new session minted when one was supplied
    create_session.assert_not_awaited()


async def test_subscribe_fetches_fresh_checkout_url_when_none_supplied(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """No pre-supplied URL → calls create-session with the configured price id."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": "user-uuid"},
            "auth_token": "jwt",
        },
    )
    handler.hass = hass
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.get_integration_config",
            AsyncMock(return_value={"stripe_price_id": "price_xyz"}),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.create_stripe_session",
            AsyncMock(
                return_value={
                    "success": True,
                    "data": {"checkout_url": "https://fresh.example.com"},
                }
            ),
        ) as create_session,
    ):
        result = await handler.async_step_subscribe()

    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["url"] == "https://fresh.example.com"
    # Called with the dynamically-fetched price id
    create_session.assert_awaited_once()
    args = create_session.await_args.args
    assert args[2] == "price_xyz"


async def test_subscribe_aborts_when_config_unavailable(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """If integration config can't be fetched, the flow aborts cleanly."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": "user-uuid"},
            "auth_token": "jwt",
        },
    )
    handler.hass = hass
    with patch(
        "homeassistant.components.ezlohacloud.options_flow.get_integration_config",
        AsyncMock(return_value=None),
    ):
        result = await handler.async_step_subscribe()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "config_unavailable"


async def test_subscribe_aborts_when_no_user_uuid(
    hass: HomeAssistant, handler: EzloOptionsFlowHandler
) -> None:
    """No user_uuid in config → session_expired abort."""
    handler.hass = hass
    result = await handler.async_step_subscribe()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "session_expired"


async def test_subscribe_aborts_when_stripe_session_fails(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Stripe API error during create-session aborts with stripe_failed."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": "user-uuid"},
            "auth_token": "jwt",
        },
    )
    handler.hass = hass
    with (
        patch(
            "homeassistant.components.ezlohacloud.options_flow.get_integration_config",
            AsyncMock(return_value={"stripe_price_id": "price_x"}),
        ),
        patch(
            "homeassistant.components.ezlohacloud.options_flow.create_stripe_session",
            AsyncMock(return_value={"success": False, "error": "down"}),
        ),
    ):
        result = await handler.async_step_subscribe()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "stripe_failed"


# ── async_step_view_status ──────────────────────────────────────────


async def test_view_status_invalid_state_shows_resubscribe(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """past_due / canceled / partner_trial_expired exposes a Resubscribe button."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": "u"},
            "subscription_status": SUBSCRIPTION_PAST_DUE,
        },
    )
    handler.hass = hass
    with patch(
        "homeassistant.components.ezlohacloud.options_flow.get_subscription_status",
        AsyncMock(
            return_value={"success": True, "status": "past_due", "is_active": False}
        ),
    ):
        result = await handler.async_step_view_status()
    assert "subscribe" in result["menu_options"]
    assert "init" in result["menu_options"]


async def test_view_status_active_no_resubscribe(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Active subscription only shows the Back option."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": "u"},
            "subscription_status": SUBSCRIPTION_ACTIVE,
        },
    )
    handler.hass = hass
    with patch(
        "homeassistant.components.ezlohacloud.options_flow.get_subscription_status",
        AsyncMock(
            return_value={"success": True, "status": "active", "is_active": True}
        ),
    ):
        result = await handler.async_step_view_status()
    assert "subscribe" not in result["menu_options"]
    assert "init" in result["menu_options"]


# ── Advanced API endpoint override ──────────────────────────────────


def test_get_api_uri_defaults_when_not_set(
    handler: EzloOptionsFlowHandler,
) -> None:
    """Without an override in entry data, _get_api_uri returns DEFAULT_API_URI."""
    assert handler._get_api_uri() == DEFAULT_API_URI


def test_get_api_uri_returns_override_when_set(
    hass: HomeAssistant,
    configured_entry: ConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """When entry data has CONF_API_URI, that value is returned."""
    dev_api = "https://api-dev.harc.cloud"
    hass.config_entries.async_update_entry(
        configured_entry, data={**configured_entry.data, CONF_API_URI: dev_api}
    )
    assert handler._get_api_uri() == dev_api


async def test_advanced_step_hidden_when_advanced_options_off(
    hass: HomeAssistant, configured_entry: ConfigEntry
) -> None:
    """The advanced menu entry is suppressed when show_advanced_options is False."""
    result = await hass.config_entries.options.async_init(
        configured_entry.entry_id, context={"show_advanced_options": False}
    )
    assert result["type"] is FlowResultType.MENU
    assert "advanced" not in result["menu_options"]


async def test_advanced_step_visible_when_advanced_options_on(
    hass: HomeAssistant, configured_entry: ConfigEntry
) -> None:
    """The advanced menu entry appears when show_advanced_options is True."""
    result = await hass.config_entries.options.async_init(
        configured_entry.entry_id, context={"show_advanced_options": True}
    )
    assert result["type"] is FlowResultType.MENU
    assert "advanced" in result["menu_options"]


async def test_advanced_step_persists_override(
    hass: HomeAssistant, configured_entry: ConfigEntry
) -> None:
    """Submitting the advanced form writes CONF_API_URI into entry data."""
    dev_api = "https://api-dev.harc.cloud"
    result = await hass.config_entries.options.async_init(
        configured_entry.entry_id, context={"show_advanced_options": True}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "advanced"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_API_URI: dev_api}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "config_saved"
    assert configured_entry.data[CONF_API_URI] == dev_api


async def test_advanced_step_clearing_field_removes_override(
    hass: HomeAssistant, configured_entry: ConfigEntry
) -> None:
    """Submitting an empty api_uri removes the override (revert to default)."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={**configured_entry.data, CONF_API_URI: "https://api-dev.harc.cloud"},
    )
    result = await hass.config_entries.options.async_init(
        configured_entry.entry_id, context={"show_advanced_options": True}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_API_URI: ""}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "config_saved"
    assert CONF_API_URI not in configured_entry.data

