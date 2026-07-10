"""Tests for the Ezlo HA Cloud options flow."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.network import NoURLAvailableError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezlocloudharc.api import (
    AuthResult,
    StripeSession,
    SubscriptionStatusResult,
    UserDict,
)
from custom_components.ezlocloudharc.const import (
    CONF_API_URI,
    DEFAULT_API_URI,
    DOMAIN,
    SubscriptionStatus,
)
from custom_components.ezlocloudharc.exceptions import (
    EzloApiUnreachableError,
    EzloAuthError,
)
from custom_components.ezlocloudharc.models import EzloRuntimeData
from custom_components.ezlocloudharc.options_flow import (
    EzloOptionsFlowHandler,
    compute_trial_days,
)

USER_UUID = "f960d12e-4ccb-4f0a-b37f-0abee2cd9717"


def _make_jwt(payload: dict[str, Any]) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.signature"


def _auth_result(*, payment_required: bool = False) -> AuthResult:
    return AuthResult(
        token=_make_jwt({"uuid": USER_UUID, "ezlo_user_id": 42}),
        tunnel_token="tt",
        user=UserDict(
            uuid=USER_UUID,
            username="alice",
            email="alice@example.com",
            ezlo_id=42,
        ),
        subscription_status="" if payment_required else SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        payment_required=payment_required,
        trial_ends_at=None,
        checkout_url="https://checkout.stripe.com/abc" if payment_required else None,
    )


# ── compute_trial_days ──────────────────────────────────────────────


def test_compute_trial_days_future() -> None:
    """A future ISO datetime returns positive days remaining."""
    future = (datetime.now(UTC) + timedelta(days=10, hours=1)).isoformat()
    assert compute_trial_days(future) == 10


def test_compute_trial_days_past() -> None:
    """A past datetime returns 0 (never negative)."""
    past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    assert compute_trial_days(past) == 0


def test_compute_trial_days_none() -> None:
    """None or empty string returns None."""
    assert compute_trial_days(None) is None
    assert compute_trial_days("") is None


def test_compute_trial_days_invalid() -> None:
    """Invalid datetime strings return None instead of raising."""
    assert compute_trial_days("not-a-date") is None


def test_compute_trial_days_with_z_suffix() -> None:
    """RFC3339 'Z' suffix is handled."""
    future = (datetime.now(UTC) + timedelta(days=15, hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert compute_trial_days(future) == 15


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def configured_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An empty config entry with a runtime_data attached so handlers can read it."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    entry.runtime_data = EzloRuntimeData()
    return entry


@pytest.fixture
def handler(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> EzloOptionsFlowHandler:
    """An EzloOptionsFlowHandler bound to hass + the fixture entry."""
    h = EzloOptionsFlowHandler(configured_entry)
    h.hass = hass
    return h


# ── async_step_init ─────────────────────────────────────────────────


async def test_init_not_logged_in_shows_login_signup(
    handler: EzloOptionsFlowHandler,
) -> None:
    """When not logged in, the menu shows login + signup options."""
    result = await handler.async_step_init()
    assert result["type"] is FlowResultType.MENU
    assert "login" in result["menu_options"]
    assert "signup" in result["menu_options"]


async def test_init_logged_in_shows_cloud_status(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """When logged in with valid sub, the menu shows status options."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "subscription_status": SubscriptionStatus.ACTIVE.value,
        },
    )
    result = await handler.async_step_init()
    assert result["type"] is FlowResultType.MENU
    assert "cloud_status" in result["menu_options"]
    assert "logout" in result["menu_options"]
    assert "subscribe" not in result["menu_options"]


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.PAST_DUE.value,
        SubscriptionStatus.CANCELED.value,
        SubscriptionStatus.PARTNER_TRIAL_EXPIRED.value,
    ],
)
async def test_init_invalid_subscription_shows_resubscribe(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
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
    result = await handler.async_step_init()
    assert "subscribe" in result["menu_options"]


# ── async_step_login ────────────────────────────────────────────────


async def test_login_form_renders_on_first_call(
    handler: EzloOptionsFlowHandler,
) -> None:
    """No input → renders the login form with empty error placeholder."""
    result = await handler.async_step_login()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "login"
    assert result["description_placeholders"] == {"error_detail": ""}


async def test_login_invalid_credentials_shows_error(
    handler: EzloOptionsFlowHandler,
) -> None:
    """A typed EzloAuthError maps to errors[base]=invalid_credentials."""
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.authenticate",
            AsyncMock(side_effect=EzloAuthError("invalid credentials")),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
    ):
        result = await handler.async_step_login({"username": "u", "password": "p"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_credentials"}


async def test_login_network_error_shows_error(
    handler: EzloOptionsFlowHandler,
) -> None:
    """An EzloApiUnreachableError maps to errors[base]=network_error."""
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.authenticate",
            AsyncMock(side_effect=EzloApiUnreachableError("dns")),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
    ):
        result = await handler.async_step_login({"username": "u", "password": "p"})
    assert result["errors"] == {"base": "network_error"}


async def test_login_success_aborts_login_successful(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Successful login starts frpc, persists tokens, aborts login_successful."""
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.authenticate",
            AsyncMock(return_value=_auth_result()),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.fetch_and_update_frp_config",
            AsyncMock(
                return_value={"server_name": "x.ezlo.com", "subdomain": "abc"}
            ),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.start_frpc", AsyncMock()
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
    ):
        result = await handler.async_step_login({"username": "u", "password": "p"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "login_successful"
    assert configured_entry.data["auth_token"].startswith("eyJ")
    assert configured_entry.data["tunnel_token"] == "tt"
    assert configured_entry.data["is_logged_in"] is True


async def test_login_payment_required_routes_to_subscribe(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """payment_required=True routes through async_step_subscribe."""
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.authenticate",
            AsyncMock(return_value=_auth_result(payment_required=True)),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
    ):
        result = await handler.async_step_login({"username": "u", "password": "p"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "subscribe"
    assert (
        result["description_placeholders"]["url"] == "https://checkout.stripe.com/abc"
    )
    assert configured_entry.data.get("is_logged_in") is not True


# ── async_step_signup ───────────────────────────────────────────────


async def test_signup_form_renders_on_first_call(
    handler: EzloOptionsFlowHandler,
) -> None:
    """No input → renders signup form with empty error detail."""
    result = await handler.async_step_signup()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "signup"
    assert result["description_placeholders"]["error_detail"] == ""


async def test_signup_failure_shows_backend_error(
    handler: EzloOptionsFlowHandler,
) -> None:
    """A signup-failure exception surfaces under errors[base]=signup_failed."""
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.signup",
            AsyncMock(side_effect=EzloAuthError("Username taken")),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
    ):
        result = await handler.async_step_signup(
            {"username": "u", "email": "u@x.com", "password": "p"}
        )
    assert result["errors"] == {"base": "signup_failed"}


async def test_signup_payment_required_routes_to_subscribe(
    handler: EzloOptionsFlowHandler,
) -> None:
    """Successful signup with payment_required routes to subscribe."""
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.signup",
            AsyncMock(return_value=_auth_result(payment_required=True)),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
    ):
        result = await handler.async_step_signup(
            {"username": "u", "email": "u@x.com", "password": "p"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "subscribe"


# ── async_step_logout ───────────────────────────────────────────────


async def test_logout_clears_state_and_stops_frpc(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Logout wipes auth/user state, stops frpc, aborts logged_out."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "auth_token": "jwt",
            "tunnel_token": "tt",
        },
    )
    with patch(
        "custom_components.ezlocloudharc.options_flow.stop_frpc", AsyncMock()
    ) as stop:
        result = await handler.async_step_logout()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "logged_out"
    assert configured_entry.data["is_logged_in"] is False
    assert configured_entry.data["auth_token"] is None
    stop.assert_awaited_once()


# ── async_step_cloud_status ─────────────────────────────────────────


async def test_cloud_status_renders_connected_with_url(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """When connected and frp info present, cloud_status shows the URL."""
    configured_entry.runtime_data.is_connected = True  # type: ignore[union-attr]
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"username": "alice"},
            "server_name": "x.ezlo.com",
            "subdomain": "abc",
            "subscription_status": SubscriptionStatus.ACTIVE.value,
        },
    )
    result = await handler.async_step_cloud_status()
    assert result["type"] is FlowResultType.MENU
    assert result["description_placeholders"]["connection_status"] == "Connected"
    assert result["description_placeholders"]["cloud_url"] == "https://abc.x.ezlo.com"
    assert result["description_placeholders"]["username"] == "alice"


async def test_cloud_status_no_cloud_url(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Missing subdomain/server_name reports 'Not available'."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={**configured_entry.data, "is_logged_in": True, "user": {}},
    )
    result = await handler.async_step_cloud_status()
    assert result["description_placeholders"]["cloud_url"] == "Not available"
    assert result["description_placeholders"]["username"] == "Unknown"


# ── async_step_subscribe ────────────────────────────────────────────


async def test_subscribe_with_prebuilt_checkout_url(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """A pre-supplied checkout_url is shown directly (no Stripe call)."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": USER_UUID},
            "auth_token": "jwt",
        },
    )
    with patch(
        "custom_components.ezlocloudharc.options_flow.create_stripe_session",
        AsyncMock(),
    ) as create_session:
        result = await handler.async_step_subscribe(
            checkout_url="https://prebuilt.example.com"
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "subscribe"
    assert (
        result["description_placeholders"]["url"] == "https://prebuilt.example.com"
    )
    create_session.assert_not_awaited()


async def test_subscribe_fetches_checkout_url_when_none_supplied(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """No URL supplied → fetches integration config + mints a Stripe session."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": USER_UUID},
            "auth_token": "jwt",
        },
    )
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.get_integration_config",
            AsyncMock(return_value={"stripe_price_id": "price_xyz"}),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.create_stripe_session",
            AsyncMock(
                return_value=StripeSession(checkout_url="https://fresh.example.com")
            ),
        ) as create_session,
    ):
        result = await handler.async_step_subscribe()

    assert result["description_placeholders"]["url"] == "https://fresh.example.com"
    create_session.assert_awaited_once()
    assert create_session.await_args.args[2] == "price_xyz"


async def test_subscribe_aborts_when_no_user_uuid(
    handler: EzloOptionsFlowHandler,
) -> None:
    """No user uuid in entry data → session_expired."""
    result = await handler.async_step_subscribe()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "session_expired"


async def test_subscribe_aborts_when_config_unavailable(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Integration-config fetch raising an EzloError aborts config_unavailable."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": USER_UUID},
            "auth_token": "jwt",
        },
    )
    with patch(
        "custom_components.ezlocloudharc.options_flow.get_integration_config",
        AsyncMock(side_effect=EzloApiUnreachableError("dns")),
    ):
        result = await handler.async_step_subscribe()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "config_unavailable"


async def test_subscribe_aborts_when_stripe_session_fails(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Stripe API error during create-session aborts stripe_failed."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": USER_UUID},
            "auth_token": "jwt",
        },
    )
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.get_integration_config",
            AsyncMock(return_value={"stripe_price_id": "price_x"}),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.create_stripe_session",
            AsyncMock(side_effect=EzloApiUnreachableError("down")),
        ),
    ):
        result = await handler.async_step_subscribe()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "stripe_failed"


# ── view_status ─────────────────────────────────────────────────────


async def test_view_status_invalid_state_shows_resubscribe(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """past_due / canceled / partner_trial_expired exposes a Resubscribe button."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": USER_UUID},
            "subscription_status": SubscriptionStatus.PAST_DUE.value,
        },
    )
    with patch(
        "custom_components.ezlocloudharc.options_flow.get_subscription_status",
        AsyncMock(
            return_value=SubscriptionStatusResult(
                status="past_due",
                is_active=False,
                start_timestamp="",
                end_timestamp="",
            )
        ),
    ):
        result = await handler.async_step_view_status()
    assert "subscribe" in result["menu_options"]
    assert "init" in result["menu_options"]


