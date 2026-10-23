# Venice.ai for Home Assistant Assist — build and deployment record

Built 23 October 2026 (Vulcan). Goal: Home Assistant's Assist pipeline should run as
locally and privately as possible, with the language model coming from Venice.ai
(private inference; `e2ee-` models run in a TEE) using Zek's Pro plan.

## What exists now

| Piece | Where | Purpose |
|---|---|---|
| Venice bridge | `/config/.hermes/profiles/vulcan/tools/ha-bridge/venice_bridge.py` (listening `0.0.0.0:8787`) | OpenAI-compatible endpoint. Holds the Venice key, maps model aliases, applies Venice request tuning, logs every Assist turn. |
| Bridge token | `state/assist_bridge.token` (0600, generated) | Bearer token HA must present; unauthenticated calls from other containers get 401. |
| Bridge log | `logs/venice-bridge.log` (2 MB rotate) | One line per request: alias, model, latency, tokens, errors. |
| Keepalive | cron `66b7ece4812e`, every 2 min, `scripts/venice_bridge_keepalive.sh` | Restarts the bridge within 2 minutes of it dying (add-on restarts wipe any service definition, so this is the durable supervisor). Silent when healthy. |
| HA integration | `custom_components/hermes_assist` (repo `Zekbold/hermes-assist`, commit `00f1f49`) | Conversation agent + config/options flow. Registered in HACS as a custom repository. |
| HA entity | `conversation.hermes_assist` | What a pipeline points at. |

Why a bridge rather than HA calling Venice directly: the Venice key never enters
Home Assistant (so it never lands in HA config entry storage or backups), model choice
is changed in one place, and the Venice-specific tuning is applied server-side
(`include_venice_system_prompt: false` removes the ~1.6k-token prompt Venice injects
by default; `disable_thinking` stops a reasoning model burning seconds before answering).

## Model results (measured from this box, tool-calling payload)

| alias | Venice model | latency/turn | tool calls | notes |
|---|---|---|---|---|
| `assist` | `e2ee-qwen3-8-27b` | 0.6–1.5 s | correct | default; best voice latency |
| `assist-smart` | `e2ee-glm-5-3-flash` | 2–6 s | correct | one 45 s stall seen in 12 probes |
| `assist-heretic` | `olafangensan-glm-4.7-flash-heretic` | ~1 s | correct | uncensored, tool-capable |
| `assist-uncensored` | `e2ee-gemma-4-26b-a4b-uncensored-p` | ~1 s | none (model rejects `tools`) | bridge retries without tools, so it degrades to chat instead of failing |

Rejected: `e2ee-gpt-oss-120b-p`, `e2ee-gemma-4-26b-a4b-uncensored-p` (no function
calling), `e2ee-kimi-k2-6` (12–14 s), `e2ee-deepseek-v4-flash` (4.4 s).

## Evidence (test instance, real HA 2026.10.2 + this component)

A throwaway HA instance (own config dir, port 8127, demo lights) was used to verify
before touching production:

- config flow → entry created against the bridge (token accepted).
- `conversation.process` "What is the capital of France?" → `Paris`, 0.7 s.
- "Turn on the bed light." → `light.bed_light` went `off` → `on`, reply "The bed light is on." (2.7 s).
- follow-up "Now turn it off again." in the same conversation → light off (context preserved).
- "Turn on the kitchen lights." with nothing exposed → model called `intent__HassTurnOn`,
  HA returned `MatchFailedError`, model explained the limitation instead of pretending.
- options flow: switching to `assist-smart` reloaded the entry and the next turn ran on
  `e2ee-glm-5-3-flash`.
- debug log confirms HA's own Assist tools reach the model (`intent__HassTurnOn`,
  `homeassistant__GetLiveContext`, `llm__GetDateTime`, `assist_satellite__HassBroadcast`).

## State at hand-over

- Component `00f1f49` is already on disk in production `/config/custom_components/hermes_assist`
  (installed through HACS). Production HA has **not** been restarted yet, so the integration
  is not loaded and no config entry exists.
- Remaining steps are automated by `finish_setup.py` (idempotent): waits for HA, creates the
  config entry (`http://0a6523c6-hermes-agent:8787/v1`, IP fallback), applies options,
  creates the pipeline **"Venice (Hermes)"** cloned from the Gemini pipeline's STT/TTS/wake
  settings, then runs one live question.
- `finish_setup.py --set-preferred` also makes that pipeline the default; without the flag the
  existing pipelines stay the default and can be switched in Settings → Voice assistants.

## Operations

- Check the bridge: `curl -s http://127.0.0.1:8787/health`
- Watch Assist turns: `tail -f /config/.hermes/profiles/vulcan/logs/venice-bridge.log`
- Rotate the token: delete `state/assist_bridge.token`, restart the bridge, update the
  integration's entry (reconfigure URL/token via the flow).
- Roll back: HACS → Hermes Assist → Remove (or delete `custom_components/hermes_assist`),
  restart HA; the Venice bridge is independent and harmless.
- Revert Assist to a cloud agent: Settings → Voice assistants → set the pipeline's
  conversation agent back to Gemini/OpenRouter/Home Assistant.
