"""Tests for the Ezlo HA Cloud config flow."""

from homeassistant import config_entries
from homeassistant.components.ezlohacloud.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType


async def test_user_flow_creates_entry_immediately(hass: HomeAssistant) -> None:
    """Picking the integration creates the entry — no extra confirm screen."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ezlo HA Cloud"
    assert result["data"] == {}


async def test_single_instance_only(hass: HomeAssistant) -> None:
    """A second config flow aborts because only one instance is allowed."""
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
