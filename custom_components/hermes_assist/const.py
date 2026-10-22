"""Constants for the Hermes Assist integration."""

import logging

from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT
from homeassistant.helpers import llm

DOMAIN = "hermes_assist"
LOGGER = logging.getLogger(__package__)

CONF_URL = "url"
CONF_TOKEN = "token"

# Default: the Hermes add-on container on the Home Assistant docker network.
# Home Assistant resolves add-on hostnames, so this survives IP changes.
DEFAULT_URL = "http://0a6523c6-hermes-agent:8787/v1"
DEFAULT_MODEL = "assist"

RECOMMENDED_OPTIONS = {
    CONF_MODEL: DEFAULT_MODEL,
    CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
    CONF_PROMPT: llm.DEFAULT_INSTRUCTIONS_PROMPT,
}

# Assist needs one round trip per tool call. Two is normal (call, then answer);
# the cap stops a confused model from looping forever.
MAX_TOOL_ITERATIONS = 5

# Spoken answers, not essays. Also caps a runaway reasoning model.
MAX_TOKENS = 600
