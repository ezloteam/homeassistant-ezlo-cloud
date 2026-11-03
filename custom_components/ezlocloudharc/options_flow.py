"""Ezlo HA Cloud integration options flow for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .api import (
    AuthResult,
    SubscriptionStatusResult,
    UserDict,
    authenticate,
    get_subscription_status,
    logout,
    signup,
)
from .const import (
    CONF_API_URI,
    CONF_PROPERTY_URI,
    DEFAULT_API_URI,
    DEFAULT_PROPERTY_URI,
    SUBSCRIPTION_INVALID_STATES,
    SUBSCRIPTION_VALID_STATES,
    SubscriptionStatus,
)
from .exceptions import (
    EzloApiUnreachableError,
    EzloAuthError,
    EzloError,
)
from .ezlo_app import async_disconnect as async_disconnect_ezlo_app
from .ezlo_app_flow import BAD_CREDENTIALS, EzloAppFlowMixin
from .frp_helpers import fetch_and_update_frp_config, start_frpc, stop_frpc
from .models import EzloAppData, EzloConfigEntry, EzloRuntimeData
from .utils import clear_trial_issues, compute_trial_days

# Re-exported for callers/tests that import it from here.
__all__ = [
    "EzloOptionsFlowHandler",
    "FlowState",
    "compute_trial_days",
    "entry_state",
    "subscribe_prompt",
]

_LOGGER = logging.getLogger(__name__)

# 60-second cache for subscription status — the menu can be re-opened
# quickly and we don't want to hammer the backend.
_STATUS_CACHE_TTL = 60.0


class FlowState(StrEnum):
    """The single UI state an entry is in, shared by both flows."""

    LOGGED_OUT = "logged_out"
    SUBSCRIBED = "subscribed"
    EXPIRED = "expired"
    UNSUBSCRIBED = "unsubscribed"


def entry_state(data: Mapping[str, Any]) -> FlowState:
    """Classify a config entry's stored data into one UI state.

    Authentication and subscription are independent: a user can be logged in
    (credentials saved) yet unsubscribed. Both the options flow (cog) and the
    config flow (reconfigure) branch off this so they always agree.
    """
    if not data.get("is_logged_in") or not data.get("auth_token"):
        return FlowState.LOGGED_OUT
    status = data.get("subscription_status")
    if status in SUBSCRIPTION_VALID_STATES:
        return FlowState.SUBSCRIBED
    if status in SUBSCRIPTION_INVALID_STATES and status != SubscriptionStatus.NONE.value:
        return FlowState.EXPIRED
    return FlowState.UNSUBSCRIBED


def classify_login_error(error: str | None) -> tuple[str, str]:
    """Map a backend login-failure string to a (translation key, detail) tuple.

    ``POST /api/auth/login`` and ``/signup`` also enforce the binding between
    this Home Assistant instance and an account: a 409 whose message carries
    the ``device_already_bound`` code means the instance is linked to a
    different account. Without classification the UI would mislabel every
    failure as "Invalid username or password".
    """
    if not error:
        return "unknown", ""

    lower = error.lower()

    if "device_already_bound" in lower:
        return (
            "device_already_bound",
            "This Home Assistant installation is already linked to a"
            " different Ezlo Cloud HARC account.",
        )

    credential_markers = (
        "invalid credentials",
        "invalid username",
        "invalid password",
        "user not found",
        "unauthorized",
    )
    if any(marker in lower for marker in credential_markers):
        return "invalid_credentials", error

    return "unknown", error


def classify_signup_error(error: str | None) -> tuple[str, str]:
    """Map a backend signup-failure string to a (translation key, detail) tuple.

    Signup enforces the same instance binding as login, so a
    ``device_already_bound`` answer gets its dedicated message; anything else
    is a generic signup failure.
    """
    key, detail = classify_login_error(error)
    if key == "device_already_bound":
        return key, detail
    return "signup_failed", error or ""


async def release_backend_binding(
    hass: HomeAssistant, data: Mapping[str, Any], api_uri: str
) -> None:
    """Best-effort ``POST /api/auth/logout`` before clearing local credentials.

    Releases this HA instance's binding on the backend so another account can
    log in here afterwards. Skipped when there is no token to present.
    """
    token = data.get("auth_token")
    if not token:
        return
    if not await logout(hass, auth_token=str(token), api_uri=api_uri):
        _LOGGER.info(
            "Backend did not acknowledge logout; the instance binding may need "
            "to be released by support before another account can log in here"
        )


class EzloOptionsFlowHandler(EzloAppFlowMixin, config_entries.OptionsFlow):
    """Handles the options flow (the cog) for Ezlo HA Cloud.

    A single flat screen driven by ``entry_state``: logged-out shows login /
    create-account; subscribed shows connection details; unsubscribed/expired
    shows the subscribe/resubscribe link. Every logged-in screen offers Log out.
    """

    def __init__(self, config_entry: EzloConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry: EzloConfigEntry = config_entry
        # Set when a login/signup leads to the Ezlo app question. The password
        # (in _ezlo_credentials) only lives as long as this flow.
        self._login_reason = ""

    # ── Helpers ──────────────────────────────────────────────────

    def _runtime(self) -> EzloRuntimeData | None:
        """Return the runtime data on the bound entry, if set up."""
        runtime: EzloRuntimeData | None = getattr(
            self._config_entry, "runtime_data", None
        )
        if isinstance(runtime, EzloRuntimeData):
            return runtime
        return None

    def _get_abort_placeholders(self) -> dict[str, str]:
        """Build description placeholders for abort messages."""
        cloud_url = self._get_cloud_url()
        sub_status = self._config_entry.data.get("subscription_status", "")
        trial_ends_at = self._config_entry.data.get("trial_ends_at")

        url_text = cloud_url if cloud_url else "Not yet available"
        trial_text = _trial_text_for_status(sub_status, trial_ends_at)

        return {"cloud_url": url_text, "trial_info": trial_text}

    def _get_cloud_url(self) -> str:
        """Build the cloud URL from frpc config data."""
        subdomain = self._config_entry.data.get("subdomain", "")
        server_name = self._config_entry.data.get("server_name", "")
        if subdomain and server_name:
            return f"https://{subdomain}.{server_name}"
        return ""

    def _get_api_uri(self) -> str:
        """Return the API URI for the current config entry."""
        api_uri = self._config_entry.data.get(CONF_API_URI) or DEFAULT_API_URI
        return str(api_uri)

    def _get_property_uri(self) -> str:
        """Return the Ezlo app (property platform) URI for this entry."""
        return str(self._config_entry.data.get(CONF_PROPERTY_URI) or DEFAULT_PROPERTY_URI)

    def _ezlo_app_menu(self) -> dict[str, str]:
        """Menu entries for connecting or disconnecting the Ezlo app."""
        if self._config_entry.data.get("ezlo_app"):
            return {
                "ezlo_app": "Reconnect Ezlo app",
                "ezlo_app_disconnect": "Disconnect Ezlo app",
            }
        return {"ezlo_app": "Connect Ezlo app"}

    def _with_advanced(self, menu_options: dict[str, str]) -> dict[str, str]:
        """Append the advanced entry to a menu when advanced options are on."""
        if self.show_advanced_options:
            return {**menu_options, "advanced": "Advanced (API endpoint)"}
        return menu_options

    def _ensure_payment_poll(
        self,
        user_uuid: str,
        token: str,
        tunnel_token: str | None,
        user_info: dict[str, Any],
    ) -> None:
        """(Re)start the background poll that completes login once subscribed."""
        runtime = self._runtime()
        if runtime is not None and runtime.payment_poll_task is not None:
            runtime.payment_poll_task.cancel()
        task = self.hass.async_create_task(
            self._poll_payment_and_login(user_uuid, token, tunnel_token, user_info)
        )
        if runtime is not None:
            runtime.payment_poll_task = task

    # ── Main screen (state-driven, flat) ─────────────────────────

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Render the single state-appropriate screen."""
        data = self._config_entry.data
        state = entry_state(data)

        if state is FlowState.LOGGED_OUT:
            return self.async_show_menu(
                step_id="init",
                menu_options=self._with_advanced(
                    {"login": "Log in", "signup": "Create a new account"}
                ),
                description_placeholders={"body": "Log in or create an account."},
            )

        if state is FlowState.SUBSCRIBED:
            runtime = self._runtime()
            connected = runtime is not None and runtime.is_connected
            user_data = data.get("user", {}) or {}
            cloud_url = self._get_cloud_url() or "Not available"
            sub_status = data.get("subscription_status", "")
            status_line = _trial_text_for_status(sub_status, data.get("trial_ends_at"))
            ezlo_app = _ezlo_app_status(data.get("ezlo_app"))
            body = (
                f"**Status:** {'Connected' if connected else 'Disconnected'}\n\n"
                f"**Account:** {user_data.get('username', 'Unknown')}\n\n"
                f"**Remote URL:** [{cloud_url}]({cloud_url})\n\n"
                f"**Ezlo app:** {ezlo_app}"
            )
            if status_line:
                body += f"\n\n{status_line}"
            # On a free trial the user may subscribe early so access continues
            # seamlessly — offer the central subscribe link when the backend has one.
            if sub_status == SubscriptionStatus.TRIALING.value and user_data.get("uuid"):
                status = await self._get_cached_subscription_status(user_data["uuid"])
                url = (status.subscribe_url if status is not None else "") or data.get(
                    "subscribe_url"
                )
                if url:
                    body += f"\n\n[Subscribe now to keep remote access after your trial]({url})"
            return self.async_show_menu(
                step_id="init",
                menu_options=self._with_advanced(
                    {**self._ezlo_app_menu(), "logout": "Log out"}
                ),
                description_placeholders={"body": body},
            )

        # Partner access is admin-managed — the user cannot self-serve, so show the
        # contact-your-account-manager message with no subscribe link and no poll.
        sub_status = data.get("subscription_status")
        if sub_status == SubscriptionStatus.PARTNER_TRIAL_EXPIRED.value:
            return self.async_show_menu(
                step_id="init",
                menu_options=self._with_advanced({"logout": "Log out"}),
                description_placeholders={
                    "body": _trial_text_for_status(sub_status, data.get("trial_ends_at"))
                },
            )

        # UNSUBSCRIBED or EXPIRED (regular self-serve) — surface the subscribe/resubscribe link.
        user_data = data.get("user", {}) or {}
        user_uuid = user_data.get("uuid")
        url = ""
        if user_uuid:
            status = await self._get_cached_subscription_status(user_uuid)
            if status is not None:
                url = status.subscribe_url
            # Auto-complete login once the subscription lands.
            self._ensure_payment_poll(
                user_uuid,
                data.get("auth_token") or "",
                data.get("tunnel_token"),
                dict(user_data),
            )
        action, lead = subscribe_prompt(state, sub_status, data.get("trial_ends_at"))
        account_line = f"**Account:** {user_data.get('username', 'Unknown')}\n\n"
        if url:
            body = (
                f"{account_line}"
                f"{lead}\n\n"
                f"[{action} to Ezlo Cloud HARC]({url})\n\n"
                "This page updates automatically once your subscription is confirmed."
            )
        else:
            body = (
                f"{account_line}"
                f"{lead} The subscribe link could not be loaded right now. "
                "Please try again shortly."
            )
        return self.async_show_menu(
            step_id="init",
            menu_options=self._with_advanced({"logout": "Log out"}),
            description_placeholders={"body": body},
        )

    # ── Advanced ─────────────────────────────────────────────────

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Override the Ezlo Cloud API endpoint for this entry."""
        if not self.show_advanced_options:
            return self.async_abort(reason="advanced_disabled")

        if user_input is not None:
            new_data = dict(self._config_entry.data)
            # A field left at its default is not an override, so a later change
            # of default still reaches this entry.
            for key, default in (
                (CONF_API_URI, DEFAULT_API_URI),
                (CONF_PROPERTY_URI, DEFAULT_PROPERTY_URI),
            ):
                value = (user_input.get(key) or "").strip()
                if value and value != default:
                    new_data[key] = value
                else:
                    new_data.pop(key, None)
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return self.async_abort(reason="config_saved")

        current = self._get_api_uri()
        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_API_URI, default=current): str,
                    vol.Optional(
                        CONF_PROPERTY_URI, default=self._get_property_uri()
                    ): str,
                }
            ),
            description_placeholders={
                "current_api_uri": current,
                "default_api_uri": DEFAULT_API_URI,
                "default_property_uri": DEFAULT_PROPERTY_URI,
            },
        )

    # ── Ezlo app (HARC-20) ───────────────────────────────────────

    async def async_step_ezlo_app(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Connect (or reconnect) the Ezlo app; needs the password again."""
        if user_input is None:
            return self._async_show_password_form("")
        self._ezlo_ha_url = self._get_cloud_url()
        if not self._ezlo_ha_url:
            return self._async_show_password_form(
                "Could not connect the Ezlo app: your remote access address is not ready yet"
            )
        username = (self._config_entry.data.get("user") or {}).get("username", "")
        self._ezlo_credentials = (username, user_input["password"])
        return await self._async_ezlo_app_choose()

    def _async_show_password_form(self, error: str) -> config_entries.ConfigFlowResult:
        """Ask for the Ezlo password, showing why the last attempt failed."""
        errors: dict[str, str] = {}
        detail = ""
        if error == BAD_CREDENTIALS:
            errors["base"] = "invalid_credentials"
        elif error:
            errors["base"] = "ezlo_app_failed"
            detail = error
        return self.async_show_form(
            step_id="ezlo_app",
            data_schema=vol.Schema({vol.Required("password"): str}),
            errors=errors,
            description_placeholders={
                "username": (self._config_entry.data.get("user") or {}).get("username", ""),
                "error_detail": detail,
            },
        )

    async def async_step_ezlo_app_disconnect(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Revoke the Ezlo app's access by removing the "Ezlo" user."""
        data = dict(self._config_entry.data)
        await async_disconnect_ezlo_app(self.hass, data.pop("ezlo_app", None))
        self.hass.config_entries.async_update_entry(self._config_entry, data=data)
        return self.async_abort(reason="ezlo_app_disconnected")

    async def _async_offer_ezlo_app(
        self, username: str, password: str, reason: str
    ) -> config_entries.ConfigFlowResult:
        """After a login with access, ask about the Ezlo app before closing.

        Without a remote URL (server-config failed) there is nothing to
        register, so the flow closes as before.
        """
        if not self._get_cloud_url():
            return self.async_abort(
                reason=reason, description_placeholders=self._get_abort_placeholders()
            )
        self._ezlo_credentials = (username, password)
        self._login_reason = reason
        return await self.async_step_ezlo_app_offer()

    async def async_step_ezlo_app_offer(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask: allow Ezlo to show and control your devices?"""
        return self.async_show_menu(
            step_id="ezlo_app_offer",
            menu_options=["ezlo_app_offer_allow", "ezlo_app_offer_skip"],
            description_placeholders={
                **self._get_abort_placeholders(),
                "error_detail": self._ezlo_error,
            },
        )

    async def async_step_ezlo_app_offer_allow(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Use the credentials typed in the login/signup step."""
        self._ezlo_ha_url = self._get_cloud_url()
        return await self._async_ezlo_app_choose()

    async def async_step_ezlo_app_offer_skip(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Close with the usual login message; the Ezlo app can be connected later."""
        self._ezlo_credentials = None
        return self.async_abort(
            reason=self._login_reason,
            description_placeholders=self._get_abort_placeholders(),
        )

    # Hooks for EzloAppFlowMixin.

    def _ezlo_app_property_uri(self) -> str:
        return self._get_property_uri()

    def _ezlo_app_previous(self) -> EzloAppData | None:
        previous: EzloAppData | None = self._config_entry.data.get("ezlo_app")
        return previous

    async def _async_ezlo_app_connected(
        self, ezlo_app: EzloAppData
    ) -> config_entries.ConfigFlowResult:
        self.hass.config_entries.async_update_entry(
            self._config_entry, data={**self._config_entry.data, "ezlo_app": ezlo_app}
        )
        return self.async_abort(reason="ezlo_app_connected")

    async def _async_ezlo_app_back(self, error: str) -> config_entries.ConfigFlowResult:
        # Back to whichever question led here: the one after a login, or the
        # password form of Connect Ezlo app.
        if self._login_reason:
            self._ezlo_error = error
            return await self.async_step_ezlo_app_offer()
        return self._async_show_password_form(error)

    # ── Logout ───────────────────────────────────────────────────

    async def async_step_logout(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle manual logout action."""
        await release_backend_binding(
            self.hass, self._config_entry.data, self._get_api_uri()
        )
        await async_disconnect_ezlo_app(self.hass, self._config_entry.data.get("ezlo_app"))
        self.hass.config_entries.async_update_entry(
            self._config_entry, data=logged_out_data(self._config_entry.data)
        )
        clear_trial_issues(self.hass)
        await stop_frpc(self.hass, self._config_entry)
        return self.async_abort(reason="logged_out")

    # ── Login ────────────────────────────────────────────────────

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle login authentication form."""
        errors: dict[str, str] = {}
        login_error_detail = ""

        if user_input is not None:
            username = user_input["username"]
            password = user_input["password"]
            system_uuid = await async_get_instance_id(self.hass) or ""

            try:
                result = await authenticate(
                    self.hass,
                    username,
                    password,
                    system_uuid,
                    api_uri=self._get_api_uri(),
                )
            except EzloAuthError as err:
                error_key, login_error_detail = classify_login_error(str(err))
                errors["base"] = error_key
            except EzloApiUnreachableError as err:
                errors["base"] = "network_error"
                login_error_detail = str(err)
            except EzloError as err:
                errors["base"] = "unknown"
                login_error_detail = str(err)
            else:
                if result.payment_required:
                    self._persist_unsubscribed(result)
                    return await self.async_step_init()

                await self._handle_successful_login(result)
                return await self._async_offer_ezlo_app(
                    username, password, "login_successful"
                )

        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(
                {
                    vol.Required("username"): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
            description_placeholders={"error_detail": login_error_detail},
        )

    # ── Signup ───────────────────────────────────────────────────

    async def async_step_signup(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle account signup."""
        errors: dict[str, str] = {}
        signup_error_detail = ""

        if user_input is not None:
            username = user_input["username"]
            email = user_input["email"]
            password = user_input["password"]
            system_uuid = await async_get_instance_id(self.hass) or ""

            try:
                result = await signup(
                    self.hass,
                    username,
                    email,
                    password,
                    system_uuid,
                    api_uri=self._get_api_uri(),
                )
            except EzloAuthError as err:
                errors["base"], signup_error_detail = classify_signup_error(str(err))
            except EzloApiUnreachableError as err:
                errors["base"] = "network_error"
                signup_error_detail = str(err)
            except EzloError as err:
                errors["base"] = "signup_failed"
                signup_error_detail = str(err)
            else:
                if not result.payment_required:
                    await self._handle_successful_login(result)
                    return await self._async_offer_ezlo_app(
                        username, password, "signup_trial_started"
                    )

                self._persist_unsubscribed(result)
                return await self.async_step_init()

        return self.async_show_form(
            step_id="signup",
            data_schema=vol.Schema(
                {
                    vol.Required("username"): str,
                    vol.Required("email"): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
            description_placeholders={"error_detail": signup_error_detail},
        )

    # ── Shared entry-data writers ────────────────────────────────

    def _persist_unsubscribed(self, result: AuthResult) -> None:
        """Persist an authenticated-but-unsubscribed login on the entry.

        is_logged_in=True (credentials saved) with payment_required=True so the
        state screen shows the subscribe link, not the login menu.
        """
        new_data = dict(self._config_entry.data)
        new_data.update(
            {
                "auth_token": result.token,
                "tunnel_token": result.tunnel_token,
                "user": dict(result.user),
                "is_logged_in": True,
                "subscription_status": result.subscription_status,
                "trial_ends_at": result.trial_ends_at,
                "payment_required": True,
                "subscribe_url": result.checkout_url,
            }
        )
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)

    async def _handle_successful_login(self, result: AuthResult) -> None:
        """Shared logic to handle a subscribed login/signup — starts the tunnel."""
        new_data = dict(self._config_entry.data)
        new_data.update(
            {
                "auth_token": result.token,
                "tunnel_token": result.tunnel_token,
                "user": dict(result.user),
                "is_logged_in": True,
                "subscription_status": result.subscription_status,
                "trial_ends_at": result.trial_ends_at,
                "payment_required": False,
                "subscribe_url": result.checkout_url,
            }
        )
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)

        try:
            frp_info = await fetch_and_update_frp_config(
                hass=self.hass,
                uuid=result.user.get("uuid", ""),
                token=result.token,
                api_uri=self._get_api_uri(),
            )
        except EzloError as err:
            _LOGGER.error("Failed to fetch the server details: %s", err)
        except (OSError, ValueError, RuntimeError) as err:
            _LOGGER.error("Failed to fetch the server details: %s", err)
        else:
            updated_data = dict(self._config_entry.data)
            updated_data["server_name"] = frp_info.get("server_name", "")
            updated_data["subdomain"] = frp_info.get("subdomain", "")
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=updated_data
            )
            await start_frpc(hass=self.hass, config_entry=self._config_entry)

        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._config_entry.entry_id)
        )

    # ── Payment polling ──────────────────────────────────────────

    async def _poll_payment_and_login(
        self,
        user_uuid: str,
        token: str,
        tunnel_token: str | None,
        user_info: dict[str, Any],
    ) -> None:
        """Poll subscription status until valid, then complete login."""
        timeout = 15 * 60
        interval = 5
        attempts = timeout // interval
        for _ in range(attempts):
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                _LOGGER.debug("payment polling cancelled")
                return

            try:
                status = await get_subscription_status(
                    self.hass, user_uuid, auth_token=token, api_uri=self._get_api_uri()
                )
            except EzloAuthError as err:
                # The token itself was rejected; polling again will not change that.
                _LOGGER.warning(
                    "Subscription poll stopped: token rejected (%s). Please log in again",
                    err,
                )
                return
            except EzloError:
                continue

            if status.status in SUBSCRIPTION_VALID_STATES:
                _LOGGER.info("Subscription is %s. Completing login", status.status)
                user = UserDict(
                    uuid=user_info.get("uuid", ""),
                    username=user_info.get("username", ""),
                    email=user_info.get("email", ""),
                    ezlo_id=user_info.get("ezlo_id", ""),
                )
                synthetic = AuthResult(
                    token=token,
                    tunnel_token=tunnel_token,
                    user=user,
                    subscription_status=status.status,
                    is_trial=status.is_trial,
                    payment_required=False,
                    trial_ends_at=status.trial_ends_at or None,
                    checkout_url=status.subscribe_url or None,
                )
                await self._handle_successful_login(synthetic)
                return

        _LOGGER.warning("Polling timeout: subscription did not activate")

    async def _get_cached_subscription_status(
        self, user_uuid: str
    ) -> SubscriptionStatusResult | None:
        """Return cached subscription status, refreshing if past TTL."""
        runtime = self._runtime()
        now = time.monotonic()
        if runtime is not None and runtime.subscription_cache is not None:
            ts, cached = runtime.subscription_cache
            if now - ts < _STATUS_CACHE_TTL:
                return SubscriptionStatusResult(
                    status=str(cached.get("status", "unknown")),
                    is_active=bool(cached.get("is_active", False)),
                    is_trial=bool(cached.get("is_trial", False)),
                    trial_ends_at=str(cached.get("trial_ends_at", "")),
                    subscribe_url=str(cached.get("subscribe_url", "")),
                )

        token = self._config_entry.data.get("auth_token")
        if not token:
            return None
        try:
            result = await get_subscription_status(
                self.hass, user_uuid, auth_token=token, api_uri=self._get_api_uri()
            )
        except EzloError as err:
            _LOGGER.debug("subscription status fetch failed: %s", err)
            return None

        if runtime is not None:
            runtime.subscription_cache = (
                now,
                {
                    "status": result.status,
                    "is_active": result.is_active,
                    "is_trial": result.is_trial,
                    "trial_ends_at": result.trial_ends_at,
                    "subscribe_url": result.subscribe_url,
                },
            )
        return result


