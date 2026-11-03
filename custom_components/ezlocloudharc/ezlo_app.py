"""Connect this Home Assistant to the Ezlo app (the Ezlo property platform).

With the user's consent the integration creates a dedicated "Ezlo" Home
Assistant user, mints a long-lived access token for it, and registers the hub
in the user's Ezlo property with ``registerHomeAssistantHub`` — so nobody has
to copy and paste a token. The token stays visible and revocable in Home
Assistant: deactivating or deleting the "Ezlo" user (Settings → People) cuts
the platform off, as does "Disconnect Ezlo app" in the integration options.

The user picks which of their properties the hub goes to, or creates one
(``listHomeAssistantProperties``). The platform decides where properties live:
the v4 Property API for accounts migrated to it, its own tables otherwise.
Against a platform without the picker the hub goes to the user's own account,
as before.

The platform only offers browser-style auth (an Ezlo username/password login
that sets a session cookie), so registration has to happen while the user is
typing their password in a flow step. The password is never stored. The
planned follow-up moves registration server-to-server through the HARC API so
no password is needed (internal design note: docs/ezlo-app-registration.md).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import (
    TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
    RefreshToken,
    User,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DEFAULT_PROPERTY_URI
from .exceptions import EzloAppAuthError, EzloAppError
from .models import EzloAppData

_LOGGER = logging.getLogger(__name__)

EZLO_APP_USER_NAME = "Ezlo"
EZLO_APP_TOKEN_NAME = "Ezlo app"
# Matches what the Ezlo onboarding component mints; the user revokes it in
# Home Assistant rather than waiting for it to expire.
EZLO_APP_TOKEN_LIFETIME = timedelta(days=3650)

_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Laravel answers validation and auth failures with JSON only when asked to.
_HEADERS = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

# Fields of a new property, in the order the form shows them. Named as the v4
# Property API names them; the platform says which it requires and which it can
# store (a property kept in the platform's own tables stores only the name).
NEW_PROPERTY_FIELDS = (
    "property_name",
    "street_address",
    "additional_street_address",
    "city",
    "state",
    "zipcode",
    "country",
    "time_zone",
)


@dataclass(frozen=True)
class PropertyOption:
    """A property the hub can be added to."""

    id: str
    name: str
    address: str = ""
    has_hub: bool = False


@dataclass(frozen=True)
class PropertyChoices:
    """What the user can pick from, as listHomeAssistantProperties reports it."""

    source: str
    properties: tuple[PropertyOption, ...]
    can_create: bool
    required_fields: frozenset[str]
    optional_fields: frozenset[str]


async def async_list_properties(
    hass: HomeAssistant,
    *,
    username: str,
    password: str,
    ha_url: str,
    property_uri: str = DEFAULT_PROPERTY_URI,
) -> PropertyChoices | None:
    """Return the properties the user can add this Home Assistant to.

    ``None`` means the platform predates the property picker; the caller then
    registers on the user's own account, as before.

    Raises:
        EzloAppAuthError: the platform rejected the credentials.
        EzloAppError: the list could not be read. An unreadable list is never
            reported as an empty one, which would offer to create a duplicate.
    """
    session = async_create_clientsession(
        hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
    )
    try:
        await _async_login(session, property_uri, username, password)
        body = await _async_post(
            session,
            f"{property_uri}/api/",
            {"mode": "listHomeAssistantProperties", "ha_url": ha_url},
            allow_empty=True,
        )
    finally:
        session.detach()

    # An unknown mode makes the platform answer with an empty body.
    if body is None:
        return None
    if body.get("status") != 1:
        raise EzloAppError(str(body.get("message") or "Could not read your properties"))
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("properties"), list):
        raise EzloAppError("The Ezlo app sent an unexpected property list")
    fields = data.get("create_fields") if isinstance(data.get("create_fields"), dict) else {}
    return PropertyChoices(
        source=str(data.get("source") or ""),
        properties=tuple(
            PropertyOption(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                address=str(item.get("address") or ""),
                has_hub=bool(item.get("has_hub")),
            )
            for item in data["properties"]
            if isinstance(item, dict) and item.get("id") not in (None, "")
        ),
        can_create=bool(data.get("can_create")),
        required_fields=frozenset(fields.get("required") or ("property_name",)),
        optional_fields=frozenset(fields.get("optional") or ()),
    )


async def async_connect(
    hass: HomeAssistant,
    *,
    username: str,
    password: str,
    ha_url: str,
    property_uri: str = DEFAULT_PROPERTY_URI,
    previous: EzloAppData | None = None,
    property_id: str | None = None,
    new_property: dict[str, str] | None = None,
) -> EzloAppData:
    """Register this Home Assistant in the user's Ezlo property.

    The hub goes to ``property_id``, or to a property created from
    ``new_property``; with neither, the platform uses the user's own account.

    Signs in to the platform first, so a wrong password fails before anything
    is created in Home Assistant. Reuses the "Ezlo" user from ``previous`` and
    rotates its token: the new token is registered before the old one is
    revoked, so a failed reconnect leaves the existing connection working.

    Raises:
        EzloAppAuthError: the platform rejected the credentials.
        EzloAppError: anything else went wrong; nothing new is left behind.
    """
    session = async_create_clientsession(
        hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
    )
    try:
        pk_account = await _async_login(session, property_uri, username, password)

        user, created = await _async_get_or_create_user(hass, previous)
        refresh_token = await hass.auth.async_create_refresh_token(
            user,
            client_name=_unused_token_name(user),
            token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
            access_token_expiration=EZLO_APP_TOKEN_LIFETIME,
        )
        access_token = hass.auth.async_create_access_token(refresh_token)

        try:
            hub = await _async_register(
                session,
                property_uri,
                pk_account=pk_account,
                ha_url=ha_url,
                ha_token=access_token,
                name=hass.config.location_name or "Home Assistant",
                property_id=property_id,
                new_property=new_property,
            )
        except EzloAppError:
            hass.auth.async_remove_refresh_token(refresh_token)
            if created:
                await hass.auth.async_remove_user(user)
            raise
    finally:
        session.detach()

    _revoke_other_tokens(hass, user, keep=refresh_token)
    data = EzloAppData(
        ha_user_id=user.id,
        refresh_token_id=refresh_token.id,
        pk_account=pk_account,
        serial=str(hub["serial"]),
        ha_url=ha_url,
    )
    if hub.get("property_id") not in (None, ""):
        data["property_id"] = str(hub["property_id"])
    if hub.get("property_name"):
        data["property_name"] = str(hub["property_name"])
    return data


async def async_disconnect(hass: HomeAssistant, data: EzloAppData | None) -> None:
    """Remove the "Ezlo" user, which revokes every token the platform holds.

    The hub stays listed in the Ezlo app (removing it needs a platform session)
    but shows offline from then on; the user can remove it there.
    """
    if not data:
        return
    user = await hass.auth.async_get_user(data["ha_user_id"])
    if user is not None:
        await hass.auth.async_remove_user(user)


# ── Home Assistant side ─────────────────────────────────────────────


async def _async_get_or_create_user(
    hass: HomeAssistant, previous: EzloAppData | None
) -> tuple[User, bool]:
    """Return the "Ezlo" user, creating it when missing.

    It is an administrator because the platform edits automations and the
    entity registry, which Home Assistant only allows admins to do. It is not
    local-only: the platform reaches Home Assistant through the HARC tunnel.
    A user the owner deactivated is left alone — that is their way of saying no.
    """
    if previous:
        user = await hass.auth.async_get_user(previous["ha_user_id"])
        if user is not None:
            if not user.is_active:
                raise EzloAppError(
                    f'The "{EZLO_APP_USER_NAME}" user is deactivated. Activate it '
                    "in Settings → People, or delete it, and try again."
                )
            return user, False
    user = await hass.auth.async_create_user(
        EZLO_APP_USER_NAME, group_ids=[GROUP_ID_ADMIN], local_only=False
    )
    return user, True


def _unused_token_name(user: User) -> str:
    """Pick a token name the user does not have yet.

    Home Assistant allows one long-lived token per name, and while a
    reconnect is in flight the old token still exists.
    """
    taken = {
        token.client_name
        for token in user.refresh_tokens.values()
        if token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    }
    name = EZLO_APP_TOKEN_NAME
    suffix = 2
    while name in taken:
        name = f"{EZLO_APP_TOKEN_NAME} {suffix}"
        suffix += 1
    return name


def _revoke_other_tokens(hass: HomeAssistant, user: User, keep: RefreshToken) -> None:
    """Revoke the user's other long-lived tokens once the new one is registered."""
    for token in list(user.refresh_tokens.values()):
        if token.id != keep.id and token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
            hass.auth.async_remove_refresh_token(token)


