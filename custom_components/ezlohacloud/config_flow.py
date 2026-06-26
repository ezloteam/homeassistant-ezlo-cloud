"""Config flow for Ezlo HA Cloud."""

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback

from .const import CONF_API_URI, DEFAULT_API_URI, DOMAIN
from .options_flow import EzloOptionsFlowHandler

_LOGGER = logging.getLogger(__name__)


class EzloHACloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Ezlo HA Cloud."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Handle the initial step — show confirmation."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if self.show_advanced_options:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema(
                    {
                        vol.Optional(CONF_API_URI, default=DEFAULT_API_URI): str,
                    }
                ),
                description_placeholders={"default_api_uri": DEFAULT_API_URI},
            )
        return self.async_show_form(step_id="confirm")

    async def async_step_confirm(self, user_input=None) -> ConfigFlowResult:
        """Handle confirm step — create the entry.

        When show_advanced_options is True the confirm form has an optional
        api_uri field; persist it (only when non-default) so QA can point at
        api-dev.harc.cloud without forking. End users never see this field.
        """
        data: dict = {}
        if user_input:
            api_uri = (user_input.get(CONF_API_URI) or "").strip()
            if api_uri and api_uri != DEFAULT_API_URI:
                data[CONF_API_URI] = api_uri
        return self.async_create_entry(title="Ezlo HA Cloud", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EzloOptionsFlowHandler:
        """Enable the 'Configure' and 'Login' buttons in the UI."""
        return EzloOptionsFlowHandler(config_entry)
