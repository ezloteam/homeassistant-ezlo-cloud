"""Tests for the Ezlo HA Cloud API client (api.py)."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.core import HomeAssistant

from custom_components.ezlohacloud.api import (
    AuthResult,
    StripeSession,
    SubscriptionStatusResult,
    authenticate,
    create_stripe_session,
    decode_jwt_payload,
    get_integration_config,
    get_subscription_status,
    signup,
)
from custom_components.ezlohacloud.exceptions import (
    EzloApiUnexpectedResponseError,
    EzloApiUnreachableError,
    EzloAuthError,
    EzloMissingUUIDError,
)

USER_UUID = "f960d12e-4ccb-4f0a-b37f-0abee2cd9717"
EZLO_USER_ID = 15047842


def _make_jwt(payload: dict[str, Any]) -> str:
    """Build a fake JWT — only the payload is meaningful, signature is junk."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.signature"


def _mock_response(
    *, json_data: dict[str, Any] | None = None, status_code: int = 200
) -> MagicMock:
    """Build a mock httpx.Response with the given JSON body and status."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json = MagicMock(return_value=json_data or {})
    response.text = json.dumps(json_data) if json_data else ""
    if status_code >= 400:
        response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                f"{status_code}", request=MagicMock(), response=response
            )
        )
    else:
        response.raise_for_status = MagicMock()
    return response


def _patch_client(
    *, response: MagicMock | None = None, error: Exception | None = None
) -> MagicMock:
    """Patch get_async_client to return a mock with the desired behaviour."""
    client = MagicMock()
    if error is not None:
        client.post = AsyncMock(side_effect=error)
        client.get = AsyncMock(side_effect=error)
    else:
        client.post = AsyncMock(return_value=response)
        client.get = AsyncMock(return_value=response)
    return client


# ── decode_jwt_payload ──────────────────────────────────────────────


def test_decode_jwt_payload_valid() -> None:
    """A well-formed JWT round-trips through decode_jwt_payload."""
    payload = {"uuid": USER_UUID, "ezlo_user_id": EZLO_USER_ID, "exp": 9999999999}
    token = _make_jwt(payload)
    assert decode_jwt_payload(token) == payload


def test_decode_jwt_payload_invalid_format() -> None:
    """Tokens that don't have three dot-separated parts raise ValueError."""
    with pytest.raises(ValueError, match="Invalid JWT format"):
        decode_jwt_payload("only-one-part")
    with pytest.raises(ValueError, match="Invalid JWT format"):
        decode_jwt_payload("a.b.c.d")


# ── authenticate ────────────────────────────────────────────────────


async def test_authenticate_success(hass: HomeAssistant) -> None:
    """A successful login returns a populated AuthResult dataclass."""
    token = _make_jwt(
        {
            "uuid": USER_UUID,
            "ezlo_user_id": EZLO_USER_ID,
            "email": "user@example.com",
            "username": "user",
        }
    )
    response = _mock_response(
        json_data={
            "token": token,
            "tunnel_token": "tunnel-abc",
            "subscription_status": "trialing",
            "is_trial": True,
            "trial_ends_at": "2026-05-28T00:00:00Z",
            "payment_required": False,
        }
    )
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        result = await authenticate(hass, "user", "pw", "ha-uuid")

    assert isinstance(result, AuthResult)
    assert result.token == token
    assert result.tunnel_token == "tunnel-abc"
    assert result.user["uuid"] == USER_UUID
    assert result.user["ezlo_id"] == EZLO_USER_ID
    assert result.subscription_status == "trialing"
    assert result.is_trial is True
    assert result.payment_required is False


async def test_authenticate_no_token_raises_auth_error(
    hass: HomeAssistant,
) -> None:
    """A response missing the `token` field raises EzloAuthError."""
    response = _mock_response(json_data={"message": "Login failed"})
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloAuthError, match="Invalid credentials"),
    ):
        await authenticate(hass, "user", "pw", "ha-uuid")


async def test_authenticate_4xx_raises_auth_error(hass: HomeAssistant) -> None:
    """A 401 / 403 with a JSON body raises EzloAuthError with the message."""
    response = _mock_response(
        json_data={"error": "invalid credentials"}, status_code=401
    )
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloAuthError, match="invalid credentials"),
    ):
        await authenticate(hass, "user", "pw", "ha-uuid")


async def test_authenticate_5xx_raises_unexpected_response(
    hass: HomeAssistant,
) -> None:
    """A 5xx response raises EzloApiUnexpectedResponseError."""
    response = _mock_response(json_data={"error": "boom"}, status_code=500)
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnexpectedResponseError),
    ):
        await authenticate(hass, "user", "pw", "ha-uuid")


