"""Tests for connecting Home Assistant to the Ezlo app (HARC-20)."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, User
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.ezlocloudharc import async_remove_entry
from custom_components.ezlocloudharc.api import AuthResult, UserDict
from custom_components.ezlocloudharc.const import (
    CONF_PROPERTY_URI,
    DEFAULT_PROPERTY_URI,
    DOMAIN,
    SubscriptionStatus,
)
from custom_components.ezlocloudharc.exceptions import (
    EzloAppAuthError,
    EzloAppError,
)
from custom_components.ezlocloudharc.ezlo_app import (
    EZLO_APP_TOKEN_LIFETIME,
    EZLO_APP_USER_NAME,
    PropertyChoices,
    PropertyOption,
    async_connect,
    async_disconnect,
    async_list_properties,
)
from custom_components.ezlocloudharc.models import EzloAppData, EzloRuntimeData

LOGIN_URL = f"{DEFAULT_PROPERTY_URI}/api/login/"
API_URL = f"{DEFAULT_PROPERTY_URI}/api/"
HA_URL = "https://abc123.connect.harc.cloud"
USER_UUID = "f960d12e-4ccb-4f0a-b37f-0abee2cd9717"

LOGIN_OK = {"status": 1, "PK_account": "4242", "PK_user": 7}
REGISTER_OK = {
    "status": 1,
    "data": {"serial": "ha-0123456789ab", "hub_type": "home_assistant", "is_online": False},
}


class _MockSession:
    """Routes the module's requests to aioclient_mock without a real ClientSession.

    aioclient_mock's own sessions build a real connector, whose aiodns resolver
    starts a pycares thread that the test harness then reports as lingering.
    """

    def __init__(self, mocker: AiohttpClientMocker) -> None:
        self._mocker = mocker

    def post(self, url: str, **kwargs: Any) -> _MockRequest:
        kwargs.pop("headers", None)
        return _MockRequest(self._mocker.match_request("post", url, **kwargs))

    def detach(self) -> None:
        """Nothing to release."""


class _MockRequest:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aenter__(self) -> Any:
        return await self._response

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _platform_session(aioclient_mock: AiohttpClientMocker) -> Any:
    with patch(
        "custom_components.ezlocloudharc.ezlo_app.async_create_clientsession",
        lambda *args, **kwargs: _MockSession(aioclient_mock),
    ):
        yield


def _mock_platform(
    aioclient_mock: AiohttpClientMocker,
    *,
    login: dict[str, Any] | None = None,
    register: dict[str, Any] | None = None,
    login_status: int = 200,
    register_status: int = 200,
) -> None:
    aioclient_mock.post(LOGIN_URL, status=login_status, json=login or LOGIN_OK)
    aioclient_mock.post(API_URL, status=register_status, json=register or REGISTER_OK)


async def _connect(hass: HomeAssistant, previous: EzloAppData | None = None) -> EzloAppData:
    return await async_connect(
        hass, username="alice", password="pw", ha_url=HA_URL, previous=previous
    )


async def _ezlo_users(hass: HomeAssistant) -> list[User]:
    return [u for u in await hass.auth.async_get_users() if u.name == EZLO_APP_USER_NAME]


def _llat_ids(user: User) -> set[str]:
    return {
        t.id
        for t in user.refresh_tokens.values()
        if t.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    }


FLOW = "custom_components.ezlocloudharc.ezlo_app_flow"


@contextmanager
def _platform(
    connect: AsyncMock, choices: PropertyChoices | Exception | None = None
) -> Iterator[None]:
    """Patch the platform calls the flows make; ``None`` choices = no picker."""
    listing = (
        AsyncMock(side_effect=choices)
        if isinstance(choices, Exception)
        else AsyncMock(return_value=choices)
    )
    with (
        patch(f"{FLOW}.async_list_properties", listing),
        patch(f"{FLOW}.async_connect", connect),
    ):
        yield


# ── ezlo_app.async_connect ──────────────────────────────────────────


async def test_connect_creates_admin_user_and_registers_its_token(
    hass: HomeAssistant, hass_owner_user: User, aioclient_mock: AiohttpClientMocker
) -> None:
    """The platform gets a working long-lived token for a dedicated admin user."""
    _mock_platform(aioclient_mock)

    data = await _connect(hass)

    user = await hass.auth.async_get_user(data["ha_user_id"])
    assert user is not None
    assert user.name == EZLO_APP_USER_NAME
    assert user.is_admin
    assert not user.is_owner
    assert not user.local_only
    assert [g.id for g in user.groups] == [GROUP_ID_ADMIN]

    refresh = hass.auth.async_get_refresh_token(data["refresh_token_id"])
    assert refresh is not None
    assert refresh.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    assert refresh.access_token_expiration == EZLO_APP_TOKEN_LIFETIME

    assert data == {
        "ha_user_id": user.id,
        "refresh_token_id": refresh.id,
        "pk_account": 4242,
        "serial": "ha-0123456789ab",
        "ha_url": HA_URL,
    }

    (_, _, login_body, _), (_, _, register_body, _) = aioclient_mock.mock_calls
    assert login_body == {"username": "alice", "password": "pw", "pk_oem": "auto"}
    assert register_body["mode"] == "registerHomeAssistantHub"
    assert register_body["pk_account"] == 4242
    assert register_body["ha_url"] == HA_URL
    assert register_body["controller_name"] == hass.config.location_name
    # The token handed over is a valid access token for the Ezlo user.
    validated = hass.auth.async_validate_access_token(register_body["ha_token"])
    assert validated is not None
    assert validated.id == refresh.id


@pytest.mark.parametrize(
    ("login", "login_status"),
    [
        ({"status": 0, "message": "Invalid Login"}, 200),
        ({"message": "The username field is required."}, 422),
    ],
)
async def test_connect_rejected_login_creates_nothing(
    hass: HomeAssistant,
    hass_owner_user: User,
    aioclient_mock: AiohttpClientMocker,
    login: dict[str, Any],
    login_status: int,
) -> None:
    """A wrong password fails before anything is created in Home Assistant."""
    _mock_platform(aioclient_mock, login=login, login_status=login_status)

    with pytest.raises(EzloAppAuthError):
        await _connect(hass)

    assert await _ezlo_users(hass) == []
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize(
    ("register", "register_status", "match"),
    [
        ({"status": 0, "message": "Not authorized to access this property"}, 200, "Not authorized"),
        ({"status": 0}, 403, "HTTP 403"),
        ({"status": 1, "data": {}}, 200, "hub id"),
        ({"status": 0, "message": "Error registering HA hub: boom"}, 500, "HTTP 500"),
    ],
)
async def test_connect_failed_registration_removes_new_user(
    hass: HomeAssistant,
    hass_owner_user: User,
    aioclient_mock: AiohttpClientMocker,
    register: dict[str, Any],
    register_status: int,
    match: str,
) -> None:
    """Nothing is left behind when the platform refuses the hub."""
    _mock_platform(aioclient_mock, register=register, register_status=register_status)

    with pytest.raises(EzloAppError, match=match):
        await _connect(hass)

    assert await _ezlo_users(hass) == []


async def test_reconnect_reuses_user_and_rotates_token(
    hass: HomeAssistant, hass_owner_user: User, aioclient_mock: AiohttpClientMocker
) -> None:
    """Reconnecting keeps one Ezlo user and revokes the old token only after success."""
    _mock_platform(aioclient_mock)
    first = await _connect(hass)

    second = await _connect(hass, previous=first)

    users = await _ezlo_users(hass)
    assert [u.id for u in users] == [first["ha_user_id"]]
    assert second["ha_user_id"] == first["ha_user_id"]
    assert _llat_ids(users[0]) == {second["refresh_token_id"]}
    assert hass.auth.async_get_refresh_token(first["refresh_token_id"]) is None


async def test_failed_reconnect_keeps_existing_connection(
    hass: HomeAssistant, hass_owner_user: User, aioclient_mock: AiohttpClientMocker
) -> None:
    """A failed reconnect leaves the user and the working token in place."""
    _mock_platform(aioclient_mock)
    first = await _connect(hass)
    aioclient_mock.clear_requests()
    _mock_platform(aioclient_mock, register={"status": 0, "message": "nope"})

    with pytest.raises(EzloAppError):
        await _connect(hass, previous=first)

    users = await _ezlo_users(hass)
    assert [u.id for u in users] == [first["ha_user_id"]]
    assert _llat_ids(users[0]) == {first["refresh_token_id"]}


async def test_reconnect_after_user_deleted_creates_a_new_one(
    hass: HomeAssistant, hass_owner_user: User, aioclient_mock: AiohttpClientMocker
) -> None:
    """Deleting the Ezlo user by hand does not break reconnecting."""
    _mock_platform(aioclient_mock)
    first = await _connect(hass)
    await async_disconnect(hass, first)

    second = await _connect(hass, previous=first)

    assert second["ha_user_id"] != first["ha_user_id"]
    assert len(await _ezlo_users(hass)) == 1


async def test_reconnect_refuses_deactivated_user(
    hass: HomeAssistant, hass_owner_user: User, aioclient_mock: AiohttpClientMocker
) -> None:
    """A user the owner deactivated is not silently re-enabled or replaced."""
    _mock_platform(aioclient_mock)
    first = await _connect(hass)
    user = await hass.auth.async_get_user(first["ha_user_id"])
    await hass.auth.async_deactivate_user(user)

    with pytest.raises(EzloAppError, match="deactivated"):
        await _connect(hass, previous=first)

    assert _llat_ids(user) == {first["refresh_token_id"]}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"status": 429}, "Too many attempts"),
        ({"exc": aiohttp.ClientConnectionError("down")}, "Could not reach"),
        ({"exc": TimeoutError()}, "did not answer"),
        ({"text": "<html>"}, "unreadable"),
        ({"json": ["not", "a", "dict"]}, "unexpected answer"),
        ({"json": {"status": 1}}, "did not return an account"),
    ],
)
async def test_connect_maps_platform_failures(
    hass: HomeAssistant,
    hass_owner_user: User,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    match: str,
) -> None:
    """Transport and payload problems surface as EzloAppError with a reason."""
    aioclient_mock.post(LOGIN_URL, **kwargs)

    with pytest.raises(EzloAppError, match=match):
        await _connect(hass)


async def test_disconnect_removes_user(
    hass: HomeAssistant, hass_owner_user: User, aioclient_mock: AiohttpClientMocker
) -> None:
    """Disconnecting deletes the Ezlo user and with it every token."""
    _mock_platform(aioclient_mock)
    data = await _connect(hass)

    await async_disconnect(hass, data)

    assert await hass.auth.async_get_user(data["ha_user_id"]) is None
    assert hass.auth.async_get_refresh_token(data["refresh_token_id"]) is None
    # Nothing to do the second time, or without a connection.
    await async_disconnect(hass, data)
    await async_disconnect(hass, None)


async def test_remove_entry_disconnects(hass: HomeAssistant) -> None:
    """Removing the integration revokes the Ezlo app's access."""
    entry = MockConfigEntry(domain=DOMAIN, data={"ezlo_app": {"ha_user_id": "u"}})
    with patch(
        "custom_components.ezlocloudharc.async_disconnect_ezlo_app", AsyncMock()
    ) as disconnect:
        await async_remove_entry(hass, entry)
    disconnect.assert_awaited_once_with(hass, {"ha_user_id": "u"})


