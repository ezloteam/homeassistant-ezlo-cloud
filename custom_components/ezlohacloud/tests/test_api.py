"""Tests for the Ezlo HA Cloud API client (api.py)."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from homeassistant.components.ezlohacloud import api
from homeassistant.components.ezlohacloud.api import (
    authenticate,
    create_stripe_session,
    decode_jwt_payload,
    get_integration_config,
    get_subscription_status,
    signup,
)
from homeassistant.core import HomeAssistant

USER_UUID = "f960d12e-4ccb-4f0a-b37f-0abee2cd9717"
EZLO_USER_ID = 15047842


def _make_jwt(payload: dict) -> str:
    """Build a fake JWT — only the payload is meaningful here, signature is junk."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.signature"


def _mock_client_with_response(
    *, json_data: dict | None = None, status_code: int = 200
) -> MagicMock:
    """Build a mock async httpx client that returns the given JSON/status."""
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=json_data or {})
    response.text = json.dumps(json_data) if json_data else ""
    response.raise_for_status = MagicMock()
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status_code}", request=MagicMock(), response=response
        )
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.get = AsyncMock(return_value=response)
    return client


def _mock_client_with_error(error: Exception) -> MagicMock:
    """Build a mock async httpx client whose requests raise the given error."""
    client = MagicMock()
    client.post = AsyncMock(side_effect=error)
    client.get = AsyncMock(side_effect=error)
    return client


@pytest.fixture(autouse=True)
def _clear_integration_config_cache() -> None:
    """Reset the module-level cache between tests."""
    api._INTEGRATION_CONFIG_CACHE = None
    yield
    api._INTEGRATION_CONFIG_CACHE = None


# ── decode_jwt_payload ──────────────────────────────────────────────


def test_decode_jwt_payload_valid() -> None:
    """Valid JWT round-trips through decode_jwt_payload."""
    payload = {"uuid": USER_UUID, "ezlo_user_id": EZLO_USER_ID, "exp": 9999999999}
    token = _make_jwt(payload)
    assert decode_jwt_payload(token) == payload


def test_decode_jwt_payload_invalid_format() -> None:
    """Tokens that don't have three dot-separated parts raise ValueError."""
    with pytest.raises(ValueError, match="Invalid JWT format"):
        decode_jwt_payload("not.a.jwt.too.many.parts")
    with pytest.raises(ValueError, match="Invalid JWT format"):
        decode_jwt_payload("only-one-part")


# ── authenticate ────────────────────────────────────────────────────


async def test_authenticate_success(hass: HomeAssistant) -> None:
    """Successful login returns the expected normalised payload."""
    token = _make_jwt(
        {
            "uuid": USER_UUID,
            "ezlo_user_id": EZLO_USER_ID,
            "email": "user@example.com",
            "username": "user",
        }
    )
    client = _mock_client_with_response(
        json_data={
            "token": token,
            "tunnel_token": "tunnel-abc",
            "subscription_status": "trialing",
            "is_trial": True,
            "trial_ends_at": "2026-05-28T00:00:00Z",
            "payment_required": False,
        }
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await authenticate(hass, "user", "pw", "ha-uuid")

    assert result["success"] is True
    assert result["data"]["token"] == token
    assert result["data"]["tunnel_token"] == "tunnel-abc"
    assert result["data"]["user"]["uuid"] == USER_UUID
    assert result["data"]["user"]["ezlo_id"] == EZLO_USER_ID
    assert result["data"]["subscription_status"] == "trialing"
    assert result["data"]["is_trial"] is True
    assert result["data"]["payment_required"] is False


async def test_authenticate_no_token_in_response(hass: HomeAssistant) -> None:
    """Response without a token returns success=False."""
    client = _mock_client_with_response(json_data={"message": "Login failed"})
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await authenticate(hass, "user", "wrong-pw", "ha-uuid")

    assert result["success"] is False
    assert result["error"] == "Invalid credentials"


async def test_authenticate_http_error_with_json_body(hass: HomeAssistant) -> None:
    """4xx with JSON body surfaces the API error field."""
    client = _mock_client_with_response(
        json_data={"error": "invalid credentials"}, status_code=401
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await authenticate(hass, "user", "pw", "ha-uuid")

    assert result["success"] is False
    assert result["error"] == "invalid credentials"


async def test_authenticate_http_error_non_json_body(hass: HomeAssistant) -> None:
    """4xx where the body isn't JSON falls back to response.text."""
    response = MagicMock()
    response.status_code = 500
    response.text = "Internal Server Error"
    response.json = MagicMock(side_effect=ValueError("not json"))
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=response)
    )
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await authenticate(hass, "user", "pw", "ha-uuid")

    assert result["success"] is False
    assert result["error"] == "Internal Server Error"


