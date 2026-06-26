"""Tests for the Ezlo HA Cloud config flow."""

from homeassistant import config_entries
from homeassistant.components.ezlohacloud.const import (
    CONF_API_URI,
    DEFAULT_API_URI,
    DOMAIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """User initiates flow, confirms, and an entry is created."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ezlo HA Cloud"
    assert result["data"] == {}


async def test_single_instance_only(hass: HomeAssistant) -> None:
    """A second config flow should abort because only one instance is allowed."""
    # Create the first entry
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(result["flow_id"], {})

    # Second attempt should abort
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ── advanced API URI override ─────────────────────────────────────────


async def test_user_flow_advanced_persists_custom_api_uri(
    hass: HomeAssistant,
) -> None:
    """With advanced options enabled, a non-default api_uri is persisted."""
    dev_api = "https://api-dev.harc.cloud"
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER, "show_advanced_options": True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    # Advanced form exposes the api_uri field
    assert CONF_API_URI in result["data_schema"].schema

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_URI: dev_api}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_API_URI: dev_api}


async def test_user_flow_advanced_default_value_is_not_persisted(
    hass: HomeAssistant,
) -> None:
    """If QA leaves the field at its default, no override is stored."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER, "show_advanced_options": True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_URI: DEFAULT_API_URI}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Default values are not persisted — keeps the entry clean for end users
    assert result["data"] == {}
