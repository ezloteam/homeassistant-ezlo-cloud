"""Config flow for Ezlo HA Cloud."""

import logging

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback

from .const import DOMAIN
from .options_flow import EzloOptionsFlowHandler

_LOGGER = logging.getLogger(__name__)


class EzloHACloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Ezlo HA Cloud."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Create the config entry immediately; login happens in the options flow."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Ezlo HA Cloud", data={})

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EzloOptionsFlowHandler:
        """Enable the 'Configure' and 'Login' buttons in the UI."""
        return EzloOptionsFlowHandler(config_entry)
