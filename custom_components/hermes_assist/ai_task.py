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
from homeassistant.helpers.json import json_dumps
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


def _read_image_base64(path: Any) -> str:
    """Encode an attachment image off the event loop."""
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _as_noul_answer(answer: dict[str, Any], question: dict[str, Any], qid: str) -> Any:
    """Coerce the model's reply into a NoulAnswer."""
    value = answer.get("noul", answer.get("value", answer.get("score")))
    try:
        probability = float(value)
    except (TypeError, ValueError) as err:
        raise HomeAssistantError(
            f"Hermes answer for '{qid}' is not a probability: {value!r}"
        ) from err
    probability = min(1.0, max(0.0, probability))
    return ai_task.NoulAnswer(noul=probability)


def _as_choice_answer(answer: dict[str, Any], question: dict[str, Any], qid: str) -> Any:
    """Coerce the model's reply into a ChoiceAnswer."""
    options = list(question["criteria"])
    chosen = answer.get("choice", answer.get("value"))
    if chosen not in options:
        matches = [opt for opt in options if str(chosen).strip().lower() == opt.lower()]
        if len(matches) == 1:
            chosen = matches[0]
        else:
            raise HomeAssistantError(
                f"Hermes picked '{chosen}' for '{qid}' but options are {options}"
            )
    raw = answer.get("probabilities")
    probabilities: dict[str, float] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                probabilities[str(key)] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue
    if chosen not in probabilities:
        probabilities[chosen] = 1.0
    for opt in options:
        probabilities.setdefault(opt, 0.0)
    total = sum(probabilities.values())
    if total <= 0:
        probabilities = {opt: 1.0 / len(options) for opt in options}
    else:
        probabilities = {key: value / total for key, value in probabilities.items()}
    return ai_task.ChoiceAnswer(choice=chosen, probabilities=probabilities)


def _as_score_answer(answer: dict[str, Any], question: dict[str, Any], qid: str) -> Any:
    """Coerce the model's reply into a ScoreAnswer."""
    criteria = list(question["criteria"])
    top = len(criteria) - 1
    value = answer.get("score", answer.get("index", answer.get("value")))
    try:
        score = float(value)
    except (TypeError, ValueError) as err:
        # Accept a named level too.
        try:
            score = float(criteria.index(str(value).strip()))
        except (ValueError, AttributeError) as inner:
            raise HomeAssistantError(
                f"Hermes answer for '{qid}' is not a level index: {value!r}"
            ) from inner
    score = min(float(top), max(0.0, score))
    raw = answer.get("probabilities")
    probabilities: list[float] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                probabilities.append(max(0.0, min(1.0, float(item))))
            except (TypeError, ValueError):
                continue
    while len(probabilities) < len(criteria):
        probabilities.append(0.0)
    probabilities = probabilities[: len(criteria)]
    if sum(probabilities) <= 0:
        index = int(round(score))
        probabilities = [1.0 if i == index else 0.0 for i in range(len(criteria))]
    else:
        total = sum(probabilities)
        probabilities = [value / total for value in probabilities]
    return ai_task.ScoreAnswer(score=score, probabilities=probabilities)