# ── Module-private helpers ────────────────────────────────────────


def logged_out_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of entry data with all auth/subscription fields cleared.

    The Ezlo app connection goes too: it was made for this account and its
    remote URL, and the next login may be a different account.
    """
    cleared = dict(data)
    cleared.pop("ezlo_app", None)
    cleared.update(
        {
            "is_logged_in": False,
            "auth_token": None,
            "tunnel_token": None,
            "user": {},
            "subscription_status": None,
            "trial_ends_at": None,
            "payment_required": False,
            "subscribe_url": None,
        }
    )
    return cleared


def _ezlo_app_status(ezlo_app: Mapping[str, Any] | None) -> str:
    """The Ezlo app line of the status screen."""
    if not ezlo_app:
        return "Not connected"
    if name := ezlo_app.get("property_name"):
        return f"Connected to {name}"
    return "Connected"


def subscribe_prompt(
    state: FlowState, sub_status: str | None, trial_ends_at: str | None
) -> tuple[str, str]:
    """Return the (link verb, lead sentence) for a self-serve unsubscribed screen.

    A lapsed free trial is a first subscription, not a renewal: the user has
    never paid, so the verb is "Subscribe" and the lead names the trial.
    """
    if sub_status == SubscriptionStatus.TRIAL_EXPIRED.value:
        return "Subscribe", _trial_text_for_status(sub_status, trial_ends_at)
    if state is FlowState.EXPIRED:
        return "Resubscribe", "You don't have an active subscription."
    return "Subscribe", "You don't have an active subscription."


def _trial_text_for_status(sub_status: str | None, trial_ends_at: str | None) -> str:
    """Render the contextual trial/subscription text for a status."""
    if sub_status == SubscriptionStatus.FEATURE_HARC.value:
        return "Your Ezlo subscription is active."
    if sub_status == SubscriptionStatus.TRIALING.value:
        days = compute_trial_days(trial_ends_at)
        if days is not None:
            return (
                f"Free trial: {days} day{'s' if days != 1 else ''} remaining. "
                "No payment details are needed during the trial — subscribe before "
                "it ends to keep remote access."
            )
        return (
            "You are on a free trial. No payment details are needed during the "
            "trial — subscribe before it ends to keep remote access."
        )
    if sub_status == SubscriptionStatus.TRIAL_EXPIRED.value:
        return "Your free trial has ended. Subscribe to restore remote access."
    if sub_status == SubscriptionStatus.INTERNAL.value:
        return "Internal user — unlimited access. No subscription required."
    if sub_status == SubscriptionStatus.PARTNER_TRIAL.value:
        days = compute_trial_days(trial_ends_at)
        if days is not None:
            return (
                f"Partner trial: {days} day{'s' if days != 1 else ''} remaining. "
                "Contact your account manager before it ends."
            )
        return "Partner trial active. Contact your account manager before it ends."
    if sub_status == SubscriptionStatus.PARTNER_TRIAL_EXPIRED.value:
        return (
            "Your partner trial has expired. "
            "Contact your account manager to restore access."
        )
    if sub_status == SubscriptionStatus.ACTIVE.value:
        return "Your subscription is active."
    if sub_status == SubscriptionStatus.PAST_DUE.value:
        return (
            "Your last payment failed. "
            "Update your payment method to restore remote access."
        )
    if sub_status == SubscriptionStatus.CANCELED.value:
        return "Your subscription was canceled. Resubscribe to restore remote access."
    if sub_status == SubscriptionStatus.NONE.value:
        return (
            "You don't have an active subscription. "
            "Subscribe to enable remote access."
        )
    return ""