# ── Config flow consent step ────────────────────────────────────────

EZLO_APP = EzloAppData(
    ha_user_id="u1",
    refresh_token_id="r1",
    pk_account=4242,
    serial="ha-0123456789ab",
    ha_url=HA_URL,
)


def _make_jwt(payload: dict[str, Any]) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.signature"


def _auth_result(*, payment_required: bool = False) -> AuthResult:
    return AuthResult(
        token=_make_jwt({"uuid": USER_UUID}),
        tunnel_token="tt",
        user=UserDict(uuid=USER_UUID, username="alice", email="a@example.com", ezlo_id=42),
        subscription_status=(
            SubscriptionStatus.NONE.value
            if payment_required
            else SubscriptionStatus.TRIALING.value
        ),
        is_trial=not payment_required,
        payment_required=payment_required,
        trial_ends_at=None,
        checkout_url=None,
    )


async def _flow_at_consent(hass: HomeAssistant, step: str = "login") -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    user_input = {"username": "alice", "password": "pw"}
    if step == "signup":
        user_input["email"] = "a@example.com"
    with patch(
        f"custom_components.ezlocloudharc.config_flow.{'authenticate' if step == 'login' else 'signup'}",
        AsyncMock(return_value=_auth_result()),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "ezlo_app"
    assert result["menu_options"] == ["ezlo_app_allow", "ezlo_app_skip"]
    return result


@pytest.mark.parametrize("step", ["login", "signup"])
async def test_consent_allow_registers_and_creates_entry(
    hass: HomeAssistant, step: str
) -> None:
    """Allowing registers the HARC URL with the password typed in the flow."""
    result = await _flow_at_consent(hass, step)
    frp = AsyncMock(return_value={"server_name": "connect.harc.cloud", "subdomain": "abc123"})
    connect = AsyncMock(return_value=EZLO_APP)
    with (
        patch("custom_components.ezlocloudharc.config_flow.fetch_and_update_frp_config", frp),
        _platform(connect),
        patch("custom_components.ezlocloudharc.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_allow"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["ezlo_app"] == EZLO_APP
    assert result["data"]["server_name"] == "connect.harc.cloud"
    assert result["data"]["subdomain"] == "abc123"
    assert "password" not in json.dumps(result["data"])
    connect.assert_awaited_once()
    assert connect.await_args.kwargs == {
        "username": "alice",
        "password": "pw",
        "ha_url": HA_URL,
        "property_uri": DEFAULT_PROPERTY_URI,
        "previous": None,
        "property_id": None,
        "new_property": None,
    }


@pytest.mark.parametrize(
    ("frp_info", "error", "detail"),
    [
        ({"server_name": "connect.harc.cloud", "subdomain": "abc123"}, EzloAppAuthError("bad"), "did not accept"),
        ({"server_name": "connect.harc.cloud", "subdomain": "abc123"}, EzloAppError("platform down"), "platform down"),
        ({"server_name": "", "subdomain": ""}, None, "not ready yet"),
        (OSError("disk full"), None, "disk full"),
    ],
)
async def test_consent_failure_returns_to_menu_then_skip(
    hass: HomeAssistant,
    frp_info: dict[str, str] | Exception,
    error: Exception | None,
    detail: str,
) -> None:
    """A failed connection explains why; the user can retry or skip."""
    result = await _flow_at_consent(hass)
    frp = AsyncMock(side_effect=frp_info) if isinstance(frp_info, Exception) else AsyncMock(return_value=frp_info)
    connect = AsyncMock(side_effect=error, return_value=EZLO_APP)
    with (
        patch("custom_components.ezlocloudharc.config_flow.fetch_and_update_frp_config", frp),
        _platform(connect),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_allow"}
        )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "ezlo_app"
    assert detail in result["description_placeholders"]["error_detail"]

    with patch("custom_components.ezlocloudharc.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_skip"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "ezlo_app" not in result["data"]


async def test_no_consent_step_without_access(hass: HomeAssistant) -> None:
    """Without a subscription there is no remote URL, so no question yet."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "login"}
    )
    with (
        patch(
            "custom_components.ezlocloudharc.config_flow.authenticate",
            AsyncMock(return_value=_auth_result(payment_required=True)),
        ),
        patch("custom_components.ezlocloudharc.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "alice", "password": "pw"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "ezlo_app" not in result["data"]


# ── Options flow ────────────────────────────────────────────────────


@pytest.fixture
def subscribed_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A logged-in, subscribed entry with its tunnel URL known."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            "auth_token": "tok",
            "is_logged_in": True,
            "user": {"uuid": USER_UUID, "username": "alice"},
            "subscription_status": SubscriptionStatus.FEATURE_HARC.value,
            "server_name": "connect.harc.cloud",
            "subdomain": "abc123",
        },
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EzloRuntimeData()
    return entry


async def test_options_menu_offers_connect(
    hass: HomeAssistant, subscribed_entry: MockConfigEntry
) -> None:
    """A subscribed entry without the Ezlo app offers to connect it."""
    result = await hass.config_entries.options.async_init(subscribed_entry.entry_id)
    assert list(result["menu_options"]) == ["ezlo_app", "logout"]
    assert result["menu_options"]["ezlo_app"] == "Connect Ezlo app"
    assert "**Ezlo app:** Not connected" in result["description_placeholders"]["body"]


async def test_options_menu_offers_reconnect_and_disconnect(
    hass: HomeAssistant, subscribed_entry: MockConfigEntry
) -> None:
    """A connected entry offers to reconnect or disconnect."""
    hass.config_entries.async_update_entry(
        subscribed_entry, data={**subscribed_entry.data, "ezlo_app": EZLO_APP}
    )
    result = await hass.config_entries.options.async_init(subscribed_entry.entry_id)
    assert list(result["menu_options"]) == ["ezlo_app", "ezlo_app_disconnect", "logout"]
    assert "**Ezlo app:** Connected" in result["description_placeholders"]["body"]


async def test_options_connect_success(
    hass: HomeAssistant, subscribed_entry: MockConfigEntry
) -> None:
    """Connecting from the options asks for the password and stores the result."""
    hass.config_entries.async_update_entry(
        subscribed_entry,
        data={**subscribed_entry.data, CONF_PROPERTY_URI: "https://staging.example"},
    )
    result = await hass.config_entries.options.async_init(subscribed_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ezlo_app"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["username"] == "alice"

    connect = AsyncMock(return_value=EZLO_APP)
    with _platform(connect):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"password": "pw"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "ezlo_app_connected"
    assert subscribed_entry.data["ezlo_app"] == EZLO_APP
    assert connect.await_args.kwargs == {
        "username": "alice",
        "password": "pw",
        "ha_url": HA_URL,
        "property_uri": "https://staging.example",
        "previous": None,
        "property_id": None,
        "new_property": None,
    }


@pytest.mark.parametrize(
    ("error", "base", "detail"),
    [
        (EzloAppAuthError("bad"), "invalid_credentials", ""),
        (EzloAppError("platform down"), "ezlo_app_failed", "platform down"),
    ],
)
async def test_options_connect_failure_shows_error(
    hass: HomeAssistant,
    subscribed_entry: MockConfigEntry,
    error: Exception,
    base: str,
    detail: str,
) -> None:
    """Failures keep the form open with the reason."""
    result = await hass.config_entries.options.async_init(subscribed_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ezlo_app"}
    )
    with _platform(AsyncMock(side_effect=error)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"password": "pw"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": base}
    assert detail in result["description_placeholders"]["error_detail"]
    assert "ezlo_app" not in subscribed_entry.data


async def test_options_connect_without_remote_url(
    hass: HomeAssistant, subscribed_entry: MockConfigEntry
) -> None:
    """Without a tunnel URL there is nothing to register."""
    data = {k: v for k, v in subscribed_entry.data.items() if k != "subdomain"}
    hass.config_entries.async_update_entry(subscribed_entry, data=data)
    result = await hass.config_entries.options.async_init(subscribed_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ezlo_app"}
    )
    connect = AsyncMock()
    with _platform(connect):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"password": "pw"}
        )
    assert result["errors"] == {"base": "ezlo_app_failed"}
    assert "not ready yet" in result["description_placeholders"]["error_detail"]
    connect.assert_not_awaited()


async def test_options_disconnect(
    hass: HomeAssistant, subscribed_entry: MockConfigEntry
) -> None:
    """Disconnecting revokes access and forgets the connection."""
    hass.config_entries.async_update_entry(
        subscribed_entry, data={**subscribed_entry.data, "ezlo_app": EZLO_APP}
    )
    result = await hass.config_entries.options.async_init(subscribed_entry.entry_id)
    disconnect = AsyncMock()
    with patch(
        "custom_components.ezlocloudharc.options_flow.async_disconnect_ezlo_app", disconnect
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_disconnect"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "ezlo_app_disconnected"
    disconnect.assert_awaited_once_with(hass, EZLO_APP)
    assert "ezlo_app" not in subscribed_entry.data


@pytest.mark.parametrize(
    ("submitted", "stored"),
    [("https://staging.example", "https://staging.example"), (DEFAULT_PROPERTY_URI, None), ("", None)],
)
async def test_advanced_property_uri_override(
    hass: HomeAssistant,
    subscribed_entry: MockConfigEntry,
    submitted: str,
    stored: str | None,
) -> None:
    """The Ezlo app URL can be overridden; the default is not stored as one."""
    hass.config_entries.async_update_entry(
        subscribed_entry,
        data={**subscribed_entry.data, CONF_PROPERTY_URI: "https://old.example"},
    )
    result = await hass.config_entries.options.async_init(
        subscribed_entry.entry_id, context={"show_advanced_options": True}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PROPERTY_URI: submitted}
    )
    assert result["reason"] == "config_saved"
    assert subscribed_entry.data.get(CONF_PROPERTY_URI) == stored



# ── Login/signup from the options and reconfigure flows ─────────────


@contextmanager
def _options_login(result: AuthResult, frp_info: dict[str, str] | Exception) -> Iterator[None]:
    """Patch what an options-flow login touches outside the Ezlo app."""
    frp = (
        AsyncMock(side_effect=frp_info)
        if isinstance(frp_info, Exception)
        else AsyncMock(return_value=frp_info)
    )
    base = "custom_components.ezlocloudharc.options_flow"
    with (
        patch(f"{base}.authenticate", AsyncMock(return_value=result)),
        patch(f"{base}.signup", AsyncMock(return_value=result)),
        patch(f"{base}.fetch_and_update_frp_config", frp),
        patch(f"{base}.start_frpc", AsyncMock()),
        patch(f"{base}.async_get_instance_id", AsyncMock(return_value="ha-uuid")),
        patch("custom_components.ezlocloudharc.async_setup_entry", AsyncMock(return_value=True)),
    ):
        yield


@pytest.fixture
def logged_out_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A logged-out entry, as left behind by Log out."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={"is_logged_in": False})
    entry.add_to_hass(hass)
    entry.runtime_data = EzloRuntimeData()
    return entry


@pytest.mark.parametrize(
    ("step", "user_input"),
    [
        ("login", {"username": "alice", "password": "pw"}),
        ("signup", {"username": "alice", "email": "a@example.com", "password": "pw"}),
    ],
)
async def test_options_login_offers_ezlo_app(
    hass: HomeAssistant,
    logged_out_entry: MockConfigEntry,
    step: str,
    user_input: dict[str, str],
) -> None:
    """Logging in from Configure asks about the Ezlo app with the same password."""
    frp = {"server_name": "connect.harc.cloud", "subdomain": "abc123"}
    connect = AsyncMock(return_value=EZLO_APP)
    with (
        _options_login(_auth_result(), frp),
        _platform(connect),
    ):
        result = await hass.config_entries.options.async_init(logged_out_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input
        )
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "ezlo_app_offer"
        assert result["description_placeholders"]["cloud_url"] == HA_URL

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_offer_allow"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "ezlo_app_connected"
    assert logged_out_entry.data["ezlo_app"] == EZLO_APP
    assert connect.await_args.kwargs == {
        "username": "alice",
        "password": "pw",
        "ha_url": HA_URL,
        "property_uri": DEFAULT_PROPERTY_URI,
        "previous": None,
        "property_id": None,
        "new_property": None,
    }


async def test_options_offer_failure_then_skip(
    hass: HomeAssistant, logged_out_entry: MockConfigEntry
) -> None:
    """A failed connection stays on the question; skipping closes as a normal signup."""
    frp = {"server_name": "connect.harc.cloud", "subdomain": "abc123"}
    with (
        _options_login(_auth_result(), frp),
        _platform(AsyncMock(side_effect=EzloAppAuthError("bad"))),
    ):
        result = await hass.config_entries.options.async_init(logged_out_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "signup"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"username": "alice", "email": "a@example.com", "password": "pw"},
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_offer_allow"}
        )
        assert result["type"] is FlowResultType.MENU
        assert "did not accept" in result["description_placeholders"]["error_detail"]

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_offer_skip"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "signup_trial_started"
    assert "ezlo_app" not in logged_out_entry.data


async def test_options_login_without_remote_url_skips_question(
    hass: HomeAssistant, logged_out_entry: MockConfigEntry
) -> None:
    """When server-config fails there is no URL to register, so no question."""
    with _options_login(_auth_result(), EzloAppError("down")):
        result = await hass.config_entries.options.async_init(logged_out_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "login"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"username": "alice", "password": "pw"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "login_successful"


async def test_options_logout_disconnects_ezlo_app(
    hass: HomeAssistant, subscribed_entry: MockConfigEntry
) -> None:
    """Logging out revokes the Ezlo app; the next account must not inherit it."""
    hass.config_entries.async_update_entry(
        subscribed_entry, data={**subscribed_entry.data, "ezlo_app": EZLO_APP}
    )
    disconnect = AsyncMock()
    base = "custom_components.ezlocloudharc.options_flow"
    with (
        patch(f"{base}.async_disconnect_ezlo_app", disconnect),
        patch(f"{base}.release_backend_binding", AsyncMock()),
        patch(f"{base}.stop_frpc", AsyncMock()),
    ):
        result = await hass.config_entries.options.async_init(subscribed_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "logout"}
        )

    assert result["reason"] == "logged_out"
    disconnect.assert_awaited_once_with(hass, EZLO_APP)
    assert "ezlo_app" not in subscribed_entry.data
    assert subscribed_entry.data["is_logged_in"] is False


async def test_reconfigure_login_offers_ezlo_app(hass: HomeAssistant) -> None:
    """Logging in from Reconfigure asks too, reusing the entry's endpoints and user."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            "is_logged_in": False,
            CONF_PROPERTY_URI: "https://staging.example",
            "ezlo_app": EZLO_APP,
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_login"}
    )
    base = "custom_components.ezlocloudharc.config_flow"
    connected = {**EZLO_APP, "refresh_token_id": "r2"}
    connect = AsyncMock(return_value=connected)
    with (
        patch(f"{base}.authenticate", AsyncMock(return_value=_auth_result())),
        patch(
            f"{base}.fetch_and_update_frp_config",
            AsyncMock(return_value={"server_name": "connect.harc.cloud", "subdomain": "abc123"}),
        ),
        _platform(connect),
        patch("custom_components.ezlocloudharc.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "alice", "password": "pw"}
        )
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "ezlo_app"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_allow"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["is_logged_in"] is True
    assert entry.data["ezlo_app"] == connected
    assert connect.await_args.kwargs["property_uri"] == "https://staging.example"
    assert connect.await_args.kwargs["previous"] == EZLO_APP


async def test_reconfigure_logout_disconnects_ezlo_app(hass: HomeAssistant) -> None:
    """Log out from Reconfigure revokes the Ezlo app as well."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            "auth_token": "tok",
            "is_logged_in": True,
            "user": {"uuid": USER_UUID, "username": "alice"},
            "subscription_status": SubscriptionStatus.FEATURE_HARC.value,
            "ezlo_app": EZLO_APP,
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    base = "custom_components.ezlocloudharc.config_flow"
    disconnect = AsyncMock()
    with (
        patch(f"{base}.async_disconnect_ezlo_app", disconnect),
        patch(f"{base}.release_backend_binding", AsyncMock()),
        patch("custom_components.ezlocloudharc.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "reconfigure_logout"}
        )

    assert result["reason"] == "logged_out"
    disconnect.assert_awaited_once_with(hass, EZLO_APP)
    assert "ezlo_app" not in entry.data
    assert entry.data["is_logged_in"] is False


# ── Property picker (D2) and new-property form (D3) ─────────────────

LIST_V4 = {
    "status": 1,
    "data": {
        "source": "v4",
        "properties": [
            {"id": "p-1", "name": "Beach House", "address": "1 Sea Rd, Goa", "has_hub": False},
            {"id": "p-2", "name": "City Flat", "address": "", "has_hub": True},
        ],
        "can_create": True,
        "create_fields": {
            "required": ["property_name", "street_address", "city", "state", "country", "zipcode"],
            "optional": ["additional_street_address", "time_zone"],
        },
    },
}

CHOICES = PropertyChoices(
    source="v4",
    properties=(
        PropertyOption(id="p-1", name="Beach House", address="1 Sea Rd, Goa"),
        PropertyOption(id="p-2", name="City Flat", has_hub=True),
    ),
    can_create=True,
    required_fields=frozenset(
        {"property_name", "street_address", "city", "state", "country", "zipcode"}
    ),
    optional_fields=frozenset({"additional_street_address", "time_zone"}),
)


async def test_list_properties_reads_the_platform_list(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The list call signs in, asks for this URL's properties and parses them."""
    aioclient_mock.post(LOGIN_URL, json=LOGIN_OK)
    aioclient_mock.post(API_URL, json=LIST_V4)

    choices = await async_list_properties(
        hass, username="alice", password="pw", ha_url=HA_URL
    )

    assert choices == CHOICES
    (_, _, list_body, _) = aioclient_mock.mock_calls[1]
    assert list_body == {"mode": "listHomeAssistantProperties", "ha_url": HA_URL}


@pytest.mark.parametrize("text", ["", "false"])
async def test_list_properties_on_platform_without_picker(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, text: str
) -> None:
    """An unknown mode answers with an empty body: no picker, not an error."""
    aioclient_mock.post(LOGIN_URL, json=LOGIN_OK)
    aioclient_mock.post(API_URL, text=text)

    assert await async_list_properties(hass, username="alice", password="pw", ha_url=HA_URL) is None


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"status": 0, "message": "The property service did not respond"}, "did not respond"),
        ({"status": 1, "data": {"properties": "nope"}}, "unexpected property list"),
    ],
)
async def test_list_properties_failure_is_not_an_empty_list(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, body: dict[str, Any], match: str
) -> None:
    """An unreadable list raises; treating it as empty would offer a duplicate."""
    aioclient_mock.post(LOGIN_URL, json=LOGIN_OK)
    aioclient_mock.post(API_URL, json=body)

    with pytest.raises(EzloAppError, match=match):
        await async_list_properties(hass, username="alice", password="pw", ha_url=HA_URL)


