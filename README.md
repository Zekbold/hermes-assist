# Hermes Assist — Venice.ai conversation agent for Home Assistant

A custom Home Assistant conversation integration that points the **Assist pipeline** at
[Venice.ai](https://venice.ai) through the **Hermes Agent** add-on running on the same HAOS
host. Venice's models are private-inference by default, and the `e2ee-…` variants run inside
a TEE enclave with an attestation, so nothing in an Assist turn is retained or trained on.

```
Voicebox / app / kiosk
        │  STT (local Whisper, wyoming)
        ▼
Home Assistant Assist pipeline
        │  conversation.process
        ▼
hermes_assist  (this integration, /config/custom_components/hermes_assist)
        │  OpenAI-compatible chat completions + HA Assist tools
        ▼
Hermes add-on "Venice bridge"  (127.0.0.1:8787 in the add-on, docker-network reachable)
        │  holds the Venice API key, maps model aliases, tunes venice_parameters
        ▼
api.venice.ai  (private / E2EE text models)
```

## Why a bridge instead of pointing HA straight at Venice

* The Venice API key stays inside the Hermes add-on — Home Assistant's config entry and
  backups only ever contain the bridge's own random token.
* Model choice lives in one place. The bridge maps stable aliases (`assist`,
  `assist-smart`, `assist-heretic`, `assist-uncensored`) onto whatever Venice model is
  currently best, so switching models never means editing HA.
* Venice-specific request tuning is applied once, server-side: the default
  ~1.6k-token Venice system prompt is suppressed and thinking tokens are disabled, which
  is the difference between a 2-second and a 6-second answer.
* Every Assist request is logged on the Hermes side (`logs/venice-bridge.log`), which is
  where a slow or failing voice command is actually diagnosable.

## AI Task: data + image generation

The same integration now provides an **AI Task** entity (`ai_task.<device>_ai_task_<model>`),
so `ai_task.generate_data` and `ai_task.generate_image` work from automations, scripts and
the dashboard, and Home Assistant's *Settings → Devices & services → AI Task* preferences can
point at it.

| service | endpoint on the bridge | backend |
|---|---|---|
| `generate_data` | `POST <url>/chat/completions` | the entry's model — pointed at the Vulcan bridge that is the full Hermes agent, with its tools, files and skills |
| `generate_image` | `POST <url>/images/generations` | Venice, **quota-included models only** (policy in the add-on's `tools/ha-bridge/image_support.py`) |

Notes that matter:

* **Structured tasks** (`structure:` given) get the JSON schema appended to the instructions.
  The reply is parsed tolerantly (code fences and stray prose are stripped) and fails loudly
  if it is not JSON. The schema is *not* sent as `response_format` because the agent runtime
  ignores OpenAI request fields it does not use.
* **Images never cost credits.** `model: auto` resolves to a private, flat-priced model from
  the live Venice catalogue; an unknown model name falls back to the same set instead of a
  per-image billed one. The image is returned as bytes and stored by Home Assistant under
  `media-source://ai_task/image/...`, exactly like a core integration.
* AI Task requests carry `X-Hermes-Surface: ai-task`, which stops the Vulcan bridge treating
  wording like "send to" in machine instructions as a Telegram handoff request.
* `generate_image` requires `entity_id` in the service schema (core's rule, not ours);
  `generate_data` falls back to the preferred entity set in the AI Task settings.

## Install (HACS)

1. HACS → Integrations → ⋮ → Custom repositories → `https://github.com/Zekbold/hermes-assist`,
   category *Integration*.
2. Install **Hermes Assist**, then restart Home Assistant.
3. Settings → Devices & services → Add integration → **Hermes Assist**.
   * URL: `http://0a6523c6-hermes-agent:8787/v1`
   * Token: contents of `state/assist_bridge.token` in the Hermes vulcan profile.
4. Configure: pick a model, keep the **Assist** API selected (that is what lets the agent
   read state and control devices).
5. Settings → Voice assistants → pick the `Hermes Assist` conversation agent in your
   pipeline.

## Models

| alias | Venice model | notes |
|---|---|---|
| `assist` | `e2ee-qwen3-8-27b` | fastest tool-capable agent (~0.9 s/turn) |
| `assist-smart` | `e2ee-glm-5-3-flash` | better reasoning, 2–6 s/turn |
| `assist-heretic` | `olafangensan-glm-4.7-flash-heretic` | uncensored, tool-capable |
| `assist-uncensored` | `e2ee-gemma-4-26b-a4b-uncensored-p` | chat only — this model rejects `tools` |

Models that do not advertise `supportsFunctionCalling` cannot drive devices; the bridge
list only includes ones that can, except `assist-uncensored` which is marked chat-only.

## Requirements

* Home Assistant 2026.10+
* The Hermes bridge listening on `0.0.0.0:8787` inside the add-on container
* `openai` (already shipped with Home Assistant core)
