"""Ezlo HA Cloud integration options flow for Home Assistant."""

import asyncio
from datetime import datetime
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .api import (
    authenticate,
    create_stripe_session,
    decode_jwt_payload,
    get_integration_config,
    get_subscription_status,
    signup,
)
from .const import (
    DOMAIN,
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_CANCELED,
    SUBSCRIPTION_INTERNAL,
    SUBSCRIPTION_INTERNAL_TRIAL,
    SUBSCRIPTION_INVALID_STATES,
    SUBSCRIPTION_PARTNER_TRIAL,
    SUBSCRIPTION_PARTNER_TRIAL_EXPIRED,
    SUBSCRIPTION_PAST_DUE,
    SUBSCRIPTION_TRIALING,
    SUBSCRIPTION_VALID_STATES,
)
from .frp_helpers import fetch_and_update_frp_config, start_frpc, stop_frpc

_LOGGER = logging.getLogger(__name__)


def _raise_missing_uuid() -> None:
    """Raise when the JWT payload is missing a uuid claim."""
    raise ValueError("UUID missing in token payload")


def _compute_trial_days(trial_ends_at: str | None) -> int | None:
    """Compute remaining trial days from an ISO datetime string."""
    if not trial_ends_at:
        return None
    try:
        end_dt = datetime.fromisoformat(trial_ends_at.replace("Z", "+00:00"))
        now = datetime.now(tz=end_dt.tzinfo)
        remaining = (end_dt - now).days
        return max(remaining, 0)
    except (ValueError, TypeError):
        return None