async def test_connect_to_chosen_property(
    hass: HomeAssistant, hass_owner_user: User, aioclient_mock: AiohttpClientMocker
) -> None:
    """The chosen property goes to the platform and comes back on the entry data."""
    _mock_platform(
        aioclient_mock,
        register={
            "status": 1,
            "data": {"serial": "ha-0123456789ab", "property_id": "p-1", "property_name": "Beach House"},
        },
    )

    data = await async_connect(
        hass, username="alice", password="pw", ha_url=HA_URL, property_id="p-1"
    )

    assert data["property_id"] == "p-1"
    assert data["property_name"] == "Beach House"
    register_body = aioclient_mock.mock_calls[1][2]
    assert register_body["property_id"] == "p-1"
    assert "new_property" not in register_body


async def test_connect_with_incomplete_new_property(
    hass: HomeAssistant, hass_owner_user: User, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 422 naming missing fields becomes a readable error; nothing is left behind."""
    _mock_platform(
        aioclient_mock,
        register={"status": 0, "failure": "address_incomplete", "missing": ["street_address", "zipcode"]},
        register_status=422,
    )

    with pytest.raises(EzloAppError, match="needs: street address, zipcode"):
        await async_connect(
            hass,
            username="alice",
            password="pw",
            ha_url=HA_URL,
            new_property={"property_name": "Home"},
        )

    assert aioclient_mock.mock_calls[1][2]["new_property"] == {"property_name": "Home"}
    assert await _ezlo_users(hass) == []


async def _config_flow_at_picker(hass: HomeAssistant, choices: Any) -> dict[str, Any]:
    """Log in, allow the Ezlo app, and stop at whatever comes next."""
    result = await _flow_at_consent(hass)
    frp = AsyncMock(return_value={"server_name": "connect.harc.cloud", "subdomain": "abc123"})
    with (
        patch("custom_components.ezlocloudharc.config_flow.fetch_and_update_frp_config", frp),
        patch(f"{FLOW}.async_list_properties", AsyncMock(return_value=choices)),
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_allow"}
        )


async def test_picker_lists_properties_and_registers_the_choice(hass: HomeAssistant) -> None:
    """The picker preselects the property that has this hub and registers the pick."""
    result = await _config_flow_at_picker(hass, CHOICES)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ezlo_app_property"
    selector = result["data_schema"].schema["property"]
    options = selector.config["options"]
    assert [o["value"] for o in options] == ["p-1", "p-2", "__new__", "__skip__"]
    assert options[0]["label"] == "Beach House — 1 Sea Rd, Goa"
    assert options[1]["label"] == "City Flat (already added)"
    marker = next(k for k in result["data_schema"].schema if k == "property")
    assert marker.default() == "p-2"

    connected = {**EZLO_APP, "property_id": "p-1", "property_name": "Beach House"}
    connect = AsyncMock(return_value=connected)
    with (
        patch(f"{FLOW}.async_connect", connect),
        patch("custom_components.ezlocloudharc.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"property": "p-1"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["ezlo_app"] == connected
    assert connect.await_args.kwargs["property_id"] == "p-1"
    assert connect.await_args.kwargs["new_property"] is None


async def test_picker_skip_sets_up_remote_access_only(hass: HomeAssistant) -> None:
    """Setup can still finish without the Ezlo app from the picker."""
    result = await _config_flow_at_picker(hass, CHOICES)
    with patch("custom_components.ezlocloudharc.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"property": "__skip__"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "ezlo_app" not in result["data"]


async def test_new_property_form_is_prefilled_from_home_assistant(hass: HomeAssistant) -> None:
    """Create a new property: what HA knows is filled in; the platform's fields are asked."""
    await hass.config.async_update(location_name="Lake House", country="IN", time_zone="Asia/Kolkata")
    result = await _config_flow_at_picker(hass, CHOICES)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"property": "__new__"}
    )

    assert result["step_id"] == "ezlo_app_new_property"
    keys = {str(k): k for k in result["data_schema"].schema}
    assert list(keys) == [
        "property_name", "street_address", "additional_street_address",
        "city", "state", "zipcode", "country", "time_zone",
    ]
    assert keys["property_name"].description == {"suggested_value": "Lake House"}
    assert keys["country"].description == {"suggested_value": "IN"}
    assert keys["time_zone"].description == {"suggested_value": "Asia/Kolkata"}
    assert keys["street_address"].description == {"suggested_value": None}
    required = {str(k) for k in keys.values() if isinstance(k, vol.Required)}
    assert required == {"property_name", "street_address", "city", "state", "country", "zipcode"}

    connect = AsyncMock(return_value=EZLO_APP)
    with (
        patch(f"{FLOW}.async_connect", connect),
        patch("custom_components.ezlocloudharc.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "property_name": " Lake House ",
                "street_address": "2 Hill St",
                "additional_street_address": "",
                "city": "Ooty",
                "state": "Tamil Nadu",
                "zipcode": "643001",
                "country": "IN",
                "time_zone": "Asia/Kolkata",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert connect.await_args.kwargs["property_id"] is None
    assert connect.await_args.kwargs["new_property"] == {
        "property_name": "Lake House",
        "street_address": "2 Hill St",
        "city": "Ooty",
        "state": "Tamil Nadu",
        "zipcode": "643001",
        "country": "IN",
        "time_zone": "Asia/Kolkata",
    }


async def test_new_property_failure_stays_on_the_form(hass: HomeAssistant) -> None:
    """A refused create keeps what the user typed and says why."""
    result = await _config_flow_at_picker(hass, CHOICES)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"property": "__new__"}
    )
    typed = {
        "property_name": "Lake House", "street_address": "2 Hill St", "city": "Ooty",
        "state": "TN", "zipcode": "643001", "country": "IN",
    }
    with patch(
        f"{FLOW}.async_connect",
        AsyncMock(side_effect=EzloAppError("The property needs: zipcode")),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], typed)

    assert result["step_id"] == "ezlo_app_new_property"
    assert "needs: zipcode" in result["description_placeholders"]["error_detail"]
    keys = {str(k): k for k in result["data_schema"].schema}
    assert keys["street_address"].description == {"suggested_value": "2 Hill St"}


