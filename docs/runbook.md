# Runbook

Day-to-day operation of Victoria on the Mac that runs it: starting and stopping, updating, restoring a backup, adding a tool, and adding an agent. Run every command from the repository folder. Setup from nothing is in the [README](../README.md).

Draft written 2026-11-12, not yet reviewed by Bill. The commands were read from the code and the README; the ones marked *not run* below were not tried while writing this.

## The services

Each service is a LaunchAgent for the logged-in user. They start at login and restart if they exit.

| Service | LaunchAgent label | Port | What it is |
|---|---|---|---|
| Assistant | `com.victoria.voice-assistant` | 8765 (localhost) | Voice, chat, tools, satellites |
| Web interface | `com.victoria.voice-assistant-web` | 8766 | Login and the dashboard |
| Search | `com.victoria.searxng` | 8888 (localhost) | SearXNG |
| Hermes | `com.victoria.hermes` | see [hermes.md](hermes.md) | The Hermes agent's container |
| Daily job | `com.victoria.backup` | none | 03:30: a backup, then log rotation |
| Decider (optional) | written by `assistant launchd install-decider` | 8090 (localhost) | The decision model |

LM Studio is its own app and must be running with its server on, for the language models.

Logs are in `~/Library/Logs/Victoria Voice Assistant/`, one JSON object per line. The assistant's is `assistant-error.log`.

## Check that it's running

```sh
launchctl list | grep com.victoria
```

A number in the first column is a running process; `-` means not running.

```sh
curl -s http://127.0.0.1:8765/health
```

`"status": "ok"` and `"speech": "ready"` mean chat and voice can be used. Speech takes a short while after a start.

```sh
uv run assistant tools status
```

Lists every tool, and for each stub, why it is one.

## Start

The services start on their own at login. To start one that was stopped, or after first writing its LaunchAgent:

```sh
uv run assistant launchd install --load
```

```sh
uv run assistant launchd install-web --load
```

```sh
uv run assistant launchd install-searxng --load
```

```sh
uv run assistant launchd install-backup --load
```

Installing the assistant's LaunchAgent also rewrites `config/.env` from the Keychain, so it starts after a reboot with its secrets ([secrets.md](secrets.md)).

## Restart

After a code change, a certificate change, or when something is stuck:

```sh
launchctl kickstart -k gui/$(id -u)/com.victoria.voice-assistant
```

```sh
launchctl kickstart -k gui/$(id -u)/com.victoria.voice-assistant-web
```

```sh
launchctl kickstart -k gui/$(id -u)/com.victoria.searxng
```

Edits to `config/*.yaml` need no restart: they are picked up when the file is saved. An invalid edit is rejected and logged, and the previous configuration stays in use. Check an edit with `uv run assistant config check`.

A restart ends voice sessions and open chats; the dashboard reconnects, and satellites reconnect within a minute. Timers, notifications, history and chats are kept in the database and come back.

## Stop

To stop a service so that it stays stopped until the next login or the next `install --load` (*not run*):

```sh
launchctl bootout gui/$(id -u)/com.victoria.voice-assistant
```

```sh
launchctl bootout gui/$(id -u)/com.victoria.voice-assistant-web
```

`kill` isn't enough: launchd starts the service again.

## Update

1. Get the new code: `git pull`.
2. Python packages: `uv sync --extra dev`.
3. The web client, if `web/client/` changed: `(cd web/client && npm install && npm run build)`.
4. Configuration: compare `config/examples/` with your `config/` for new settings (`git diff HEAD@{1} -- config/examples`), then `uv run assistant config check`.
5. Tests: `uv run pytest -q`.
6. Restart the assistant and the web interface (above). Database changes are applied when the assistant starts.
7. Check `/health` and `uv run assistant tools status`, then try one request by chat and one by voice.

Satellite firmware is updated separately, only when `services/satellite1/` changed: `uv run assistant satellites status`, then `uv run assistant satellites update` ([satellites.md](satellites.md)).

After a Pipecat upgrade, run `uv run pytest -q tests/test_pipecat_internals.py` first. If it fails, voice is broken even though everything starts: fix `src/victoria_voice_assistant/voice/pipecat_internals.py` before going on.

## Back up and restore

The daily job writes a backup at 03:30: the database (timers, notifications, history, chats), the recipes folder beside it, and `config/` (YAML, SearXNG settings, certificates). It never holds secrets: `config/.env` and private keys stay out, and secrets come back from the Keychain.

```sh
uv run assistant backup list
```

```sh
uv run assistant backup create
```

To restore (*not run while writing this*):

1. Stop the assistant and the web interface (above).
2. `uv run assistant backup restore <name from the list>`. What it replaces is saved first as a `pre-restore` backup, so a restore can be undone by restoring that.
3. `uv run assistant secrets sync` to rewrite `config/.env` from the Keychain.
4. Start the services and check `/health`.

