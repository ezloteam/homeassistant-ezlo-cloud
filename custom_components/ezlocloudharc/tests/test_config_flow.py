"""Tests for the Ezlo HA Cloud config flow."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezlocloudharc.api import AuthResult, UserDict
from custom_components.ezlocloudharc.const import DOMAIN
from custom_components.ezlocloudharc.exceptions import (
    EzloApiUnexpectedResponseError,
    EzloApiUnreachableError,
    EzloAuthError,
)

USER_UUID = "f960d12e-4ccb-4f0a-b37f-0abee2cd9717"
EZLO_USER_ID = 15047842


def _make_jwt(payload: dict[str, Any]) -> str:
    """Build a fake JWT for tests."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.signature"


def _auth_result(*, payment_required: bool = False) -> AuthResult:
    """Build a canonical AuthResult dataclass for the happy path."""
    return AuthResult(
        token=_make_jwt({"uuid": USER_UUID, "ezlo_user_id": EZLO_USER_ID}),
        tunnel_token="tt",
        user=UserDict(
            uuid=USER_UUID,
            username="alice",
            email="alice@example.com",
            ezlo_id=EZLO_USER_ID,
        ),
        subscription_status="trialing",
        is_trial=True,
        payment_required=payment_required,
        trial_ends_at="2026-12-31T00:00:00Z",
        checkout_url="https://checkout.stripe.com/x" if payment_required else None,
    )


# ── async_step_user (menu) ──────────────────────────────────────────


async def test_user_flow_shows_login_signup_menu(hass: HomeAssistant) -> None:
    """The initial step shows a menu with login and signup options."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert "login" in result["menu_options"]
    assert "signup" in result["menu_options"]


async def test_single_instance_only(hass: HomeAssistant) -> None:
    """A second config flow aborts because only one instance is allowed."""
    MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ── async_step_login ────────────────────────────────────────────────


async def test_login_success_creates_entry(hass: HomeAssistant) -> None:
    """A successful login creates an entry populated with auth state."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "login"

    with patch(
        "custom_components.ezlocloudharc.config_flow.authenticate",
        AsyncMock(return_value=_auth_result()),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "alice", "password": "pw"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ezlo Cloud HARC"
    data = result["data"]
    assert data["user"]["uuid"] == USER_UUID
    assert data["user"]["username"] == "alice"
    assert data["is_logged_in"] is True
    assert data["payment_required"] is False
    assert data["subscription_status"] == "trialing"


@pytest.mark.parametrize(
    ("error", "expected_base"),
    [
        (EzloAuthError("invalid credentials"), "invalid_credentials"),
        (EzloApiUnreachableError("dns"), "network_error"),
    ],
)
async def test_login_failure_shows_classified_error(
    hass: HomeAssistant, error: Exception, expected_base: str
) -> None:
    """Typed exceptions map to the right `errors['base']` translation key."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    with patch(
        "custom_components.ezlocloudharc.config_flow.authenticate",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "alice", "password": "wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "login"
    assert result["errors"] == {"base": expected_base}


# ── async_step_signup ───────────────────────────────────────────────


async def test_signup_success_creates_entry_with_payment_required(
    hass: HomeAssistant,
) -> None:
    """Signup that returns payment_required still creates the entry.

    Authentication succeeded, so is_logged_in is True (credentials saved);
    payment_required stays True separately so setup idles the tunnel and the
    options flow surfaces the subscribe link.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "signup"}
    )
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.ezlocloudharc.config_flow.signup",
        AsyncMock(return_value=_auth_result(payment_required=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "alice", "email": "alice@example.com", "password": "pw"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["user"]["uuid"] == USER_UUID
    assert data["payment_required"] is True
    assert data["is_logged_in"] is True


async def test_signup_failure_shows_backend_error(hass: HomeAssistant) -> None:
    """A signup-failure exception surfaces as `errors['base'] = 'signup_failed'`."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "signup"}
    )
    with patch(
        "custom_components.ezlocloudharc.config_flow.signup",
        AsyncMock(side_effect=EzloAuthError("Username taken")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "alice", "email": "alice@example.com", "password": "pw"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "signup_failed"}


# ── reauth ──────────────────────────────────────────────────────────


async def test_reauth_flow_updates_entry(hass: HomeAssistant) -> None:
    """A successful reauth updates the existing entry's auth fields."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"auth_token": "stale", "user": {"uuid": USER_UUID}},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.ezlocloudharc.config_flow.authenticate",
        AsyncMock(return_value=_auth_result()),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "alice", "password": "fresh-pw"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["auth_token"].startswith("eyJ")  # JWT-shaped
    assert entry.data["is_logged_in"] is True


# ── reconfigure ─────────────────────────────────────────────────────


async def test_login_generic_ezlo_error_shows_unknown(hass: HomeAssistant) -> None:
    """A generic EzloError (non-auth, non-network) maps to errors[base]=unknown."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    with patch(
        "custom_components.ezlocloudharc.config_flow.authenticate",
        AsyncMock(side_effect=EzloApiUnexpectedResponseError("malformed")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "u", "password": "p"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_signup_network_error_shows_network_error(
    hass: HomeAssistant,
) -> None:
    """A network-shaped signup error maps to errors[base]=network_error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "signup"}
    )
    with patch(
        "custom_components.ezlocloudharc.config_flow.signup",
        AsyncMock(side_effect=EzloApiUnreachableError("dns")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "a", "email": "a@x.com", "password": "p"},
        )
    assert result["errors"] == {"base": "network_error"}


async def test_signup_generic_ezlo_error_shows_signup_failed(
    hass: HomeAssistant,
) -> None:
    """A generic EzloError during signup maps to signup_failed."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "signup"}
    )
    with patch(
        "custom_components.ezlocloudharc.config_flow.signup",
        AsyncMock(side_effect=EzloApiUnexpectedResponseError("oops")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "a", "email": "a@x.com", "password": "p"},
        )
    assert result["errors"] == {"base": "signup_failed"}


async def test_reauth_network_error_keeps_form(hass: HomeAssistant) -> None:
    """A network error during reauth stays on the form with the error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"auth_token": "stale", "user": {"uuid": USER_UUID}},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.ezlocloudharc.config_flow.authenticate",
        AsyncMock(side_effect=EzloApiUnreachableError("dns")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "u", "password": "p"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "network_error"}


async def test_reconfigure_network_error_keeps_form(
    hass: HomeAssistant,
) -> None:
    """A network error during reconfigure stays on the form."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"auth_token": "old", "user": {"uuid": USER_UUID}},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.ezlocloudharc.config_flow.authenticate",
        AsyncMock(side_effect=EzloApiUnreachableError("dns")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "u", "password": "p"}
        )
    assert result["errors"] == {"base": "network_error"}


async def test_reconfigure_flow_updates_entry(hass: HomeAssistant) -> None:
    """A successful reconfigure swaps credentials in place."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={"auth_token": "old", "user": {"uuid": USER_UUID}},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(
        "custom_components.ezlocloudharc.config_flow.authenticate",
        AsyncMock(return_value=_auth_result()),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "alice", "password": "pw"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["is_logged_in"] is True