async def test_no_properties_goes_straight_to_the_form(hass: HomeAssistant) -> None:
    """A user with no property is asked for one; a legacy platform asks only the name."""
    legacy = PropertyChoices(
        source="legacy",
        properties=(),
        can_create=True,
        required_fields=frozenset({"property_name"}),
        optional_fields=frozenset(),
    )
    result = await _config_flow_at_picker(hass, legacy)
    assert result["step_id"] == "ezlo_app_new_property"
    assert [str(k) for k in result["data_schema"].schema] == ["property_name"]


@pytest.mark.parametrize(
    ("choices", "detail"),
    [
        (
            PropertyChoices(
                source="legacy", properties=(), can_create=False,
                required_fields=frozenset({"property_name"}), optional_fields=frozenset(),
            ),
            "Create one in the Ezlo app first",
        ),
        (EzloAppAuthError("bad"), "did not accept"),
        (EzloAppError("The property service did not respond"), "did not respond"),
    ],
)
async def test_picker_dead_ends_return_to_the_question(
    hass: HomeAssistant, choices: Any, detail: str
) -> None:
    """Nothing to pick or no list: back to the consent question with the reason."""
    result = await _flow_at_consent(hass)
    frp = AsyncMock(return_value={"server_name": "connect.harc.cloud", "subdomain": "abc123"})
    listing = (
        AsyncMock(side_effect=choices) if isinstance(choices, Exception)
        else AsyncMock(return_value=choices)
    )
    with (
        patch("custom_components.ezlocloudharc.config_flow.fetch_and_update_frp_config", frp),
        patch(f"{FLOW}.async_list_properties", listing),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "ezlo_app_allow"}
        )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "ezlo_app"
    assert detail in result["description_placeholders"]["error_detail"]


async def test_options_connect_with_picker(
    hass: HomeAssistant, subscribed_entry: MockConfigEntry
) -> None:
    """Configure → Connect Ezlo app → pick a property; the status names it."""
    result = await hass.config_entries.options.async_init(subscribed_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ezlo_app"}
    )
    connected = {**EZLO_APP, "property_id": "p-2", "property_name": "City Flat"}
    with _platform(AsyncMock(return_value=connected), CHOICES):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"password": "pw"}
        )
        assert result["step_id"] == "ezlo_app_property"
        # The options flow can simply be closed, so it offers no skip.
        values = [o["value"] for o in result["data_schema"].schema["property"].config["options"]]
        assert values == ["p-1", "p-2", "__new__"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"property": "p-2"}
        )

    assert result["reason"] == "ezlo_app_connected"
    assert subscribed_entry.data["ezlo_app"] == connected
    result = await hass.config_entries.options.async_init(subscribed_entry.entry_id)
    assert "**Ezlo app:** Connected to City Flat" in result["description_placeholders"]["body"]
