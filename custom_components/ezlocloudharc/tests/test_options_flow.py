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
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezlocloudharc.api import (
    AuthResult,
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
        subscription_status=(
            SubscriptionStatus.NONE.value
            if payment_required
            else SubscriptionStatus.FEATURE_HARC.value
        ),
        is_trial=False,
        payment_required=payment_required,
        trial_ends_at=None,
        checkout_url=None,
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


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.FEATURE_HARC.value,
        SubscriptionStatus.INTERNAL.value,
        SubscriptionStatus.PARTNER_TRIAL.value,
    ],
)
async def test_init_subscribed_shows_details_and_logout(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
    status: str,
) -> None:
    """Any access-granting state renders the flat details screen with Log out.

    Covers the central (feature_harc) and admin-managed (internal/partner)
    classes — all are valid access states, so no subscribe option appears.
    """
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "auth_token": "jwt",
            "user": {"username": "alice"},
            "server_name": "x.ezlo.com",
            "subdomain": "abc",
            "subscription_status": status,
        },
    )
    result = await handler.async_step_init()
    assert result["type"] is FlowResultType.MENU
    assert "logout" in result["menu_options"]
    assert "subscribe" not in result["menu_options"]
    assert "Remote URL" in result["description_placeholders"]["body"]


@pytest.mark.parametrize(
    "status",
    [SubscriptionStatus.NONE.value, SubscriptionStatus.PAST_DUE.value, SubscriptionStatus.CANCELED.value],
)
async def test_init_regular_unsubscribed_shows_subscribe_link(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
    status: str,
) -> None:
    """Regular self-serve states surface the subscribe link in the body + Log out."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "user": {"uuid": USER_UUID, "username": "alice"},
            "auth_token": "jwt",
            "subscription_status": status,
        },
    )
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.get_subscription_status",
            AsyncMock(return_value=_status_result(status=status, subscribe_url=SUBSCRIBE_URL)),
        ),
        patch.object(handler, "_ensure_payment_poll"),
    ):
        result = await handler.async_step_init()
    assert result["type"] is FlowResultType.MENU
    assert "logout" in result["menu_options"]
    assert "subscribe" not in result["menu_options"]
    body = result["description_placeholders"]["body"]
    assert SUBSCRIBE_URL in body
    assert "alice" in body  # logged-in username is shown alongside the link


async def test_init_partner_expired_shows_contact_manager(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Partner-expired can't self-serve: contact-manager message, no link, no fetch."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "is_logged_in": True,
            "auth_token": "jwt",
            "user": {"uuid": USER_UUID},
            "subscription_status": SubscriptionStatus.PARTNER_TRIAL_EXPIRED.value,
        },
    )
    with patch(
        "custom_components.ezlocloudharc.options_flow.get_subscription_status",
        AsyncMock(),
    ) as fetch_status:
        result = await handler.async_step_init()
    assert result["type"] is FlowResultType.MENU
    assert "logout" in result["menu_options"]
    assert "subscribe" not in result["menu_options"]
    assert "account manager" in result["description_placeholders"]["body"].lower()
    fetch_status.assert_not_awaited()


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


async def test_login_payment_required_persists_and_shows_init(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """payment_required=True keeps the user logged in and renders the init screen.

    Authentication succeeded → credentials saved, is_logged_in=True,
    payment_required=True; the flat init screen then surfaces the subscribe link.
    """
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.authenticate",
            AsyncMock(return_value=_auth_result(payment_required=True)),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.get_subscription_status",
            AsyncMock(return_value=_status_result(subscribe_url=SUBSCRIBE_URL)),
        ),
        patch.object(handler, "_ensure_payment_poll"),
    ):
        result = await handler.async_step_login({"username": "u", "password": "p"})

    assert result["type"] is FlowResultType.MENU
    assert "logout" in result["menu_options"]
    assert configured_entry.data["is_logged_in"] is True
    assert configured_entry.data["payment_required"] is True


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


async def test_signup_payment_required_persists_and_shows_init(
    handler: EzloOptionsFlowHandler,
) -> None:
    """Successful signup with payment_required renders the init subscribe screen."""
    with (
        patch(
            "custom_components.ezlocloudharc.options_flow.signup",
            AsyncMock(return_value=_auth_result(payment_required=True)),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.async_get_instance_id",
            AsyncMock(return_value="ha-uuid"),
        ),
        patch(
            "custom_components.ezlocloudharc.options_flow.get_subscription_status",
            AsyncMock(return_value=_status_result(subscribe_url=SUBSCRIBE_URL)),
        ),
        patch.object(handler, "_ensure_payment_poll"),
    ):
        result = await handler.async_step_signup(
            {"username": "u", "email": "u@x.com", "password": "p"}
        )
    assert result["type"] is FlowResultType.MENU
    assert "logout" in result["menu_options"]


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


# ── Subscribe-link helpers (used by the init tests above) ───────────


SUBSCRIBE_URL = (
    "https://api-cloud.ezlo.com/api/v4/subscription/1/subscribe"
    "?cadence=monthly&email=u%40x.com&plan=ezlo_harc_only"
)


def _status_result(
    *,
    status: str = "none",
    is_active: bool = False,
    subscribe_url: str = "",
) -> SubscriptionStatusResult:
    return SubscriptionStatusResult(
        status=status,
        is_active=is_active,
        is_trial=False,
        trial_ends_at="",
        subscribe_url=subscribe_url,
    )


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


