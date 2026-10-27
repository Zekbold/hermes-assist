"""AI Task support for Hermes Assist.

Home Assistant's AI Task integration asks an agent to *generate* something
instead of conversing: structured data for a template, automation or dashboard,
or an image for a notification. Both use the same Hermes bridge as Assist, just
with different endpoints:

* **data**  — ``POST <base>/chat/completions``. The model is whatever the entry
  is configured with, so the entry pointed at the Vulcan bridge runs the task in
  the full Hermes agent (its own tools, files and skills), while the Venice
  entry runs it on the selected chat model.
* **image** — ``POST <base>/images/generations``, served by the bridge itself
  with Venice and restricted to quota-included models, so an automation can
  never bill a per-image model by accident.

The generated image is returned to Home Assistant as raw bytes and written into
`media-source://ai_task/image`, exactly like a core integration would.
"""

from __future__ import annotations

import asyncio
import base64
from json import JSONDecodeError, loads
import re
from typing import Any, override

import aiohttp
import openai

from homeassistant.components import ai_task, conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HermesAssistConfigEntry
from .const import (
    CONF_TOKEN,
    CONF_URL,
    DATA_TIMEOUT,
    DOMAIN,
    IMAGE_TIMEOUT,
    LOGGER,
    MAX_TOKENS_AI_TASK,
)
from .entity import HermesAssistEntity

# Sent so the Vulcan bridge knows this is a machine instruction and must never
# treat words like "send to" as a request to hand the thread to Telegram.
AI_TASK_HEADERS = {"X-Hermes-Surface": "ai-task"}

# Fences around JSON that chat models like to add even when told not to.
_CODE_FENCE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)
# First JSON value in a reply that also contains prose.
_FIRST_JSON = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: HermesAssistConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the AI Task entity for a Hermes bridge."""
    async_add_entities([HermesAssistAITaskEntity(config_entry)])


def _extract_json(text: str) -> Any:
    """Parse the model's answer as JSON, tolerating fences and preamble."""
    candidate = _CODE_FENCE.sub("", text).strip()
    for attempt in (candidate, None):
        if attempt is None:
            match = _FIRST_JSON.search(candidate)
            if match is None:
                break
            attempt = match.group(1)
        try:
            return loads(attempt)
        except JSONDecodeError:
            continue
    raise ValueError(f"reply is not JSON: {text[:300]}")


class HermesAssistAITaskEntity(ai_task.AITaskEntity, HermesAssistEntity):
    """Generate data and images through a Hermes bridge."""

    _attr_supported_features = (
        ai_task.AITaskEntityFeature.GENERATE_DATA
        | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        | ai_task.AITaskEntityFeature.GENERATE_IMAGE
    )

    def __init__(self, entry: HermesAssistConfigEntry) -> None:
        """Initialise the entity."""
        super().__init__(entry)
        # The conversation entity already claims entry.entry_id as its unique id.
        self._attr_unique_id = f"{entry.entry_id}_ai_task"

    @property
    def name(self) -> str:
        """Name the entity after the model, so two entries are distinguishable."""
        return f"AI Task ({self.model})"

    @override
    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Answer a generate-data task."""
        try:
            await self._async_handle_chat_log(
                chat_log,
                structure=task.structure,
                max_tokens=MAX_TOKENS_AI_TASK,
                extra_headers=AI_TASK_HEADERS,
                client=self.entry.runtime_data.with_options(timeout=DATA_TIMEOUT),
            )
        except openai.OpenAIError as err:
            raise HomeAssistantError(f"Hermes bridge error: {err}") from err

        if not isinstance(chat_log.content[-1], conversation.AssistantContent):
            raise HomeAssistantError("Hermes returned no answer for this task")

        text = (chat_log.content[-1].content or "").strip()

        if not task.structure:
            return ai_task.GenDataTaskResult(
                conversation_id=chat_log.conversation_id,
                data=text,
            )

        try:
            data = _extract_json(text)
        except ValueError as err:
            LOGGER.error("Hermes returned non-JSON for '%s': %s", task.name, text[:400])
            raise HomeAssistantError(
                "Hermes did not return JSON for this structured task"
            ) from err

        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id,
            data=data,
        )

    @override
    async def _async_generate_image(
        self,
        task: ai_task.GenImageTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenImageTaskResult:
        """Generate an image through the bridge's Venice endpoint."""
        base = self.entry.data[CONF_URL].rstrip("/")
        token = self.entry.data.get(CONF_TOKEN, "")
        payload = {
            "model": "auto",  # bridge resolves this to a plan-covered model
            "prompt": task.instructions,
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
        }

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{base}/images/generations",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=IMAGE_TIMEOUT),
            ) as response:
                body = await response.text()
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"Could not reach the Hermes image endpoint: {err}"
            ) from err

        if status != 200:
            LOGGER.error("Hermes image endpoint returned %s: %s", status, body[:400])
            raise HomeAssistantError(
                f"Hermes image endpoint returned {status}: {body[:200]}"
            )

        try:
            result = loads(body)
            b64_json = result["data"][0]["b64_json"]
            image_data = base64.b64decode(b64_json)
        except (JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as err:
            LOGGER.error("Unusable image response from Hermes: %s", body[:400])
            raise HomeAssistantError("Hermes returned no usable image") from err

        # Keep the chat log well-formed for whatever consumes the result.
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=self.entity_id,
                content=result.get("data", [{}])[0].get("revised_prompt")
                or task.instructions,
            )
        )

        return ai_task.GenImageTaskResult(
            image_data=image_data,
            conversation_id=chat_log.conversation_id,
            mime_type=result.get("mime_type") or "image/png",
            model=result.get("model"),
        )
