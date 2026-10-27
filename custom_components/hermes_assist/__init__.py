"""The Hermes Assist integration: Venice.ai conversation agent for Assist.

Home Assistant talks to the Hermes add-on's Venice bridge over an
OpenAI-compatible API; the bridge holds the Venice key and picks the model.
"""

from typing import Any, cast

from openai import AsyncOpenAI, AuthenticationError, OpenAIError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.httpx_client import get_async_client

from .const import CONF_TOKEN, CONF_URL, LOGGER

PLATFORMS = [Platform.CONVERSATION, Platform.AI_TASK]

HermesAssistConfigEntry = ConfigEntry[AsyncOpenAI]


def _client(hass: HomeAssistant, url: str, token: str) -> AsyncOpenAI:
    """Build the chat client for the bridge."""
    return AsyncOpenAI(
        base_url=url.rstrip("/"),
        api_key=token or "unused",
        # Legacy HTTPX clients are supported at runtime only.
        http_client=cast(Any, get_async_client(hass)),
        timeout=60.0,
        max_retries=1,
    )


async def async_setup_entry(hass: HomeAssistant, entry: HermesAssistConfigEntry) -> bool:
    """Set up Hermes Assist from a config entry."""
    client = _client(hass, entry.data[CONF_URL], entry.data.get(CONF_TOKEN, ""))

    try:
        await client.with_options(timeout=10.0).models.list()
    except AuthenticationError as err:
        LOGGER.error("Hermes bridge rejected the token: %s", err)
        raise ConfigEntryError("Invalid bridge token") from err
    except OpenAIError as err:
        raise ConfigEntryNotReady(f"Hermes bridge unreachable: {err}") from err

    entry.runtime_data = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: HermesAssistConfigEntry
) -> None:
    """Reload when options change (model, prompt, exposed tools)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HermesAssistConfigEntry) -> bool:
    """Unload Hermes Assist."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