async def test_authenticate_network_error_raises_unreachable(
    hass: HomeAssistant,
) -> None:
    """A network error raises EzloApiUnreachableError."""
    client = _patch_client(error=httpx.ConnectError("dns"))
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnreachableError),
    ):
        await authenticate(hass, "user", "pw", "ha-uuid")


async def test_authenticate_missing_uuid_raises(hass: HomeAssistant) -> None:
    """A token whose payload omits `uuid` raises EzloMissingUUIDError."""
    token = _make_jwt({"username": "u", "exp": 9999999999})  # no uuid
    response = _mock_response(json_data={"token": token})
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloMissingUUIDError),
    ):
        await authenticate(hass, "user", "pw", "ha-uuid")


# ── signup ──────────────────────────────────────────────────────────


async def test_signup_success(hass: HomeAssistant) -> None:
    """A successful signup returns an AuthResult populated from the JWT."""
    token = _make_jwt({"uuid": USER_UUID, "ezlo_user_id": EZLO_USER_ID})
    response = _mock_response(
        json_data={
            "token": token,
            "tunnel_token": "tt",
            "subscription_status": "",
            "is_trial": False,
            "payment_required": True,
            "checkout_url": "https://checkout.stripe.com/abc",
        }
    )
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        result = await signup(hass, "u", "u@x.com", "pw", "ha-uuid")

    assert isinstance(result, AuthResult)
    assert result.token == token
    assert result.checkout_url == "https://checkout.stripe.com/abc"
    assert result.payment_required is True


async def test_signup_no_token_raises_auth_error(hass: HomeAssistant) -> None:
    """A response without a token raises EzloAuthError with the message."""
    response = _mock_response(json_data={"message": "Username already exists"})
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloAuthError, match="Username already exists"),
    ):
        await signup(hass, "u", "u@x.com", "pw", "ha-uuid")


async def test_signup_409_raises_auth_error(hass: HomeAssistant) -> None:
    """A 409 with a JSON body raises EzloAuthError carrying that message."""
    response = _mock_response(
        json_data={"error": "Username taken"}, status_code=409
    )
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloAuthError, match="Username taken"),
    ):
        await signup(hass, "u", "u@x.com", "pw", "ha-uuid")


async def test_signup_network_error_raises_unreachable(
    hass: HomeAssistant,
) -> None:
    """A network error during signup raises EzloApiUnreachableError."""
    client = _patch_client(error=httpx.ConnectError("dns"))
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnreachableError),
    ):
        await signup(hass, "u", "u@x.com", "pw", "ha-uuid")


# ── create_stripe_session ───────────────────────────────────────────


async def test_create_stripe_session_success(hass: HomeAssistant) -> None:
    """A success response yields a StripeSession with the checkout_url."""
    response = _mock_response(
        json_data={
            "status": True,
            "data": {"checkout_url": "https://checkout.stripe.com/xyz"},
        }
    )
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        session = await create_stripe_session(
            hass, USER_UUID, "price_123", "https://back"
        )

    assert isinstance(session, StripeSession)
    assert session.checkout_url == "https://checkout.stripe.com/xyz"


async def test_create_stripe_session_status_false_raises(
    hass: HomeAssistant,
) -> None:
    """status: False raises EzloApiUnexpectedResponseError with the message."""
    response = _mock_response(json_data={"status": False, "error": "Stripe down"})
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnexpectedResponseError, match="Stripe down"),
    ):
        await create_stripe_session(hass, USER_UUID, "price_123", "https://back")


async def test_create_stripe_session_missing_url_raises(
    hass: HomeAssistant,
) -> None:
    """A success response without a checkout_url raises unexpected-response."""
    response = _mock_response(json_data={"status": True, "data": {}})
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnexpectedResponseError, match="Missing checkout URL"),
    ):
        await create_stripe_session(hass, USER_UUID, "price_123", "https://back")


async def test_create_stripe_session_network_error_raises_unreachable(
    hass: HomeAssistant,
) -> None:
    """A network error raises EzloApiUnreachableError."""
    client = _patch_client(error=httpx.ConnectError("dns"))
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnreachableError),
    ):
        await create_stripe_session(hass, USER_UUID, "price_123", "https://back")


# ── get_subscription_status ─────────────────────────────────────────


async def test_get_subscription_status_success(hass: HomeAssistant) -> None:
    """A success response yields a SubscriptionStatusResult with all fields."""
    response = _mock_response(
        json_data={
            "data": {
                "status": "active",
                "is_active": True,
                "start_timestamp": "2026-01-01",
                "end_timestamp": "2026-12-31",
            }
        }
    )
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        result = await get_subscription_status(hass, USER_UUID)

    assert isinstance(result, SubscriptionStatusResult)
    assert result.status == "active"
    assert result.is_active is True
    assert result.start_timestamp == "2026-01-01"
    assert result.end_timestamp == "2026-12-31"