async def test_authenticate_network_error(hass: HomeAssistant) -> None:
    """Network errors (no response) return a generic connection-failed message."""
    client = _mock_client_with_error(httpx.ConnectError("dns"))
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await authenticate(hass, "user", "pw", "ha-uuid")

    assert result["success"] is False
    assert result["error"] == "API connection failed"


async def test_authenticate_missing_uuid_in_token(hass: HomeAssistant) -> None:
    """A token without a uuid claim falls back to the generic error path."""
    token = _make_jwt({"username": "u", "exp": 9999999999})  # no uuid
    client = _mock_client_with_response(json_data={"token": token})
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await authenticate(hass, "user", "pw", "ha-uuid")

    assert result["success"] is False
    assert result["error"] == "API connection failed"


# ── signup ──────────────────────────────────────────────────────────


async def test_signup_success(hass: HomeAssistant) -> None:
    """Successful signup returns the new trial fields plus checkout_url."""
    token = _make_jwt({"uuid": USER_UUID, "ezlo_user_id": EZLO_USER_ID})
    client = _mock_client_with_response(
        json_data={
            "token": token,
            "tunnel_token": "tt",
            "subscription_status": "",
            "is_trial": False,
            "payment_required": True,
            "checkout_url": "https://checkout.stripe.com/abc",
        }
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await signup(hass, "u", "u@x.com", "pw", "ha-uuid")

    assert result["success"] is True
    assert result["data"]["token"] == token
    assert result["data"]["checkout_url"] == "https://checkout.stripe.com/abc"
    assert result["data"]["payment_required"] is True


async def test_signup_no_token(hass: HomeAssistant) -> None:
    """Response without a token returns the backend's message."""
    client = _mock_client_with_response(
        json_data={"message": "Username already exists"}
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await signup(hass, "u", "u@x.com", "pw", "ha-uuid")

    assert result["success"] is False
    assert result["error"] == "Username already exists"


async def test_signup_http_error_with_json(hass: HomeAssistant) -> None:
    """HTTP error with a JSON body surfaces the error field."""
    client = _mock_client_with_response(
        json_data={"error": "Username taken"}, status_code=409
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await signup(hass, "u", "u@x.com", "pw", "ha-uuid")

    assert result["success"] is False
    assert result["error"] == "Username taken"


async def test_signup_network_error(hass: HomeAssistant) -> None:
    """Network errors return a generic 'Network error' message."""
    client = _mock_client_with_error(httpx.ConnectError("dns"))
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await signup(hass, "u", "u@x.com", "pw", "ha-uuid")

    assert result["success"] is False
    assert result["error"] == "Network error"


# ── create_stripe_session ───────────────────────────────────────────


async def test_create_stripe_session_success(hass: HomeAssistant) -> None:
    """Successful response extracts checkout_url from nested data."""
    client = _mock_client_with_response(
        json_data={
            "status": True,
            "data": {"checkout_url": "https://checkout.stripe.com/xyz"},
        }
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await create_stripe_session(
            hass, USER_UUID, "price_123", "https://back"
        )

    assert result["success"] is True
    assert result["data"]["checkout_url"] == "https://checkout.stripe.com/xyz"


async def test_create_stripe_session_missing_checkout_url(
    hass: HomeAssistant,
) -> None:
    """status: True but no checkout_url returns a specific error."""
    client = _mock_client_with_response(json_data={"status": True, "data": {}})
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await create_stripe_session(
            hass, USER_UUID, "price_123", "https://back"
        )

    assert result["success"] is False
    assert result["error"] == "Missing checkout URL"


async def test_create_stripe_session_status_false(hass: HomeAssistant) -> None:
    """status: False returns the API's error message."""
    client = _mock_client_with_response(
        json_data={"status": False, "error": "Stripe down"}
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await create_stripe_session(
            hass, USER_UUID, "price_123", "https://back"
        )

    assert result["success"] is False
    assert result["error"] == "Stripe down"


async def test_create_stripe_session_http_error(hass: HomeAssistant) -> None:
    """HTTP error returns the parsed message field."""
    client = _mock_client_with_response(
        json_data={"message": "Stripe API key missing"}, status_code=500
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await create_stripe_session(
            hass, USER_UUID, "price_123", "https://back"
        )

    assert result["success"] is False
    assert result["error"] == "Stripe API key missing"


async def test_create_stripe_session_network_error(hass: HomeAssistant) -> None:
    """Network errors during Stripe session creation return a generic error."""
    client = _mock_client_with_error(httpx.ConnectError("dns"))
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await create_stripe_session(
            hass, USER_UUID, "price_123", "https://back"
        )

    assert result["success"] is False
    assert result["error"] == "Stripe checkout api error"


# ── get_subscription_status ─────────────────────────────────────────


async def test_get_subscription_status_success(hass: HomeAssistant) -> None:
    """Success path returns the unpacked subscription fields."""
    client = _mock_client_with_response(
        json_data={
            "data": {
                "status": "active",
                "is_active": True,
                "start_timestamp": "2026-01-01",
                "end_timestamp": "2026-12-31",
            }
        }
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await get_subscription_status(hass, USER_UUID)

    assert result["success"] is True
    assert result["status"] == "active"
    assert result["is_active"] is True


async def test_get_subscription_status_empty_data(hass: HomeAssistant) -> None:
    """Empty data field returns success=False with 'No data returned'."""
    client = _mock_client_with_response(json_data={"data": None})
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await get_subscription_status(hass, USER_UUID)

    assert result["success"] is False
    assert result["error"] == "No data returned"


async def test_get_subscription_status_http_error_treats_as_pending(
    hass: HomeAssistant,
) -> None:
    """HTTP 4xx during polling is treated as 'still pending', not a hard error."""
    client = _mock_client_with_response(json_data={}, status_code=404)
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await get_subscription_status(hass, USER_UUID)

    assert result["success"] is False
    assert result["error"] == "http_404"


async def test_get_subscription_status_network_error(hass: HomeAssistant) -> None:
    """Network errors return a generic Network error message."""
    client = _mock_client_with_error(httpx.ConnectError("dns"))
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await get_subscription_status(hass, USER_UUID)

    assert result["success"] is False
    assert result["error"] == "Network error"


# ── get_integration_config (with cache) ─────────────────────────────


async def test_get_integration_config_first_call_fetches(
    hass: HomeAssistant,
) -> None:
    """First call hits the API and returns the data payload."""
    client = _mock_client_with_response(
        json_data={"data": {"stripe_price_id": "price_123"}}
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        result = await get_integration_config(hass)

    assert result == {"stripe_price_id": "price_123"}
    assert client.get.await_count == 1


async def test_get_integration_config_cached_after_first_call(
    hass: HomeAssistant,
) -> None:
    """Second call returns from cache without hitting the API again."""
    client = _mock_client_with_response(
        json_data={"data": {"stripe_price_id": "price_123"}}
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        first = await get_integration_config(hass)
        second = await get_integration_config(hass)

    assert first == second == {"stripe_price_id": "price_123"}
    # Only one network call across both invocations
    assert client.get.await_count == 1


async def test_get_integration_config_returns_none_on_error(
    hass: HomeAssistant,
) -> None:
    """Network or HTTP errors return None, do not raise."""
    client = _mock_client_with_error(httpx.ConnectError("dns"))
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=client,
    ):
        assert await get_integration_config(hass) is None


async def test_get_integration_config_error_not_cached(
    hass: HomeAssistant,
) -> None:
    """A failed first call doesn't pollute the cache — retry should work."""
    error_client = _mock_client_with_error(httpx.ConnectError("dns"))
    success_client = _mock_client_with_response(
        json_data={"data": {"stripe_price_id": "price_123"}}
    )
    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=error_client,
    ):
        assert await get_integration_config(hass) is None

    with patch(
        "homeassistant.components.ezlohacloud.api.create_async_httpx_client",
        return_value=success_client,
    ):
        assert await get_integration_config(hass) == {"stripe_price_id": "price_123"}
