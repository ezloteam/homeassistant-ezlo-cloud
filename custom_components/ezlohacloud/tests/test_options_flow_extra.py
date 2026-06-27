"""Additional options-flow tests: helpers, polling, abort placeholders."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezlohacloud.api import (
    AuthResult,
    SubscriptionStatusResult,
    UserDict,
)
from custom_components.ezlohacloud.const import DOMAIN, SubscriptionStatus
from custom_components.ezlohacloud.exceptions import EzloApiUnreachableError
from custom_components.ezlohacloud.models import EzloRuntimeData
from custom_components.ezlohacloud.options_flow import (
    EzloOptionsFlowHandler,
    _trial_text_for_status,
)

USER_UUID = "u-uuid"


@pytest.fixture
def configured_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An empty config entry with EzloRuntimeData attached."""
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


# ── _trial_text_for_status ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "trial_ends_at", "expected_phrase"),
    [
        (SubscriptionStatus.TRIALING.value, "2099-01-01T00:00:00Z", "free trial"),
        (SubscriptionStatus.TRIALING.value, None, "free trial"),
        (SubscriptionStatus.INTERNAL.value, None, "Internal user"),
        (SubscriptionStatus.PARTNER_TRIAL.value, "2099-01-01T00:00:00Z", "Partner trial"),
        (SubscriptionStatus.PARTNER_TRIAL.value, None, "Partner trial active"),
        (
            SubscriptionStatus.PARTNER_TRIAL_EXPIRED.value,
            None,
            "partner trial has expired",
        ),
        (SubscriptionStatus.ACTIVE.value, None, "subscription is active"),
        (SubscriptionStatus.PAST_DUE.value, None, "last payment failed"),
        (SubscriptionStatus.CANCELED.value, None, "was canceled"),
        ("", None, ""),
    ],
)
def test_trial_text_for_status(
    status: str, trial_ends_at: str | None, expected_phrase: str
) -> None:
    """Trial text mirrors subscription state + remaining-days info."""
    text = _trial_text_for_status(status, trial_ends_at)
    if expected_phrase == "":
        assert text == ""
    else:
        assert expected_phrase.lower() in text.lower()


# ── _get_abort_placeholders ─────────────────────────────────────────


def test_get_abort_placeholders_no_cloud_url(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Without server_name/subdomain the cloud_url is 'Not yet available'."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "subscription_status": SubscriptionStatus.TRIALING.value,
            "trial_ends_at": "2099-01-01T00:00:00Z",
        },
    )
    placeholders = handler._get_abort_placeholders()
    assert placeholders["cloud_url"] == "Not yet available"
    assert "free trial" in placeholders["trial_info"].lower()


def test_get_abort_placeholders_with_cloud_url(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """When server_name + subdomain exist, cloud_url is built."""
    hass.config_entries.async_update_entry(
        configured_entry,
        data={
            **configured_entry.data,
            "server_name": "connect.harc.cloud",
            "subdomain": "hash1",
        },
    )
    placeholders = handler._get_abort_placeholders()
    assert placeholders["cloud_url"] == "https://hash1.connect.harc.cloud"


# ── _handle_successful_login error handling ─────────────────────────


def _auth_result(payment_required: bool = False) -> AuthResult:
    """AuthResult fixture for the success branch."""
    return AuthResult(
        token="jwt",
        tunnel_token="tt",
        user=UserDict(
            uuid=USER_UUID,
            username="user",
            email="e@x.com",
            ezlo_id=1,
        ),
        subscription_status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        payment_required=payment_required,
        trial_ends_at=None,
        checkout_url=None,
    )


async def test_handle_successful_login_frp_failure_still_logs_in(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """If FRP config/start fails, login state is still persisted."""
    with (
        patch(
            "custom_components.ezlohacloud.options_flow.fetch_and_update_frp_config",
            AsyncMock(side_effect=EzloApiUnreachableError("dns")),
        ),
        patch(
            "custom_components.ezlohacloud.options_flow.start_frpc", AsyncMock()
        ),
    ):
        await handler._handle_successful_login(_auth_result())

    assert configured_entry.data["is_logged_in"] is True
    assert configured_entry.data["auth_token"] == "jwt"


async def test_handle_successful_login_persists_server_details(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    handler: EzloOptionsFlowHandler,
) -> None:
    """Server name / subdomain from FRP config are written to the entry."""
    with (
        patch(
            "custom_components.ezlohacloud.options_flow.fetch_and_update_frp_config",
            AsyncMock(
                return_value={"server_name": "connect.harc.cloud", "subdomain": "h"}
            ),
        ),
        patch(
            "custom_components.ezlohacloud.options_flow.start_frpc", AsyncMock()
        ),
    ):
        await handler._handle_successful_login(_auth_result())

    assert configured_entry.data["server_name"] == "connect.harc.cloud"
    assert configured_entry.data["subdomain"] == "h"


# ── _poll_payment_and_login ─────────────────────────────────────────


async def test_poll_payment_completes_on_active(
    handler: EzloOptionsFlowHandler,
) -> None:
    """Polling completes login when the subscription becomes active."""
    responses = [
        EzloApiUnreachableError("transient"),
        SubscriptionStatusResult(
            status=SubscriptionStatus.ACTIVE.value,
            is_active=True,
            start_timestamp="",
            end_timestamp="",
        ),
    ]
    with (
        patch(
            "custom_components.ezlohacloud.options_flow.asyncio.sleep",
            AsyncMock(),
        ),
        patch(
            "custom_components.ezlohacloud.options_flow.get_subscription_status",
            AsyncMock(side_effect=responses),
        ),
        patch.object(
            handler, "_handle_successful_login", AsyncMock()
        ) as handle_login,
    ):
        await handler._poll_payment_and_login(
            USER_UUID,
            "jwt",
            "tt",
            {"uuid": USER_UUID, "username": "u", "email": "e", "ezlo_id": 1},
        )

    handle_login.assert_awaited_once()
    auth_result = handle_login.await_args.args[0]
    assert auth_result.subscription_status == SubscriptionStatus.ACTIVE.value


async def test_poll_payment_times_out_without_login(
    handler: EzloOptionsFlowHandler,
) -> None:
    """If the subscription never activates, polling exits without login."""
    not_active = SubscriptionStatusResult(
        status=SubscriptionStatus.INCOMPLETE.value,
        is_active=False,
        start_timestamp="",
        end_timestamp="",
    )
    with (
        patch(
            "custom_components.ezlohacloud.options_flow.asyncio.sleep",
            AsyncMock(),
        ),
        patch(
            "custom_components.ezlohacloud.options_flow.get_subscription_status",
            AsyncMock(return_value=not_active),
        ),
        patch.object(
            handler, "_handle_successful_login", AsyncMock()
        ) as handle_login,
        # Shrink the loop to a single attempt
        patch(
            "custom_components.ezlohacloud.options_flow.range",
            return_value=range(1),
        ),
    ):
        await handler._poll_payment_and_login(
            USER_UUID, "jwt", "tt", {"uuid": USER_UUID}
        )

    handle_login.assert_not_awaited()
