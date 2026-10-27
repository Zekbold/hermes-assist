"""Constants for the Hermes Assist integration."""

import logging

from homeassistant.const import CONF_LLM_HASS_API, CONF_MODEL, CONF_PROMPT
from homeassistant.helpers import llm

DOMAIN = "hermes_assist"
LOGGER = logging.getLogger(__package__)

CONF_URL = "url"
CONF_TOKEN = "token"

# Default: the Hermes add-on container on the Home Assistant docker network.
# Home Assistant resolves add-on hostnames, so this survives IP changes.
DEFAULT_URL = "http://0a6523c6-hermes-agent:8787/v1"
DEFAULT_MODEL = "assist"

# Tuned for spoken Assist turns. Without the tool/date clauses a small model
# answers "what time is it" from memory and invents device state.
DEFAULT_PROMPT = (
    "You are Zek's voice assistant for Home Assistant. Your replies are spoken aloud, so keep them\n"
    "to one or two short sentences, in plain text, with no lists, markdown or emoji.\n"
    "Always use the available tools to look up live device and sensor state instead of asking the\n"
    "user to check it themselves, and act on requests directly rather than asking for confirmation.\n"
    "For the current time or date, always call the GetDateTime tool and repeat its value exactly;\n"
    "never guess a time or date from memory.\n"
    "Zek's bedroom is the second bedroom; the main bedroom belongs to his housemate Maurer.\n"
    "If something is genuinely unavailable or not exposed, say so in one sentence.\n"
    "Never invent a value you did not read from a tool."
)

RECOMMENDED_OPTIONS = {
    CONF_MODEL: DEFAULT_MODEL,
    CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
    CONF_PROMPT: DEFAULT_PROMPT,
}

# Assist needs one round trip per tool call. Two is normal (call, then answer);
# the cap stops a confused model from looping forever.
MAX_TOOL_ITERATIONS = 5

# Spoken answers, not essays. Also caps a runaway reasoning model.
MAX_TOKENS = 600

# AI Task data generation produces JSON (or a short report), not a spoken
# sentence, so it needs a bigger ceiling than an Assist turn.
MAX_TOKENS_AI_TASK = 2000

# Seconds of patience. A Venice chat turn is seconds; a full Vulcan agent turn
# (tools, files, skills) is tens of seconds, and an image can take minutes.
DATA_TIMEOUT = 240.0
IMAGE_TIMEOUT = 300.0