# ── Platform side ───────────────────────────────────────────────────


async def _async_login(
    session: aiohttp.ClientSession, base: str, username: str, password: str
) -> int:
    """Sign in to the platform (sets the session cookie); return the PK_account.

    ``pk_oem: auto`` lets the platform find the OEM the account belongs to,
    the same way its own login page does.
    """
    body = await _async_post(
        session,
        f"{base}/api/login/",
        {"username": username, "password": password, "pk_oem": "auto"},
        auth_step=True,
    )
    if body.get("status") != 1:
        raise EzloAppAuthError(str(body.get("message") or "Invalid Login"))
    try:
        return int(body["PK_account"])
    except (KeyError, TypeError, ValueError) as err:
        raise EzloAppError("The Ezlo app did not return an account") from err


async def _async_register(
    session: aiohttp.ClientSession,
    base: str,
    *,
    pk_account: int,
    ha_url: str,
    ha_token: str,
    name: str,
    property_id: str | None,
    new_property: dict[str, str] | None,
) -> dict[str, Any]:
    """Call registerHomeAssistantHub; return its ``data`` (serial, property).

    Registering the same URL again resolves to the same hub, so a reconnect
    updates the token in place instead of adding a second hub.
    """
    payload: dict[str, Any] = {
        "mode": "registerHomeAssistantHub",
        "pk_account": pk_account,
        "ha_url": ha_url,
        "ha_token": ha_token,
        "controller_name": name,
    }
    if property_id is not None:
        payload["property_id"] = property_id
    if new_property is not None:
        payload["new_property"] = new_property
    # 422 is how the platform reports an incomplete address; its body says which
    # fields, so it is read rather than turned into a bare status error.
    body = await _async_post(session, f"{base}/api/", payload, accept_422=True)
    if body is None:
        raise EzloAppError("The Ezlo app sent an empty answer")
    if body.get("status") != 1:
        missing = body.get("missing")
        if isinstance(missing, list) and missing:
            raise EzloAppError(
                "The property needs: "
                + ", ".join(str(field).replace("_", " ") for field in missing)
            )
        raise EzloAppError(
            str(body.get("message") or body.get("error") or "Registration failed")
        )
    data = body.get("data")
    if not isinstance(data, dict) or not data.get("serial"):
        raise EzloAppError("The Ezlo app did not return a hub id")
    if not data.get("is_online"):
        # Expected on first setup: the tunnel starts after the entry is created.
        _LOGGER.info("Registered hub %s; the Ezlo app could not reach it yet", data["serial"])
    return data


