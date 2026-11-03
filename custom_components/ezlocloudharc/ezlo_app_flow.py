"""Flow steps shared by the config and options flows to connect the Ezlo app.

After the user allows the Ezlo app, they pick which of their properties this
Home Assistant joins, or create one. The address of a new property is asked
for here, prefilled with what Home Assistant already knows (its name, country
and time zone); the platform says which fields it needs and which it can keep.

Each flow sets the credentials and remote URL, then calls
``_async_ezlo_app_choose``. It provides four hooks: where to register, the
previous connection, what to do once connected, and where to go back to when
the connection cannot continue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    CountrySelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .exceptions import EzloAppAuthError, EzloError
from .ezlo_app import (
    NEW_PROPERTY_FIELDS,
    PropertyChoices,
    PropertyOption,
    async_connect,
    async_list_properties,
)
from .models import EzloAppData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Picker values that are not property ids.
NEW_PROPERTY = "__new__"
SKIP = "__skip__"

BAD_CREDENTIALS = "The Ezlo app did not accept your username and password."


class EzloAppFlowMixin:
    """Property picker and new-property form for connecting the Ezlo app."""

    hass: HomeAssistant
    _ezlo_credentials: tuple[str, str] | None = None
    _ezlo_ha_url = ""
    _ezlo_choices: PropertyChoices | None = None
    _ezlo_error = ""

    # ── Hooks ────────────────────────────────────────────────────

    def _ezlo_app_property_uri(self) -> str:
        """Return the Ezlo app (property platform) URI to register with."""
        raise NotImplementedError

    def _ezlo_app_previous(self) -> EzloAppData | None:
        """Return the existing connection, so its "Ezlo" user is reused."""
        raise NotImplementedError

    async def _async_ezlo_app_connected(self, ezlo_app: EzloAppData) -> ConfigFlowResult:
        """Finish the flow once the hub is registered."""
        raise NotImplementedError

    async def _async_ezlo_app_back(self, error: str) -> ConfigFlowResult:
        """Return to the step that asked about the Ezlo app, showing ``error``."""
        raise NotImplementedError

    def _ezlo_app_can_skip(self) -> bool:
        """Whether the picker offers to skip the Ezlo app.

        Closing the dialog during setup loses the whole setup, so the config
        flow offers a way out; the options flow can simply be closed.
        """
        return False

    async def _async_ezlo_app_skip(self) -> ConfigFlowResult:
        """Carry on without the Ezlo app (only reached when skipping is offered)."""
        raise NotImplementedError

    # ── Steps ────────────────────────────────────────────────────

    async def _async_ezlo_app_choose(self) -> ConfigFlowResult:
        """Read the user's properties, then pick one, create one, or register."""
        assert self._ezlo_credentials is not None
        username, password = self._ezlo_credentials
        self._ezlo_error = ""
        try:
            choices = await async_list_properties(
                self.hass,
                username=username,
                password=password,
                ha_url=self._ezlo_ha_url,
                property_uri=self._ezlo_app_property_uri(),
            )
        except EzloAppAuthError:
            return await self._async_ezlo_app_back(BAD_CREDENTIALS)
        except EzloError as err:
            return await self._async_ezlo_app_back(f"Could not load your properties: {err}")

        if choices is None:
            # A platform without the picker: the hub joins the user's account.
            result = await self._async_ezlo_app_register()
            return result or await self._async_ezlo_app_back(self._ezlo_error)

        self._ezlo_choices = choices
        if choices.properties:
            return await self.async_step_ezlo_app_property()
        if choices.can_create:
            return await self.async_step_ezlo_app_new_property()
        return await self._async_ezlo_app_back(
            "There is no property to add this Home Assistant to. "
            "Create one in the Ezlo app first."
        )

    async def async_step_ezlo_app_property(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the property this Home Assistant joins."""
        assert self._ezlo_choices is not None
        choices = self._ezlo_choices
        if user_input is not None:
            picked = user_input["property"]
            if picked == NEW_PROPERTY:
                self._ezlo_error = ""
                return await self.async_step_ezlo_app_new_property()
            if picked == SKIP and self._ezlo_app_can_skip():
                self._ezlo_credentials = None
                return await self._async_ezlo_app_skip()
            if result := await self._async_ezlo_app_register(property_id=picked):
                return result

        options = [
            SelectOptionDict(value=option.id, label=_property_label(option))
            for option in choices.properties
        ]
        if choices.can_create:
            options.append(SelectOptionDict(value=NEW_PROPERTY, label="Create a new property"))
        if self._ezlo_app_can_skip():
            options.append(SelectOptionDict(value=SKIP, label="Don't connect the Ezlo app now"))
        default = next(
            (option.id for option in choices.properties if option.has_hub),
            choices.properties[0].id,
        )
        return self.async_show_form(  # type: ignore[attr-defined, no-any-return]
            step_id="ezlo_app_property",
            data_schema=vol.Schema(
                {
                    vol.Required("property", default=default): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
            description_placeholders={"error_detail": self._ezlo_error},
        )

    async def async_step_ezlo_app_new_property(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a property for this Home Assistant; asks for its address."""
        choices = self._ezlo_choices
        required = choices.required_fields if choices else frozenset({"property_name"})
        allowed = required | (choices.optional_fields if choices else frozenset())
        fields = [field for field in NEW_PROPERTY_FIELDS if field in allowed]

        if user_input is not None:
            new_property = {
                field: str(user_input[field]).strip()
                for field in fields
                if str(user_input.get(field) or "").strip()
            }
            if result := await self._async_ezlo_app_register(new_property=new_property):
                return result
            suggested = user_input
        else:
            suggested = self._ezlo_app_prefill()

        schema: dict[Any, Any] = {}
        for field in fields:
            marker = vol.Required if field in required else vol.Optional
            key = marker(field, description={"suggested_value": suggested.get(field)})
            schema[key] = CountrySelector() if field == "country" else str
        return self.async_show_form(  # type: ignore[attr-defined, no-any-return]
            step_id="ezlo_app_new_property",
            data_schema=vol.Schema(schema),
            description_placeholders={"error_detail": self._ezlo_error},
        )

    # ── Helpers ──────────────────────────────────────────────────

    def _ezlo_app_prefill(self) -> dict[str, str]:
        """What Home Assistant already knows about where it is."""
        config = self.hass.config
        prefill = {
            "property_name": config.location_name or "",
            "country": config.country or "",
            "time_zone": config.time_zone or "",
        }
        return {field: value for field, value in prefill.items() if value}

    async def _async_ezlo_app_register(
        self, *, property_id: str | None = None, new_property: dict[str, str] | None = None
    ) -> ConfigFlowResult | None:
        """Register the hub; ``None`` (with ``_ezlo_error`` set) when it failed."""
        assert self._ezlo_credentials is not None
        username, password = self._ezlo_credentials
        try:
            ezlo_app = await async_connect(
                self.hass,
                username=username,
                password=password,
                ha_url=self._ezlo_ha_url,
                property_uri=self._ezlo_app_property_uri(),
                previous=self._ezlo_app_previous(),
                property_id=property_id,
                new_property=new_property,
            )
        except EzloAppAuthError:
            self._ezlo_error = BAD_CREDENTIALS
            return None
        except EzloError as err:
            self._ezlo_error = f"Could not connect the Ezlo app: {err}"
            return None
        self._ezlo_credentials = None
        self._ezlo_error = ""
        return await self._async_ezlo_app_connected(ezlo_app)


def _property_label(option: PropertyOption) -> str:
    """Name, address, and whether this Home Assistant is already there."""
    label = f"{option.name} — {option.address}" if option.address else option.name
    return f"{label} (already added)" if option.has_hub else label
