"""Ezlo HA Cloud integration API for Home Assistant."""

import base64
import binascii
import json
import logging

import httpx

from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import create_async_httpx_client

from .const import DEFAULT_API_URI

_LOGGER = logging.getLogger(__name__)


def _auth_api_url(api_uri: str) -> str:
    return f"{api_uri}/api/auth"


def _stripe_api_url(api_uri: str) -> str:
    return f"{api_uri}/api/stripe"


def _api_url(api_uri: str) -> str:
    return f"{api_uri}/api"


class SubscriptionExpiredError(Exception):
    """Raised when the API returns 402 (subscription expired)."""


def _raise_missing_uuid():
    raise ValueError("UUID missing in token payload")


async def authenticate(
    hass: HomeAssistant, username, password, uuid, *, api_uri: str = DEFAULT_API_URI
):
    """Authenticate against Ezlo API (async)."""
    payload = {
        "username": username,
        "password": password,
        "oem_id": "1",
        "ha_instance_id": uuid,
    }

    client = create_async_httpx_client(hass)
    try:
        response = await client.post(
            f"{_auth_api_url(api_uri)}/login", json=payload, timeout=10
        )
        response.raise_for_status()
        data = response.json()
        _LOGGER.info("Login response: %s", data)

        token = data.get("token")
        if token:
            payload = decode_jwt_payload(token)

            user_uuid = payload.get("uuid")
            ezlo_id = payload.get("ezlo_user_id")
            email = payload.get("email", "")
            username = payload.get("username", username)

            if not user_uuid:
                _raise_missing_uuid()

            return {
                "success": True,
                "data": {
                    "token": token,
                    "tunnel_token": data.get("tunnel_token"),
                    "user": {
                        "uuid": user_uuid,
                        "username": username,
                        "email": email,
                        "ezlo_id": ezlo_id,
                        "oem_id": 1,
                    },
                    "subscription_status": data.get("subscription_status"),
                    "is_trial": data.get("is_trial", False),
                    "payment_required": data.get("payment_required", False),
                    "trial_ends_at": data.get("trial_ends_at"),
                    "checkout_url": data.get("checkout_url"),
                },
                "error": None,
            }
        _LOGGER.warning("Login failed: %s", data)
        return {"success": False, "data": None, "error": "Invalid credentials"}  # noqa: TRY300

    except httpx.HTTPStatusError as e:
        try:
            error_data = e.response.json()
            error_msg = error_data.get("error", e.response.text)
        except (ValueError, KeyError):
            error_msg = e.response.text
        _LOGGER.error(
            "Auth request failed (HTTP %s): %s", e.response.status_code, error_msg
        )
        return {"success": False, "data": None, "error": error_msg}
    except (httpx.RequestError, ValueError, binascii.Error) as e:
        _LOGGER.error("Auth request failed: %s", e)
        return {"success": False, "data": None, "error": "API connection failed"}


async def signup(
    hass: HomeAssistant,
    username,
    email,
    password,
    ha_instance_id,
    *,
    api_uri: str = DEFAULT_API_URI,
):
    """Send signup request to Go Auth API and return the response."""
    _LOGGER.info("Sending signup request to Auth API")
    payload = {
        "username": username,
        "password": password,
        "email": email,
        # "uuid": ha_instance_id,
        "ha_instance_id": ha_instance_id,
    }

    client = create_async_httpx_client(hass)
    try:
        response = await client.post(
            f"{_auth_api_url(api_uri)}/signup", json=payload, timeout=5
        )
        response.raise_for_status()
        data = response.json()

        token = data.get("token")
        if token:
            _LOGGER.info("Signup successful")
            return {
                "success": True,
                "data": {
                    "token": token,
                    "tunnel_token": data.get("tunnel_token"),
                    "trial_ends_at": data.get("trial_ends_at"),
                    "subscription_status": data.get("subscription_status", ""),
                    "is_trial": data.get("is_trial", False),
                    "payment_required": data.get("payment_required", True),
                    "checkout_url": data.get("checkout_url"),
                },
                "error": None,
            }
        _LOGGER.warning("Signup failed. Response: %s", data)
        return {
            "success": False,
            "data": None,
            "error": data.get("message", "Signup failed"),
        }

    except httpx.HTTPStatusError as e:
        try:
            error_data = e.response.json()
            error_msg = error_data.get("error", e.response.text)
        except (ValueError, KeyError):
            error_msg = e.response.text
        _LOGGER.error(
            "Signup request failed (HTTP %s): %s",
            e.response.status_code,
            error_msg,
        )
        return {"success": False, "data": None, "error": error_msg}
    except httpx.RequestError as e:
        _LOGGER.error("Signup failed: %s", e)
        return {"success": False, "data": None, "error": "Network error"}


