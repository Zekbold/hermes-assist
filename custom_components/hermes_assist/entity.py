"""Base entity and chat-log plumbing for Hermes Assist.

The chat-log → chat-completions conversion (including Home Assistant's Assist
tools) follows Home Assistant's own OpenAI-style conversation integrations so
that exposed entities, areas and intents behave exactly like the built-in
agents.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator, Callable
import json
from mimetypes import guess_file_type
from pathlib import Path
from typing import Any, Literal

import openai
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionContentPartImageParam,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_message_function_tool_call_param import Function
from openai.types.shared_params import FunctionDefinition
import probatio

from homeassistant.components import conversation
from homeassistant.const import CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.json import json_dumps

from . import HermesAssistConfigEntry
from .const import DOMAIN, LOGGER, MAX_TOKENS, MAX_TOOL_ITERATIONS

# Sent on every request. Venice keeps thinking tokens out of the reply anyway;
# disabling them stops Assist burning seconds before it answers.
VENICE_PARAMETERS = {
    "disable_thinking": True,
    "strip_thinking_response": True,
}


def _format_tool(
    tool: llm.Tool,
    custom_serializer: Callable[[Any], Any] | None,
) -> ChatCompletionFunctionToolParam:
    """Format a Home Assistant tool as an OpenAI function tool."""
    unsupported_keys = {"oneOf", "anyOf", "allOf"}
    schema = probatio.to_openapi(
        tool.parameters, custom_serializer=custom_serializer, openapi_version="3.1.0"
    )
    schema = {k: v for k, v in schema.items() if k not in unsupported_keys}

    tool_spec = FunctionDefinition(name=tool.name, parameters=schema)
    if tool.description:
        tool_spec["description"] = tool.description
    return ChatCompletionFunctionToolParam(type="function", function=tool_spec)


def _convert_content_to_chat_message(
    content: conversation.Content,
) -> ChatCompletionMessageParam | None:
    """Convert a Home Assistant chat-log entry to a chat-completions message."""
    if isinstance(content, conversation.ToolResultContent):
        return ChatCompletionToolMessageParam(
            role="tool",
            tool_call_id=content.tool_call_id,
            content=json_dumps(
                {"data": content.result.data, "error": content.result.error}
            ),
        )

    role: Literal["user", "assistant", "system"] = content.role
    if role == "system" and content.content:
        return ChatCompletionSystemMessageParam(role="system", content=content.content)

    if role == "user" and content.content:
        return ChatCompletionUserMessageParam(role="user", content=content.content)

    if role == "assistant":
        param = ChatCompletionAssistantMessageParam(
            role="assistant",
            content=content.content,
        )
        if isinstance(content, conversation.AssistantContent) and content.tool_calls:
            param["tool_calls"] = [
                ChatCompletionMessageFunctionToolCallParam(
                    type="function",
                    id=tool_call.id,
                    function=Function(
                        arguments=json_dumps(tool_call.tool_args),
                        name=tool_call.tool_name,
                    ),
                )
                for tool_call in content.tool_calls
            ]
        return param
    LOGGER.warning("Could not convert message to Completions API: %s", content)
    return None


def _decode_tool_arguments(arguments: str) -> Any:
    """Decode tool call arguments."""
    try:
        return json.loads(arguments)
    except json.JSONDecodeError as err:
        raise HomeAssistantError(f"Unexpected tool argument response: {err}") from err


async def _transform_response(
    message: ChatCompletionMessage,
) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
    """Transform the API response into a chat-log delta."""
    data: conversation.AssistantContentDeltaDict = {
        "role": message.role,
        "content": message.content,
    }
    if message.tool_calls:
        data["tool_calls"] = [
            llm.ToolInput(
                id=tool_call.id,
                tool_name=tool_call.function.name,
                tool_args=_decode_tool_arguments(tool_call.function.arguments),
            )
            for tool_call in message.tool_calls
            if tool_call.type == "function"
        ]
    yield data


async def async_prepare_files_for_prompt(
    hass: HomeAssistant, files: list[tuple[Path, str | None]]
) -> list[ChatCompletionContentPartImageParam]:
    """Encode attachments for the vision-capable models."""

    def append_files_to_content() -> list[ChatCompletionContentPartImageParam]:
        content: list[ChatCompletionContentPartImageParam] = []

        for file_path, mime_type in files:
            if not file_path.exists():
                raise HomeAssistantError(f"`{file_path}` does not exist")

            if mime_type is None:
                mime_type = guess_file_type(file_path)[0]

            if not mime_type or not mime_type.startswith("image/"):
                raise HomeAssistantError(
                    "Only images are supported by the Venice bridge, "
                    f"`{file_path}` is not an image file"
                )

            base64_file = base64.b64encode(file_path.read_bytes()).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{base64_file}"},
                }
            )

        return content

    return await hass.async_add_executor_job(append_files_to_content)


class HermesAssistEntity(Entity):
    """Base entity for Hermes Assist."""

    _attr_has_entity_name = True

    def __init__(self, entry: HermesAssistConfigEntry) -> None:
        """Initialize the entity."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hermes Agent",
            model="Venice.ai bridge",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def model(self) -> str:
        """Return the model alias configured for this entry."""
        return self.entry.options.get(CONF_MODEL, "assist")

    async def _async_handle_chat_log(self, chat_log: conversation.ChatLog) -> None:
        """Generate an answer for the chat log."""
        model_args: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "extra_body": {"venice_parameters": VENICE_PARAMETERS},
        }

        tools: list[ChatCompletionFunctionToolParam] = []
        if chat_log.llm_api:
            tools.extend(
                _format_tool(tool, chat_log.llm_api.custom_serializer)
                for tool in chat_log.llm_api.tools
            )
        if tools:
            model_args["tools"] = tools

        model_args["messages"] = [
            message
            for content in chat_log.content
            if (message := _convert_content_to_chat_message(content))
        ]

        last_content = chat_log.content[-1]

        # Attachments ride on the last user message.
        if last_content.role == "user" and last_content.attachments:
            last_message: ChatCompletionMessageParam = model_args["messages"][-1]
            assert last_message["role"] == "user" and isinstance(
                last_message["content"], str
            )
            files = await async_prepare_files_for_prompt(
                self.hass,
                [(a.path, a.mime_type) for a in last_content.attachments],
            )
            last_message["content"] = [
                {"type": "text", "text": last_message["content"]},
                *files,
            ]

        client = self.entry.runtime_data

        for _iteration in range(MAX_TOOL_ITERATIONS):
            try:
                result = await client.chat.completions.create(**model_args)
            except openai.OpenAIError as err:
                LOGGER.error("Error talking to the Hermes bridge: %s", err)
                raise HomeAssistantError("Error talking to the Hermes bridge") from err

            if not result.choices:
                LOGGER.error("Hermes bridge returned empty choices")
                raise HomeAssistantError("API returned empty response")

            result_message = result.choices[0].message

            model_args["messages"].extend(
                [
                    msg
                    async for content in chat_log.async_add_delta_content_stream(
                        self.entity_id, _transform_response(result_message)
                    )
                    if (msg := _convert_content_to_chat_message(content))
                ]
            )
            if not chat_log.unresponded_tool_results:
                break