On a new Mac the Keychain is empty: set each secret again with `uv run assistant secrets set NAME` (`uv run assistant secrets list` shows which are missing). A recipes folder set somewhere else in `tools.yaml` (`recipes.folder`) is not in our backup.

## Add a tool

Tools are entries under `tools:` in `config/tools.yaml`. Saving the file loads them; no restart.

1. Add the entry. The model chooses tools by `name`, `description` and `params`, so write the description as when to use it. Copy a similar tool from `config/examples/tools.yaml`. The kinds:
   - `http`: fetch a URL and speak a template. No code.
   - `python`: `service: package.module:function`, your own handler.
   - `mcp` or `ha_service`: Home Assistant ([home-assistant.md](home-assistant.md)).
   - `builtin`: the services in this repository (timers, weather, search, music, reminders, calendar, recipes …).
   - `stub`: a planned tool. It says its `notice` and sends its `setup` text to the chat.

   ```yaml
   - name: sunrise
     description: Sunrise and sunset times at home today.
     kind: http
     http:
       url: https://api.sunrise-sunset.org/json
       query: { lat: "40.7", lng: "-74.0", formatted: "1" }
     reply: { template: "Sunrise is at {results[sunrise]} UTC." }
     examples:
       - when is sunrise
   ```

2. Choose how it replies: `reply: { template: true }` speaks the tool's own text, `{ template: "..." }` fills in a string, `{ silent: true }` says nothing. With none of these, the model phrases the result, which is a second model call and slower.
3. For answers that go out of date (a time, a price, a list that changes), set `forget_after_s`, so the model asks again and can't repeat an old answer.
4. Add `examples`: phrases that should reach this tool.
5. `uv run assistant config check`, then `uv run assistant tools status`. A tool that can't be resolved shows as an automatic stub with the reason.
6. `uv run assistant tools test` sends every example phrase to the router model and reports which tool it chose. Fix the description if the wrong tool wins.
7. Try it by chat, then by voice.

A secret the tool needs is never written in the YAML: store it with `uv run assistant secrets set NAME` and write `"${NAME}"` in the config.

A tool that should work on one Reminders list, like groceries, needs no code: copy the `groceries` entry and change its `name`, `description` and `list` ([reminders.md](reminders.md)).

## Add an agent

Agents are entries under `agents:` in `config/agents.yaml`, reached by saying "ask <name> …". Saving the file loads them.

1. Its model. In `config/models.yaml`, add the server under `servers:` if it's a new one, and the model under `models:`. For an agent that should pick among several models per request, add a pool under `pools:`.
2. The agent, in `config/agents.yaml`:

   ```yaml
   - name: helper
     aliases: [helpr]            # how Whisper mis-hears the name
     kind: model                 # model: a model with tools we give it; harness: runs its own tools; relay: passes the words on
     model: fast                 # or pool: <name>
     system: You answer by voice for our household. Be brief and concrete.
     tools: [weather, web_search]
     share_context: minimal
     mode: auto                  # wait: answer in the turn; auto: wait up to wait_s, then finish in the background
     wait_s: 8
   ```

3. Its key, if it has one: `uv run assistant secrets set NAME`, named in `models.yaml` as `"${NAME}"`. Without the key the agent stays a stub that says it isn't set up.
4. `uv run assistant config check`.
5. Restart the assistant if voice should recognise the new name well: the agent names are given to Whisper when a voice session starts, so an open session keeps the old list.
6. Try "ask <name> …" by chat, then by voice. If Whisper writes the name another way, add that spelling to `aliases`.

Agent-specific setup: Hermes in [hermes.md](hermes.md), Claude in [claude-setup.md](claude-setup.md). A result that arrives after `wait_s` comes as a notification ([push.md](push.md)).

## When something is wrong

| What you see | Look at |
|---|---|
| No reply by chat or voice | `/health`; is LM Studio running with its server on; `assistant-error.log` for `llm_` events |
| "<tool> isn't set up yet." | `uv run assistant tools status` gives the reason |
| Voice doesn't start in the browser | `"speech"` in `/health`; the page must be HTTPS or `localhost` ([https.md](https.md)) |
| A satellite doesn't answer | The dashboard's satellite row; `satellite_` events in the log; [satellites.md](satellites.md), Troubleshooting |
| Reminders or groceries say they can't reach Reminders | `uv run assistant reminders grant` ([reminders.md](reminders.md)) |
| "I can't reach the calendar yet." | Answer the Calendar prompt at the Mac, with full access ([calendar.md](calendar.md)) |
| A config edit had no effect | The log for a rejected reload; `uv run assistant config check` |
| Search fails | `launchctl list \| grep searxng`; restart it |