async def test_view_status_active_no_resubscribe(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """An active subscription only shows the Back option."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "user": {"uuid": USER_UUID},
            "subscription_status": SubscriptionStatus.ACTIVE.value,
        },
    )
    with patch(
        "custom_components.ezlocloudharc.options_flow.get_subscription_status",
        AsyncMock(
            return_value=SubscriptionStatusResult(
                status="active",
                is_active=True,
                start_timestamp="",
                end_timestamp="",
            )
        ),
    ):
        result = await handler.async_step_view_status()
    assert "subscribe" not in result["menu_options"]
    assert "init" in result["menu_options"]


# ── Advanced API endpoint override ──────────────────────────────────


def test_get_api_uri_defaults_when_not_set(
    handler: EzloOptionsFlowHandler,
) -> None:
    """Without an override, _get_api_uri returns DEFAULT_API_URI."""
    assert handler._get_api_uri() == DEFAULT_API_URI


def test_get_api_uri_returns_override_when_set(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """When entry data has CONF_API_URI, that value is returned."""
    dev_api = "https://api-dev.harc.cloud"
    hass.config_entries.async_update_entry(
        configured_entry, data={**configured_entry.data, CONF_API_URI: dev_api}
    )
    assert handler._get_api_uri() == dev_api


async def test_advanced_step_hidden_when_advanced_options_off(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """The advanced menu entry is suppressed when show_advanced_options is False."""
    result = await hass.config_entries.options.async_init(
        configured_entry.entry_id, context={"show_advanced_options": False}
    )
    assert result["type"] is FlowResultType.MENU
    assert "advanced" not in result["menu_options"]


async def test_advanced_step_visible_when_advanced_options_on(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """The advanced menu entry appears when show_advanced_options is True."""
    result = await hass.config_entries.options.async_init(
        configured_entry.entry_id, context={"show_advanced_options": True}
    )
    assert result["type"] is FlowResultType.MENU
    assert "advanced" in result["menu_options"]


async def test_advanced_step_persists_override(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """Submitting the advanced form writes CONF_API_URI into entry data."""
    dev_api = "https://api-dev.harc.cloud"
    result = await hass.config_entries.options.async_init(
        configured_entry.entry_id, context={"show_advanced_options": True}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced"}
    )
    assert result["step_id"] == "advanced"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_API_URI: dev_api}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "config_saved"
    assert configured_entry.data[CONF_API_URI] == dev_api


async def test_advanced_step_clearing_field_removes_override(
    hass: HomeAssistant, configured_entry: MockConfigEntry
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
    assert result["reason"] == "config_saved"
    assert CONF_API_URI not in configured_entry.data


# ── _get_base_url ───────────────────────────────────────────────────


def test_get_base_url_prefers_external(handler: EzloOptionsFlowHandler) -> None:
    """When an external URL is available, it is used."""
    with patch(
        "custom_components.ezlocloudharc.options_flow.get_url",
        return_value="https://external.example.com",
    ):
        assert handler._get_base_url() == "https://external.example.com"


def test_get_base_url_final_fallback(handler: EzloOptionsFlowHandler) -> None:
    """If no URL can be resolved at all, falls back to homeassistant.local."""
    with patch(
        "custom_components.ezlocloudharc.options_flow.get_url",
        side_effect=NoURLAvailableError,
    ):
        assert handler._get_base_url() == "http://homeassistant.local:8123"


# ── classify_login_error ────────────────────────────────────────────


def test_classify_login_error_device_already_bound() -> None:
    """Device-already-bound errors map to that specific translation key."""
    from custom_components.ezlocloudharc.options_flow import classify_login_error

    key, detail = classify_login_error("device_already_bound: subdomain")
    assert key == "device_already_bound"
    assert "different Ezlo Cloud HARC account" in detail


def test_classify_login_error_invalid_credentials() -> None:
    """Credential-shaped errors map to invalid_credentials."""
    from custom_components.ezlocloudharc.options_flow import classify_login_error

    key, _ = classify_login_error("Invalid credentials")
    assert key == "invalid_credentials"


def test_classify_login_error_none_or_empty() -> None:
    """None or empty input returns the 'unknown' key with no detail."""
    from custom_components.ezlocloudharc.options_flow import classify_login_error

    key, detail = classify_login_error(None)
    assert key == "unknown"
    assert detail == ""
    key, _ = classify_login_error("")
    assert key == "unknown"


# ── Stripe return handlers ──────────────────────────────────────────


async def test_stripe_finish_marks_active(
    configured_entry: MockConfigEntry, handler: EzloOptionsFlowHandler
) -> None:
    """stripe_finish flips subscription_status to active."""
    await handler.async_step_stripe_finish()
    assert (
        configured_entry.data["subscription_status"]
        == SubscriptionStatus.ACTIVE.value
    )


async def test_redirecting_when_not_logged_in(
    handler: EzloOptionsFlowHandler,
) -> None:
    """If not logged in yet, redirecting aborts with stripe_redirect_finished."""
    result = await handler.async_step_redirecting()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "stripe_redirect_finished"


async def test_redirecting_when_active(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """If logged in and active, aborts with subscription_activated."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "subscription_status": SubscriptionStatus.ACTIVE.value,
        },
    )
    result = await handler.async_step_redirecting()
    assert result["reason"] == "subscription_activated"


async def test_redirecting_when_logged_in_trialing(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """If logged in but not active, aborts with login_successful."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "subscription_status": SubscriptionStatus.TRIALING.value,
        },
    )
    result = await handler.async_step_redirecting()
    assert result["reason"] == "login_successful"