async def test_get_subscription_status_empty_data_raises(
    hass: HomeAssistant,
) -> None:
    """A response with a null data section raises unexpected-response."""
    response = _mock_response(json_data={"data": None})
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnexpectedResponseError),
    ):
        await get_subscription_status(hass, USER_UUID)


async def test_get_subscription_status_404_raises_unexpected_response(
    hass: HomeAssistant,
) -> None:
    """A 4xx response raises EzloApiUnexpectedResponseError (caller may catch)."""
    response = _mock_response(json_data={}, status_code=404)
    client = _patch_client(response=response)
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnexpectedResponseError, match="http_404"),
    ):
        await get_subscription_status(hass, USER_UUID)


async def test_get_subscription_status_network_error_raises_unreachable(
    hass: HomeAssistant,
) -> None:
    """A network error raises EzloApiUnreachableError."""
    client = _patch_client(error=httpx.ConnectError("dns"))
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnreachableError),
    ):
        await get_subscription_status(hass, USER_UUID)


# ── get_integration_config ──────────────────────────────────────────


async def test_get_integration_config_success(hass: HomeAssistant) -> None:
    """A success response returns the unwrapped data dict."""
    response = _mock_response(json_data={"data": {"stripe_price_id": "price_123"}})
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        result = await get_integration_config(hass)

    assert result == {"stripe_price_id": "price_123"}


async def test_get_integration_config_network_error_raises(
    hass: HomeAssistant,
) -> None:
    """A network error raises EzloApiUnreachableError."""
    client = _patch_client(error=httpx.ConnectError("dns"))
    with (
        patch(
            "custom_components.ezlohacloud.api.get_async_client", return_value=client
        ),
        pytest.raises(EzloApiUnreachableError),
    ):
        await get_integration_config(hass)


# ── api_uri override ─────────────────────────────────────────────────


_DEV_API = "https://api-dev.harc.cloud"


async def test_authenticate_uses_api_uri_override(hass: HomeAssistant) -> None:
    """authenticate(api_uri=...) targets the override host."""
    token = _make_jwt({"uuid": USER_UUID, "ezlo_user_id": EZLO_USER_ID})
    response = _mock_response(json_data={"token": token})
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        await authenticate(hass, "u", "pw", "ha-uuid", api_uri=_DEV_API)

    called_url = client.post.await_args.args[0]
    assert called_url == f"{_DEV_API}/api/auth/login"


async def test_signup_uses_api_uri_override(hass: HomeAssistant) -> None:
    """signup(api_uri=...) targets the override host."""
    token = _make_jwt({"uuid": USER_UUID, "ezlo_user_id": EZLO_USER_ID})
    response = _mock_response(json_data={"token": token, "payment_required": False})
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        await signup(hass, "u", "u@x.com", "pw", "ha-uuid", api_uri=_DEV_API)

    assert client.post.await_args.args[0] == f"{_DEV_API}/api/auth/signup"


async def test_create_stripe_session_uses_api_uri_override(
    hass: HomeAssistant,
) -> None:
    """create_stripe_session(api_uri=...) targets the override host."""
    response = _mock_response(
        json_data={"status": True, "data": {"checkout_url": "https://x"}}
    )
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        await create_stripe_session(
            hass, USER_UUID, "price", "https://back", api_uri=_DEV_API
        )

    assert client.post.await_args.args[0] == f"{_DEV_API}/api/stripe/create-session"


async def test_get_subscription_status_uses_api_uri_override(
    hass: HomeAssistant,
) -> None:
    """get_subscription_status(api_uri=...) targets the override host."""
    response = _mock_response(
        json_data={"data": {"status": "active", "is_active": True}}
    )
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        await get_subscription_status(hass, USER_UUID, api_uri=_DEV_API)

    assert client.get.await_args.args[0] == f"{_DEV_API}/api/subscription/status"


async def test_get_integration_config_uses_api_uri_override(
    hass: HomeAssistant,
) -> None:
    """get_integration_config(api_uri=...) targets the override host."""
    response = _mock_response(json_data={"data": {"stripe_price_id": "p"}})
    client = _patch_client(response=response)
    with patch(
        "custom_components.ezlohacloud.api.get_async_client", return_value=client
    ):
        await get_integration_config(hass, api_uri=_DEV_API)

    assert client.get.await_args.args[0] == f"{_DEV_API}/api/integration/config"
