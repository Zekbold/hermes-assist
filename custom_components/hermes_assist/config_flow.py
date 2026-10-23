"""Config flow for Hermes Assist."""

from __future__ import annotations

import logging
from typing import Any, override

import openai
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_LLM_HASS_API, CONF_MODEL, CONF_PROMPT
from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import _client
from .const import (
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_MODEL,
    DEFAULT_URL,
    DOMAIN,
    RECOMMENDED_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)


async def _async_list_models(hass, url: str, token: str) -> dict[str, str]:
    """Return {alias: label} from the bridge, raising on failure."""
    client = _client(hass, url, token)
    models = await client.with_options(timeout=10.0).models.list()
    return {
        model.id: getattr(model, "name", None) or model.id for model in models.data
    }


class HermesAssistConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hermes Assist."""

    VERSION = 1

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the bridge URL and token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._async_abort_entries_match({CONF_URL: user_input[CONF_URL]})
            try:
                await _async_list_models(
                    self.hass, user_input[CONF_URL], user_input.get(CONF_TOKEN, "")
                )
            except openai.AuthenticationError:
                errors["base"] = "invalid_auth"
            except (openai.OpenAIError, OSError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title="Hermes Assist",
                    data=user_input,
                    options=RECOMMENDED_OPTIONS,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=DEFAULT_URL): str,
                    vol.Optional(CONF_TOKEN, default=""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return HermesAssistOptionsFlow()


class HermesAssistOptionsFlow(OptionsFlow):
    """Configure the conversation agent."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage model, prompt and exposed tools."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get(CONF_LLM_HASS_API) is None:
                user_input.pop(CONF_LLM_HASS_API, None)
            return self.async_create_entry(data=user_input)

        entry = self.config_entry
        options = {**RECOMMENDED_OPTIONS, **entry.options}

        try:
            models = await _async_list_models(
                self.hass, entry.data[CONF_URL], entry.data.get(CONF_TOKEN, "")
            )
        except Exception:  # noqa: BLE001 - show the form anyway, with a fixed list
            _LOGGER.warning("Could not list models from the Hermes bridge")
            models = {DEFAULT_MODEL: "assist (bridge offline)"}

        model_options = [
            SelectOptionDict(value=model_id, label=label)
            for model_id, label in sorted(models.items())
        ]

        hass_apis: list[SelectOptionDict] = [
            SelectOptionDict(label=api.name, value=api.id)
            for api in llm.async_get_apis(self.hass)
        ]

        if suggested := options.get(CONF_LLM_HASS_API):
            valid = {api["value"] for api in hass_apis}
            options[CONF_LLM_HASS_API] = [api for api in suggested if api in valid]

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MODEL, default=options.get(CONF_MODEL, DEFAULT_MODEL)
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=model_options, mode=SelectSelectorMode.DROPDOWN
                        )
                    ),
                    vol.Optional(
                        CONF_PROMPT, default=options.get(CONF_PROMPT, "")
                    ): TemplateSelector(),
                    vol.Optional(
                        CONF_LLM_HASS_API,
                        default=options.get(CONF_LLM_HASS_API, []),
                    ): SelectSelector(
                        SelectSelectorConfig(options=hass_apis, multiple=True)
                    ),
                }
            ),
            errors=errors,
        )
