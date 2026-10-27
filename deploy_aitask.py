#!/usr/bin/env python3
"""Deploy the hermes-assist integration through HACS, then wire AI Task preferences.

Usage (inside the Hermes add-on, with its venv python):
    python3 deploy_aitask.py            # download latest master, show versions
    python3 deploy_aitask.py --prefs    # also point ai_task at the Hermes entities

Nothing here restarts Home Assistant: a new/updated custom component only loads
on restart, and restarting is Zek's call.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

PROFILE = Path("/config/.hermes/profiles/vulcan")
REPO_FULL_NAME = "zekbold/hermes-assist"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in (PROFILE / ".env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


ENV = load_env()
HASS = ENV.get("HASS_URL", "http://192.168.0.200:8123").rstrip("/")
HASS_TOKEN = ENV["HASS_TOKEN"]


async def ws(calls: list[tuple[str, dict]]) -> list[dict]:
    import websockets

    url = HASS.replace("http", "ws") + "/api/websocket"
    results: list[dict] = []
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


def api(path: str, method: str = "GET", body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        HASS + path,
        data=data,
        headers={"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, raw[:400]


def find_repo(listing: dict) -> dict | None:
    for repo in listing.get("result") or []:
        if (repo.get("full_name") or "").lower() == REPO_FULL_NAME:
            return repo
    return None


async def main() -> int:
    listing = (await ws([("hacs/repositories/list", {})]))[0]
    repo = find_repo(listing)
    if repo is None:
        print("hermes-assist is not a HACS repository on this instance")
        return 1

    print("before  installed:", repo.get("installed"), "version:", repo.get("installed_version"),
          "available:", repo.get("available_version"))

    download = (await ws([("hacs/repository/download", {"repository": repo["id"]})]))[0]
    print("download:", download.get("success"), download.get("error") or "")

    listing = (await ws([("hacs/repositories/list", {})]))[0]
    repo = find_repo(listing) or {}
    print("after   installed:", repo.get("installed"), "version:", repo.get("installed_version"),
          "available:", repo.get("available_version"))

    status, state = api("/api/states/update.hermes_assist_update")
    if status == 200 and isinstance(state, dict):
        print("update entity:", state.get("state"), state.get("attributes", {}).get("installed_version"))

    if "--prefs" not in sys.argv:
        return 0

    status, states = api("/api/states")
    if status != 200 or not isinstance(states, list):
        print("could not list states")
        return 1
    tasks = sorted((s for s in states if s["entity_id"].startswith("ai_task.hermes")),
                   key=lambda s: s["entity_id"])
    print("hermes ai_task entities:",
          [(s["entity_id"], s["attributes"].get("friendly_name")) for s in tasks])
    if not tasks:
        print("none registered yet - restart Home Assistant and re-run with --prefs")
        return 2

    # The Vulcan entry names its entity after its model option ("AI Task (vulcan)").
    by_model = [s for s in tasks
                if "vulcan" in str(s["attributes"].get("friendly_name", "")).lower()]
    target = (by_model or tasks)[-1]["entity_id"]
    result = (await ws([("ai_task/preferences/set",
                        {"gen_data_entity_id": target, "gen_image_entity_id": target})]))[0]
    print("preferences:", json.dumps(result.get("result") or result.get("error")))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