class HermesAssistAITaskEntity(ai_task.AITaskEntity, HermesAssistEntity):
    """Generate data and images through a Hermes bridge."""

    _attr_supported_features = (
        ai_task.AITaskEntityFeature.GENERATE_DATA
        | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        | ai_task.AITaskEntityFeature.GENERATE_IMAGE
        | ai_task.AITaskEntityFeature.EVALUATE
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

    @override
    async def _async_evaluate(
        self, task: ai_task.EvaluationTask
    ) -> ai_task.EvaluationTaskResult:
        """Answer decision questions with one structured chat completion.

        Mirrors the shape of core's Decisions API without needing it: noul
        questions return a probability, choice questions a selected value with
        a distribution, score questions a level index with a distribution.
        """
        if not task.questions:
            raise HomeAssistantError("Evaluation task has no questions")

        parts: list[dict[str, Any]] = []
        state_text = task.state if isinstance(task.state, str) else json_dumps(task.state)
        if state_text:
            parts.append({"type": "text", "text": f"State to evaluate:\n{state_text}"})
        for attachment in task.attachments or []:
            mime_type = attachment.mime_type.partition(";")[0].strip().lower()
            if mime_type == "image/jpg":
                mime_type = "image/jpeg"
            if mime_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                raise HomeAssistantError(
                    "Evaluations only support JPEG, PNG, WebP and GIF image "
                    f"attachments; received {attachment.mime_type}"
                )
            try:
                encoded = await self.hass.async_add_executor_job(
                    _read_image_base64, attachment.path
                )
            except OSError as err:
                raise HomeAssistantError(
                    "Unable to read evaluation attachment"
                ) from err
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}}
            )

        lines = [
            "Answer every question about the supplied state and attachments.",
            "Reply with ONLY a JSON object of the form:",
            '{"answers": {"<question_id>": ...}, where each answer is:',
            '- noul question: {"noul": <probability between 0 and 1>}',
            '- choice question: {"choice": "<one option value>", "probabilities": {"<option value>": <weight>}}',
            '- score question: {"score": <level index, 0-based>, "probabilities": [<weight per level>]}',
            "Weight/probability values must be numbers between 0 and 1.",
            "Questions:",
        ]
        for qid, question in task.questions.items():
            if question["type"] == "noul":
                lines.append(f'- {qid} (noul): {question["instructions"]}')
            elif question["type"] == "choice":
                lines.append(
                    f'- {qid} (choice, pick one of {sorted(question["criteria"])}): '
                    f'{question["instructions"]}'
                )
            else:
                criteria = list(question["criteria"])
                lines.append(
                    f'- {qid} (score, levels 0..{len(criteria) - 1} where '
                    f'{criteria}): {question["instructions"]}'
                )
        parts.append({"type": "text", "text": "\n".join(lines)})

        base = self.entry.data[CONF_URL].rstrip("/")
        token = self.entry.data.get(CONF_TOKEN, "")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": parts}],
            "max_tokens": MAX_TOKENS_AI_TASK,
        }
        headers = {"Authorization": f"Bearer {token}", **AI_TASK_HEADERS}

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{base}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=DATA_TIMEOUT),
            ) as response:
                body = await response.text()
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(f"Could not reach the Hermes endpoint: {err}") from err
        if status != 200:
            LOGGER.error("Hermes evaluation returned %s: %s", status, body[:400])
            raise HomeAssistantError(f"Hermes evaluation returned {status}: {body[:200]}")

        try:
            content = loads(body)["choices"][0]["message"]["content"] or ""
        except (JSONDecodeError, KeyError, IndexError, TypeError) as err:
            LOGGER.error("Unusable evaluation response: %s", body[:400])
            raise HomeAssistantError("Hermes returned no usable evaluation") from err

        try:
            answers = _extract_json(content)["answers"]
        except (ValueError, KeyError, TypeError) as err:
            LOGGER.error("Evaluation answer is not JSON: %s", content[:400])
            raise HomeAssistantError("Hermes evaluation reply was not JSON") from err

        result: dict[str, ai_task.EvaluationAnswer] = {}
        for qid, question in task.questions.items():
            answer = answers.get(qid)
            if not isinstance(answer, dict):
                raise HomeAssistantError(f"Hermes gave no answer for question '{qid}'")
            if question["type"] == "noul":
                result[qid] = _as_noul_answer(answer, question, qid)
            elif question["type"] == "choice":
                result[qid] = _as_choice_answer(answer, question, qid)
            else:
                result[qid] = _as_score_answer(answer, question, qid)
        return ai_task.EvaluationTaskResult(answers=result)