class EzloOptionsFlowHandler(config_entries.OptionsFlow):
    """Handles the options flow for Ezlo HA Cloud integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        # Temporary storage for pending login during payment flow
        self._pending_token: str | None = None
        self._pending_tunnel_token: str | None = None
        self._pending_user: dict | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Register options flow steps."""
        return EzloOptionsFlowHandler(config_entry)

    def _get_abort_placeholders(self) -> dict[str, str]:
        """Build description placeholders for abort messages."""
        cloud_url = self._get_cloud_url()
        sub_status = self._config_entry.data.get("subscription_status", "")
        trial_ends_at = self._config_entry.data.get("trial_ends_at")

        if cloud_url:
            url_text = cloud_url
        else:
            url_text = "Not yet available"

        if sub_status == SUBSCRIPTION_TRIALING:
            days = _compute_trial_days(trial_ends_at)
            if days is not None:
                trial_text = f"You are on a free trial with {days} day{'s' if days != 1 else ''} remaining. Your card has been saved and will be charged when the trial ends."
            else:
                trial_text = "You are on a free trial. Your card has been saved and will be charged when the trial ends."
        elif sub_status == SUBSCRIPTION_INTERNAL_TRIAL:
            trial_text = "You are on a free trial."
        elif sub_status == SUBSCRIPTION_INTERNAL:
            trial_text = "Internal user — unlimited access. No subscription required."
        elif sub_status == SUBSCRIPTION_PARTNER_TRIAL:
            days = _compute_trial_days(trial_ends_at)
            if days is not None:
                trial_text = f"Partner trial: {days} day{'s' if days != 1 else ''} remaining. Contact your account manager before it ends."
            else:
                trial_text = (
                    "Partner trial active. Contact your account manager before it ends."
                )
        elif sub_status == SUBSCRIPTION_PARTNER_TRIAL_EXPIRED:
            trial_text = "Your partner trial has expired. Contact your account manager to restore access."
        elif sub_status == SUBSCRIPTION_ACTIVE:
            trial_text = "Your subscription is active."
        elif sub_status == SUBSCRIPTION_PAST_DUE:
            trial_text = "Your last payment failed. Please update your payment method to restore remote access."
        elif sub_status == SUBSCRIPTION_CANCELED:
            trial_text = (
                "Your subscription was canceled. Resubscribe to restore remote access."
            )
        else:
            trial_text = ""

        return {
            "cloud_url": url_text,
            "trial_info": trial_text,
        }

    def _get_cloud_url(self) -> str:
        """Build the cloud URL from frpc config data."""
        subdomain = self._config_entry.data.get("subdomain", "")
        server_name = self._config_entry.data.get("server_name", "")

        if subdomain and server_name:
            return f"https://{subdomain}.{server_name}"
        return ""

    def _get_base_url(self) -> str:
        """Get the best URL for Stripe redirect."""
        try:
            return get_url(self.hass, allow_internal=False, allow_external=True)
        except NoURLAvailableError:
            pass
        try:
            return get_url(self.hass, require_current_request=True)
        except NoURLAvailableError:
            pass
        try:
            return get_url(self.hass)
        except NoURLAvailableError:
            return "http://homeassistant.local:8123"

    # ── Main menu ────────────────────────────────────────────────

    async def async_step_init(self, user_input=None):
        """Check login status and show the correct UI."""
        config_data = self._config_entry.data
        is_logged_in = config_data.get("is_logged_in", False)
        sub_status = config_data.get("subscription_status")

        if is_logged_in:
            # Subscription invalid (past_due/canceled/incomplete) —
            # show resubscribe option prominently
            if sub_status in SUBSCRIPTION_INVALID_STATES:
                return self.async_show_menu(
                    step_id="init",
                    menu_options={
                        "subscribe": "Resubscribe",
                        "cloud_status": "Cloud Connection Status",
                        "logout": "Logout",
                    },
                )

            return self.async_show_menu(
                step_id="init",
                menu_options={
                    "cloud_status": "Cloud Connection Status",
                    "view_status": "Subscription Status",
                    "logout": "Logout",
                },
            )

        return self.async_show_menu(
            step_id="init",
            menu_options={
                "login": "Login to Ezlo Cloud",
                "signup": "Create a New Account",
            },
        )

    # ── Cloud status ─────────────────────────────────────────────

    async def async_step_cloud_status(self, user_input=None):
        """Show cloud connection status and remote URL."""
        config_data = self._config_entry.data
        user_data = config_data.get("user", {})
        username = user_data.get("username", user_data.get("name", "Unknown"))
        sub_status = config_data.get("subscription_status", "")
        trial_ends_at = config_data.get("trial_ends_at")

        # Check if frpc process is running
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id, {})
        process = entry_data.get("process")
        if process and process.poll() is None:
            connection_status = "Connected"
        else:
            connection_status = "Disconnected"

        cloud_url = self._get_cloud_url()
        if not cloud_url:
            cloud_url = "Not available"

        # Build trial info string
        if sub_status == SUBSCRIPTION_TRIALING:
            days = _compute_trial_days(trial_ends_at)
            if days is not None:
                trial_info = f"Free trial: {days} day{'s' if days != 1 else ''} remaining. Your card will be charged automatically when the trial ends."
            else:
                trial_info = "You are on a free trial. Your card will be charged automatically when the trial ends."
        elif sub_status == SUBSCRIPTION_INTERNAL_TRIAL:
            trial_info = "You are on a free trial."
        elif sub_status == SUBSCRIPTION_INTERNAL:
            trial_info = "Internal user — unlimited access. No subscription required."
        elif sub_status == SUBSCRIPTION_PARTNER_TRIAL:
            days = _compute_trial_days(trial_ends_at)
            if days is not None:
                trial_info = f"Partner trial: {days} day{'s' if days != 1 else ''} remaining. Contact your account manager before it ends."
            else:
                trial_info = (
                    "Partner trial active. Contact your account manager before it ends."
                )
        elif sub_status == SUBSCRIPTION_PARTNER_TRIAL_EXPIRED:
            trial_info = "Your partner trial has expired. Contact your account manager to restore access."
        elif sub_status == SUBSCRIPTION_PAST_DUE:
            trial_info = "Your last payment failed. Update your payment method to restore remote access."
        elif sub_status == SUBSCRIPTION_CANCELED:
            trial_info = (
                "Your subscription was canceled. Resubscribe to restore remote access."
            )
        elif sub_status == SUBSCRIPTION_ACTIVE:
            trial_info = "Subscription active."
        else:
            trial_info = ""

        return self.async_show_menu(
            step_id="cloud_status",
            menu_options={
                "init": "Back",
            },
            description_placeholders={
                "connection_status": connection_status,
                "username": username,
                "cloud_url": cloud_url,
                "trial_info": trial_info,
            },
        )

    # ── Force logout (expired token) ────────────────────────────

    async def async_step_force_logout(self, user_input=None):
        """Force logout the user and return to the main options step."""
        new_data = self._config_entry.data.copy()
        new_data.update(
            {
                "is_logged_in": False,
                "auth_token": None,
                "user": {},
                "token_expiry": 0,
            }
        )
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        return self.async_abort(reason="session_expired")

    # ── Login ────────────────────────────────────────────────────

    async def async_step_login(self, user_input=None):
        """Handle login authentication form."""
        errors = {}
        login_error_detail = ""
        if user_input is not None:
            username = user_input["username"]
            password = user_input["password"]

            system_uuid = await async_get_instance_id(self.hass) or ""
            if not system_uuid:
                system_uuid = ""
                _LOGGER.warning("Home Assistant system_uuid missing!")
            auth_response = await authenticate(
                self.hass, username, password, system_uuid
            )

            if auth_response["success"]:
                data = auth_response["data"]
                token = data["token"]
                tunnel_token = data.get("tunnel_token")
                user_info = data["user"]
                payment_required = data.get("payment_required", False)
                sub_status = data.get("subscription_status")
                trial_ends_at = data.get("trial_ends_at")
                checkout_url = data.get("checkout_url")

                if payment_required:
                    # Store pending login — user must pay first
                    self._pending_token = token
                    self._pending_tunnel_token = tunnel_token
                    self._pending_user = {
                        "uuid": user_info["uuid"],
                        "username": user_info["username"],
                        "email": user_info["email"],
                        "ezlo_id": user_info["ezlo_id"],
                    }
                    # Save user info so we can show it, but don't mark logged in
                    new_data = self._config_entry.data.copy()
                    new_data.update(
                        {
                            "auth_token": token,
                            "tunnel_token": tunnel_token,
                            "user": self._pending_user,
                            "subscription_status": sub_status,
                            "trial_ends_at": trial_ends_at,
                            "payment_required": True,
                        }
                    )
                    self.hass.config_entries.async_update_entry(
                        self._config_entry, data=new_data
                    )
                    return await self.async_step_subscribe(checkout_url=checkout_url)

                await self._handle_successful_login(
                    token,
                    {
                        "uuid": user_info["uuid"],
                        "username": user_info["username"],
                        "email": user_info["email"],
                        "ezlo_id": user_info["ezlo_id"],
                    },
                    tunnel_token=tunnel_token,
                    subscription_status=sub_status,
                    trial_ends_at=trial_ends_at,
                )
                return self.async_abort(
                    reason="login_successful",
                    description_placeholders=self._get_abort_placeholders(),
                )
            errors["base"] = "invalid_credentials"
            login_error_detail = (
                auth_response.get("error") or "Invalid username or password"
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

    # ── Logout ───────────────────────────────────────────────────

    async def async_step_logout(self, user_input=None):
        """Handle manual logout action."""
        new_data = self._config_entry.data.copy()
        new_data.update(
            {
                "is_logged_in": False,
                "auth_token": None,
                "tunnel_token": None,
                "user": {},
                "subscription_status": None,
                "trial_ends_at": None,
                "payment_required": False,
            }
        )
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        await stop_frpc(self.hass, self._config_entry)
        return self.async_abort(reason="logged_out")

    # ── Signup (trial flow — no Stripe) ──────────────────────────

    async def async_step_signup(self, user_input=None):
        """Handle signup with free trial."""
        errors = {}
        signup_error_detail = ""

        if user_input is not None:
            username = user_input["username"]
            email = user_input["email"]
            password = user_input["password"]

            system_uuid = await async_get_instance_id(self.hass) or ""
            if not system_uuid:
                system_uuid = ""
                _LOGGER.warning("Home Assistant system_uuid missing!")

            signup_response = await signup(
                self.hass, username, email, password, system_uuid
            )

            if signup_response.get("success") and "data" in signup_response:
                try:
                    resp_data = signup_response["data"]
                    token = resp_data.get("token", "")
                    tunnel_token = resp_data.get("tunnel_token")
                    checkout_url = resp_data.get("checkout_url")
                    payload = decode_jwt_payload(token)
                    user_uuid = payload.get("uuid")

                    if not user_uuid:
                        _raise_missing_uuid()

                    trial_ends_at = resp_data.get("trial_ends_at")
                    sub_status = resp_data.get("subscription_status", "")
                    payment_required = resp_data.get("payment_required", True)
                    user_info = {
                        "uuid": user_uuid,
                        "username": username,
                        "email": email,
                        "ezlo_id": payload.get("ezlo_user_id", ""),
                    }

                    # Billing parked (internal trial): backend already started a
                    # non-Stripe trial — complete login right away, no checkout.
                    if not payment_required:
                        await self._handle_successful_login(
                            token,
                            user_info,
                            tunnel_token=tunnel_token,
                            subscription_status=sub_status,
                            trial_ends_at=trial_ends_at,
                        )
                        return self.async_abort(
                            reason="signup_trial_started",
                            description_placeholders=self._get_abort_placeholders(),
                        )

                    # Stripe Checkout required — store tokens but don't
                    # mark logged in until trial starts via webhook
                    self._pending_token = token
                    self._pending_tunnel_token = tunnel_token
                    self._pending_user = user_info
                    new_data = self._config_entry.data.copy()
                    new_data.update(
                        {
                            "auth_token": token,
                            "tunnel_token": tunnel_token,
                            "user": self._pending_user,
                            "subscription_status": sub_status,
                            "trial_ends_at": trial_ends_at,
                            "payment_required": True,
                        }
                    )
                    self.hass.config_entries.async_update_entry(
                        self._config_entry, data=new_data
                    )
                    return await self.async_step_subscribe(checkout_url=checkout_url)

                except Exception:
                    _LOGGER.exception("Signup post-processing failed")
                    errors["base"] = "signup_failed"
                    signup_error_detail = "Post-processing failed. Please try again."
            else:
                errors["base"] = "signup_failed"
                signup_error_detail = signup_response.get("error") or "Unknown error"

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

    # ── Subscribe (Stripe payment) ───────────────────────────────

    async def async_step_subscribe(self, user_input=None, checkout_url=None):
        """Handle Stripe Checkout for new signup or resubscription.

        If checkout_url is supplied (from signup/login response), use it
        directly. Otherwise mint a fresh session via create-session API
        (for resubscribe of past_due/canceled).
        """
        config_data = self._config_entry.data
        user_data = config_data.get("user", {})
        user_uuid = user_data.get("uuid")
        token = self._pending_token or config_data.get("auth_token")
        tunnel_token = self._pending_tunnel_token or config_data.get("tunnel_token")

        if not user_uuid:
            return self.async_abort(reason="session_expired")

        # If no pre-supplied checkout_url, request a fresh one. Stripe price
        # id comes from the backend so we don't have to redeploy clients
        # when it changes.
        if not checkout_url:
            cfg = await get_integration_config(self.hass)
            price_id = (cfg or {}).get("stripe_price_id")
            if not price_id:
                _LOGGER.error("Could not load integration config (price_id missing)")
                return self.async_abort(reason="config_unavailable")

            back_url = (
                f"{self._get_base_url()}/config/integrations/integration/ezlohacloud"
            )
            stripe_response = await create_stripe_session(
                self.hass, user_uuid, price_id, back_url
            )
            if stripe_response.get("success"):
                checkout_url = stripe_response.get("data", {}).get("checkout_url")
            else:
                _LOGGER.error("Stripe session failed: %s", stripe_response.get("error"))
                return self.async_abort(reason="stripe_failed")

        if not checkout_url:
            return self.async_abort(reason="stripe_failed")

        # Start background polling for subscription activation
        pending_user = self._pending_user or {
            "uuid": user_uuid,
            "username": user_data.get("username", ""),
            "email": user_data.get("email", ""),
            "ezlo_id": user_data.get("ezlo_id", ""),
        }
        self.hass.async_create_task(
            self._poll_payment_and_login(
                user_uuid,
                token,
                tunnel_token,
                pending_user,
            )
        )
        return self.async_show_form(
            step_id="subscribe",
            description_placeholders={"url": checkout_url},
            data_schema=vol.Schema({}),
        )

    # ── Successful login handler ─────────────────────────────────

    async def _handle_successful_login(
        self,
        token: str,
        user_info: dict,
        tunnel_token: str | None = None,
        subscription_status: str | None = None,
        trial_ends_at: str | None = None,
    ) -> None:
        """Shared logic to handle successful login or signup."""
        new_data = self._config_entry.data.copy()
        new_data.update(
            {
                "auth_token": token,
                "tunnel_token": tunnel_token,
                "user": {
                    "uuid": user_info.get("uuid"),
                    "username": user_info.get("username"),
                    "email": user_info.get("email", ""),
                    "ezlo_id": user_info.get("ezlo_id", ""),
                },
                "is_logged_in": True,
                "subscription_status": subscription_status,
                "trial_ends_at": trial_ends_at,
                "payment_required": False,
            }
        )

        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)

        # Update the config toml and start the frpc client.
        try:
            frp_info = await fetch_and_update_frp_config(
                hass=self.hass,
                uuid=user_info["uuid"],
                token=token,
            )
            updated_data = self._config_entry.data.copy()
            updated_data["server_name"] = frp_info.get("server_name", "")
            updated_data["subdomain"] = frp_info.get("subdomain", "")
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=updated_data
            )
            await start_frpc(hass=self.hass, config_entry=self._config_entry)
        except (OSError, ValueError, RuntimeError) as err:
            _LOGGER.error("Failed to fetch the server details: %s", err)
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._config_entry.entry_id)
        )

    # ── Payment polling ──────────────────────────────────────────

    async def _poll_payment_and_login(
        self,
        user_uuid: str,
        token: str,
        tunnel_token: str | None,
        user_info: dict,
    ):
        """Background task to poll subscription status and complete login.

        Completes login once Stripe webhook has flipped the subscription
        to trialing or active.
        """
        timeout = 15 * 60  # 15 minutes
        interval = 5  # seconds
        attempts = timeout // interval
        for _ in range(attempts):
            await asyncio.sleep(interval)
            status_response = await get_subscription_status(self.hass, user_uuid)

            if not status_response.get("success"):
                continue

            sub_status = status_response.get("status", "")
            if sub_status in SUBSCRIPTION_VALID_STATES:
                _LOGGER.info("Subscription is %s. Completing login", sub_status)
                # Preserve trial_ends_at from API if present
                trial_ends = status_response.get("end_timestamp") or None
                await self._handle_successful_login(
                    token,
                    user_info,
                    tunnel_token=tunnel_token,
                    subscription_status=sub_status,
                    trial_ends_at=trial_ends,
                )
                return

        _LOGGER.warning("Polling timeout: subscription did not activate")

    # ── View subscription status ─────────────────────────────────

    async def async_step_view_status(self, user_input=None):
        """Display the subscription status."""
        config_data = self._config_entry.data
        user_data = config_data.get("user", {})
        user_uuid = user_data.get("uuid")
        sub_status = config_data.get("subscription_status", "")
        trial_ends_at = config_data.get("trial_ends_at")

        status_text = "Unknown"
        trial_info = ""

        if user_uuid:
            status_response = await get_subscription_status(self.hass, user_uuid)
            if status_response.get("success"):
                status = status_response.get("status", "unknown").capitalize()
                is_active = status_response.get("is_active")
                if is_active:
                    status_text = f"Active ({status})"
                else:
                    status_text = f"Inactive ({status})"
            else:
                status_text = f"Error: {status_response.get('error')}"

        if sub_status == SUBSCRIPTION_TRIALING:
            days = _compute_trial_days(trial_ends_at)
            if days is not None:
                trial_info = f"You are on a free trial with {days} day{'s' if days != 1 else ''} remaining. Your card was saved during checkout and will be charged when the trial ends."
            else:
                trial_info = "You are currently on a free trial. Your card will be charged when the trial ends."
        elif sub_status == SUBSCRIPTION_INTERNAL_TRIAL:
            trial_info = "You are on a free trial."
        elif sub_status == SUBSCRIPTION_PAST_DUE:
            trial_info = "Your last payment failed. Update your payment method to restore remote access."
        elif sub_status == SUBSCRIPTION_CANCELED:
            trial_info = (
                "Your subscription was canceled. Resubscribe to restore remote access."
            )

        # Show resubscribe option only for invalid subscription states
        if sub_status in SUBSCRIPTION_INVALID_STATES:
            return self.async_show_menu(
                step_id="view_status",
                menu_options={
                    "subscribe": "Resubscribe",
                    "init": "Back",
                },
                description_placeholders={
                    "status": status_text,
                    "trial_info": trial_info,
                },
            )

        return self.async_show_menu(
            step_id="view_status",
            menu_options={
                "init": "Back",
            },
            description_placeholders={
                "status": status_text,
                "trial_info": trial_info,
            },
        )

    # ── Stripe return handlers ───────────────────────────────────

    async def async_step_stripe_finish(self, user_input=None):
        """Handle return from Stripe redirect with flow_id."""
        _LOGGER.info("Stripe checkout finished, resuming flow")

        new_data = self._config_entry.data.copy()
        new_data["subscription_status"] = SUBSCRIPTION_ACTIVE
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)

    async def async_step_redirecting(self, user_input=None):
        """User returned from Stripe. Check payment status."""
        if self._config_entry.data.get("is_logged_in"):
            sub = self._config_entry.data.get("subscription_status")
            placeholders = self._get_abort_placeholders()
            if sub == SUBSCRIPTION_ACTIVE:
                return self.async_abort(
                    reason="subscription_activated",
                    description_placeholders=placeholders,
                )
            return self.async_abort(
                reason="login_successful",
                description_placeholders=placeholders,
            )

        return self.async_abort(reason="stripe_redirect_finished")
