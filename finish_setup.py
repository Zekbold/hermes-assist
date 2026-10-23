#!/usr/bin/env python3
"""Finish the Hermes Assist deployment against Home Assistant.

Idempotent: safe to re-run. Does, in order:

  1. wait for HA to answer,
  2. create the hermes_assist config entry (docker-hostname URL, IP fallback),
  3. apply options (model / prompt / Assist tool API),
  4. create an Assist pipeline "Venice (Hermes)" that clones the STT/TTS/wake
     settings of an existing pipeline while pointing the conversation agent at
     the Venice bridge,
  5. run one live conversation turn as proof (read-only question).

Usage: finish_setup.py [--set-preferred] [--reference-pipeline NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

PROFILE = "/config/.hermes/profiles/vulcan"
ENV_FILE = f"{PROFILE}/.env"
TOKEN_FILE = f"{PROFILE}/state/assist_bridge.token"

DEFAULT_PROMPT = (
    "You are a voice assistant for Home Assistant.\n"
    "Answer questions about the world truthfully.\n"
    "Respond simply and to the point in plain text.\n"
)

URL_CANDIDATES = [
    "http://0a6523c6-hermes-agent:8787/v1",
    "http://172.30.33.19:8787/v1",
]


def load_env() -> dict[str, str]:
    env = {}
    for line in open(ENV_FILE):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


ENV = load_env()
# Environment overrides let the same script drive a throwaway test instance.
HASS = (os.environ.get("HASS_URL") or ENV["HASS_URL"]).rstrip("/")
HASS_TOKEN = os.environ.get("HASS_TOKEN") or ENV["HASS_TOKEN"]
BRIDGE_TOKEN = open(TOKEN_FILE).read().strip()


def api(path: str, data=None, method=None, timeout=90):
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(
        HASS + path,
        data=body,
        headers={"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        raw = err.read().decode()
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, raw[:400]


async def ws(calls: list[tuple[str, dict]]) -> list[dict]:
    import websockets

    url = HASS.replace("http", "ws") + "/api/websocket"
    results = []
    async with websockets.connect(url, max_size=50_000_000) as socket:
        await socket.recv()
        await socket.send(json.dumps({"type": "auth", "access_token": HASS_TOKEN}))
        auth = json.loads(await socket.recv())
        if auth["type"] != "auth_ok":
            raise RuntimeError(f"websocket auth failed: {auth}")
        for index, (command, payload) in enumerate(calls, start=1):
            await socket.send(json.dumps({"id": index, "type": command, **payload}))
            while True:
                message = json.loads(await socket.recv())
                if message.get("id") == index:
                    results.append(message)
                    break
    return results


def wait_for_ha(deadline=180) -> None:
    start = time.time()
    while time.time() - start < deadline:
        status, _ = api("/api/")
        if status == 200:
            return
        time.sleep(3)
    raise SystemExit("Home Assistant did not come back in time")


def find_entry():
    _, entries = api("/api/config/config_entries/entry")
    for entry in entries:
        if entry["domain"] == "hermes_assist":
            return entry
    return None


def create_entry():
    status, flow = api("/api/config/config_entries/flow", {"handler": "hermes_assist"})
    if status != 200:
        raise SystemExit(f"could not start the config flow: {status} {flow}")
    flow_id = flow["flow_id"]
    for url in URL_CANDIDATES:
        status, result = api(
            f"/api/config/config_entries/flow/{flow_id}", {"url": url, "token": BRIDGE_TOKEN}
        )
        if status == 200 and result.get("type") == "create_entry":
            print(f"created entry via {url}")
            return find_entry()
        print(f"  {url} -> {status} {json.dumps(result)[:200]}")
        if result.get("errors"):
            continue
    raise SystemExit("no bridge URL worked from Home Assistant")


def apply_options(entry, model="assist"):
    status, flow = api("/api/config/config_entries/options/flow", {"handler": entry["entry_id"]})
    if status != 200:
        print("options flow unavailable:", status, flow)
        return
    status, result = api(
        f"/api/config/config_entries/options/flow/{flow['flow_id']}",
        {"model": model, "prompt": DEFAULT_PROMPT, "llm_hass_api": ["assist"]},
    )
    print("options applied:", status, result.get("type"))


def ensure_pipeline(reference="Gemini", name="Venice (Hermes)", set_preferred=False):
    listing = asyncio.run(ws([("assist_pipeline/pipeline/list", {})]))[0]
    existing = listing["result"]["pipelines"]
    for pipeline in existing:
        if pipeline["name"] == name:
            print(f"pipeline '{name}' already exists ({pipeline['id']}), leaving it alone")
            if set_preferred:
                asyncio.run(ws([("assist_pipeline/pipeline/set_preferred", {"pipeline_id": pipeline["id"]})]))
                print("set as preferred pipeline")
            return pipeline

    template = next((p for p in existing if p["name"] == reference), existing[0])
    payload = {
        "name": name,
        "language": template.get("language", "en"),
        "conversation_engine": "conversation.hermes_assist",
        "conversation_language": template.get("conversation_language", "*"),
        "stt_engine": template.get("stt_engine"),
        "stt_language": template.get("stt_language"),
        "tts_engine": template.get("tts_engine"),
        "tts_language": template.get("tts_language"),
        "tts_voice": template.get("tts_voice"),
        "wake_word_entity": template.get("wake_word_entity"),
        "wake_word_id": template.get("wake_word_id"),
        "prefer_local_intents": template.get("prefer_local_intents", True),
    }
    created = asyncio.run(ws([("assist_pipeline/pipeline/create", payload)]))[0]
    pipeline = created.get("result", {})
    print(f"pipeline created: {pipeline.get('name')} ({pipeline.get('id')})")
    if set_preferred and pipeline.get("id"):
        asyncio.run(ws([("assist_pipeline/pipeline/set_preferred", {"pipeline_id": pipeline["id"]})]))
        print("set as preferred pipeline")
    return pipeline


def smoke_test(text="What is the capital of France?"):
    started = time.time()
    status, result = api(
        "/api/services/conversation/process?return_response=true",
        {"text": text, "agent_id": "conversation.hermes_assist", "language": "en"},
        method="POST",
    )
    elapsed = time.time() - started
    try:
        speech = result["service_response"]["response"]["speech"]["plain"]["speech"]
    except (KeyError, TypeError):
        speech = f"<no speech; raw={json.dumps(result)[:300]}>"
    print(f"smoke test [{status}] {elapsed:.1f}s: {text!r} -> {speech!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-preferred", action="store_true")
    parser.add_argument("--reference-pipeline", default="Gemini")
    args = parser.parse_args()

    wait_for_ha()
    entry = find_entry() or create_entry()
    print("entry:", entry["entry_id"], entry["state"])
    apply_options(entry)
    ensure_pipeline(args.reference_pipeline, set_preferred=args.set_preferred)
    smoke_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