async def create_stripe_session(
    hass: HomeAssistant,
    user_id,
    price_id,
    back_ref_url,
    *,
    api_uri: str = DEFAULT_API_URI,
):
    """Create a Stripe Checkout session."""
    _LOGGER.info("Creating Stripe checkout session for user: %s", user_id)
    payload = {
        "user_id": user_id,
        "plan_price_id": price_id,
        "back_ref_url": back_ref_url,
    }

    client = create_async_httpx_client(hass)
    try:
        response = await client.post(
            f"{_stripe_api_url(api_uri)}/create-session", json=payload, timeout=10
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status") is True:
            checkout_url = data.get("data", {}).get("checkout_url")
            if checkout_url:
                _LOGGER.info("Stripe checkout session created")
                return {
                    "success": True,
                    "data": {"checkout_url": checkout_url},
                    "error": None,
                }
            _LOGGER.error("Stripe response missing checkout_url: %s", data)
            return {"success": False, "data": None, "error": "Missing checkout URL"}

        return {
            "success": False,
            "data": None,
            "error": data.get("error", "Unknown error"),
        }

    except httpx.HTTPStatusError as e:
        try:
            error_data = e.response.json()
            error_msg = error_data.get("message", e.response.text)
        except (ValueError, KeyError):
            error_msg = e.response.text
        _LOGGER.error(
            "Stripe session request failed (HTTP %s): %s",
            e.response.status_code,
            error_msg,
        )
        return {"success": False, "data": None, "error": error_msg}
    except httpx.RequestError as e:
        _LOGGER.error("Stripe checkout api error: %s", e)
        return {"success": False, "data": None, "error": "Stripe checkout api error"}


async def get_subscription_status(
    hass: HomeAssistant, user_uuid, *, api_uri: str = DEFAULT_API_URI
):
    """Fetch subscription status from Ezlo backend."""
    client = create_async_httpx_client(hass)
    try:
        response = await client.get(
            f"{_api_url(api_uri)}/subscription/status",
            params={"user_uuid": user_uuid},
            timeout=5,
        )
        response.raise_for_status()
        data = response.json().get("data")

        if data:
            return {
                "success": True,
                "status": data.get("status", "unknown"),
                "is_active": data.get("is_active", False),
                "start_timestamp": data.get("start_timestamp", ""),
                "end_timestamp": data.get("end_timestamp", ""),
            }

        return {"success": False, "error": "No data returned"}  # noqa: TRY300

    except httpx.HTTPStatusError as e:
        _LOGGER.warning(
            "Subscription status returned %s — treating as still pending",
            e.response.status_code,
        )
        return {"success": False, "error": f"http_{e.response.status_code}"}
    except httpx.RequestError as e:
        _LOGGER.error("Failed to fetch subscription status: %s", e)
        return {"success": False, "error": "Network error"}


_INTEGRATION_CONFIG_CACHE: dict[str, dict] = {}


async def get_integration_config(
    hass: HomeAssistant, *, api_uri: str = DEFAULT_API_URI
) -> dict | None:
    """Fetch public integration config (Stripe price id, etc.) from the backend.

    Cached per-api_uri for the lifetime of the HA process — the values are
    static per deployment and the call is cheap. Returns None on failure so
    callers can surface a clean error.
    """
    cached = _INTEGRATION_CONFIG_CACHE.get(api_uri)
    if cached is not None:
        return cached

    client = create_async_httpx_client(hass)
    try:
        response = await client.get(
            f"{_api_url(api_uri)}/integration/config", timeout=5
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        _INTEGRATION_CONFIG_CACHE[api_uri] = data
        return data  # noqa: TRY300
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        _LOGGER.error("Failed to fetch integration config: %s", e)
        return None


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT token and return its payload as a dictionary."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")

    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))