async def _async_post(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict[str, Any],
    *,
    auth_step: bool = False,
    allow_empty: bool = False,
    accept_422: bool = False,
) -> dict[str, Any] | None:
    """POST JSON and return the decoded body; map failures to EzloAppError.

    An empty body is returned as ``None`` when ``allow_empty`` is set.
    """
    try:
        async with session.post(
            url, json=payload, headers=_HEADERS, timeout=_TIMEOUT
        ) as response:
            if response.status == 429:
                raise EzloAppError("Too many attempts. Wait a minute and try again.")
            if response.status in (401, 422) and auth_step:
                raise EzloAppAuthError("Invalid username or password")
            if response.status in (401, 403):
                raise EzloAppError(f"The Ezlo app refused the request (HTTP {response.status})")
            if response.status >= 400 and not (response.status == 422 and accept_422):
                raise EzloAppError(f"The Ezlo app returned HTTP {response.status}")
            raw = await response.text()
    except aiohttp.ClientError as err:
        raise EzloAppError(f"Could not reach the Ezlo app: {err}") from err
    except TimeoutError as err:
        raise EzloAppError("The Ezlo app did not answer in time") from err
    # A Laravel controller returning false comes back as an empty body.
    if allow_empty and raw.strip() in ("", "false"):
        return None
    try:
        body = json.loads(raw)
    except ValueError as err:
        raise EzloAppError("The Ezlo app sent an unreadable answer") from err
    if not isinstance(body, dict):
        raise EzloAppError("The Ezlo app sent an unexpected answer")
    return body
