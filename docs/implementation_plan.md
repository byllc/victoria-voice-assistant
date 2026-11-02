# Home Voice Assistant: Implementation Plan (v1.4)

Owner: Bill · Updated: 2026-11-02 (v1.4: Stage 14, carried-over fixes; v1.3: Stage 13) · Companion to: *Home Voice Assistant Architecture* (`voice-pipeline-architecture.md`, currently v2.10)

This plan is written for a coding agent. The pipeline is built first and is fully usable from the web client; the ESP32 satellites come last, as an optional stage. The architecture document says **what** to build and **why**; this plan says **in what order**, **what "done" means** at each stage, and **how approval works**. When the two disagree, stop and ask.

## 1. Approval gate (read first)

Work proceeds in numbered stages. **No stage may start until the previous stage has been approved by Bill.** Approval is recorded only in the stage tracker (`docs/stages.md`), which is created in Stage 0.

Rules for the coding agent:

1. **Read `docs/stages.md` at the start of every session.** Work only on the single stage marked `in progress`. If no stage is `in progress`, the next stage may be started only if the stage before it is `approved`.
2. **One stage at a time.** Do not write code, scaffolding, stubs, or "preparation" for a later stage, even if it seems harmless. If something later is needed now, note it in the tracker and ask.
3. **When the done criteria are met:** set the stage to `ready for review`, fill in the evidence section (test output, measurements, demo steps Bill can follow), then **stop and wait**.
4. **Never mark a stage `approved`.** Only Bill changes a stage to `approved`, by editing the tracker and adding his name and the date. If Bill marks it `changes requested`, address his notes and return it to `ready for review`.
5. **Don't change the architecture silently.** If a stage shows that the architecture is wrong or incomplete, record a proposal in the tracker's decisions log and wait for Bill's answer. Do not edit the architecture document unless asked.
6. **Blocking decisions:** each stage lists the open questions it depends on. If one is unanswered when the stage begins, ask before writing code.
7. **Work on a branch per stage** (`stage-NN-short-name`). Merge to `main` only after approval, then tag `stage-NN-approved`.

Stage statuses: `not started` → `in progress` → `ready for review` → `approved` (or `changes requested` → `in progress`).

## 2. Ground rules for all stages

- **Target:** Mac mini, M4 Pro, 64 GB, macOS 26. **Development happens on an M2 Mac** with local environments and local test LLMs (LM Studio); architecture memory figures are for the target machine and M2 measurements are for design validation only. Native processes under `launchd`; no Docker. Apple `container` only for agent sandboxes (from Stage 7).
- **Python virtual environments.** Every Python environment (service, web interface, spikes) is a virtual environment managed by uv (`uv venv` + `uv sync` from `pyproject.toml` + lock file, interpreter pinned to 3.12). Never install packages into the system Python. `uv run` is the way to execute everything.
- **Python 3.12+, managed with `uv`, every dependency pinned in the lock file.** The web client uses TypeScript with Vite.
- **Pipecat built-ins first.** Use Pipecat's own feature wherever one exists (architecture §11 lists them). Replace a built-in only with measured evidence, recorded in the decisions log.
- **Secrets** are kept in the macOS Keychain and run from `config/.env` (git-ignored, never committed); config names them as `${NAME}`. Never in config files or the repo (`docs/secrets.md`).
- **Tests:** each stage adds automated tests for what it builds. Tests that need hardware or a running model are marked so they can be skipped in a quick run.
- **Latency is measured, not assumed.** From Stage 3 on, every request logs per-stage timings.
- **Config is the interface.** Behaviour that the architecture puts in `models.yaml`, `tools.yaml`, or `agents.yaml` is configured there, not hard-coded.
- **All configuration lives in `config/`**, never in the repository root:

  ```
  config/
    models.yaml        servers, models, pools, decider, roles
    tools.yaml         tools, built-in service settings, MCP servers, tests
    agents.yaml        agents and relays
    searxng/           SearXNG's settings.yml
    .env               secrets (git-ignored; never committed)
  ```

  The service reads `config/` by default; `ASSISTANT_CONFIG_DIR` overrides it. Example files committed for reference go in `config/examples/`.

## 3. Stage tracker template

Stage 0 creates `docs/stages.md` from this template.

```markdown
# Stage tracker

Rules: see implementation plan §1. Only Bill sets a stage to `approved`.

| # | Stage | Status | Started | Ready for review | Approved by / date |
|---|---|---|---|---|---|
| 0 | Tracker, instructions, repo | not started | | | |
| 1 | Defer early benchmarking | not started | | | |
| 2 | Skeleton and config | not started | | | |
| 3 | Web interface and text chat | not started | | | |
| 4 | Tool registry, timers, weather | not started | | | |
| 5 | Voice in the browser | not started | | | |
| 6 | Local web search | not started | | | |
| 7 | Agents and model pools | not started | | | |
| 8 | Notifications and task dashboard | not started | | | |
| 9 | Home control | not started | | | |
| 10 | Music | not started | | | |
| 11 | Hardening | not started | | | |
| 12 | Satellites (optional) | not started | | | |
| 13 | Satellites at household scale (optional) | not started | | | |
| 14 | Carried-over fixes and checks | not started | | | |

## Stage N: <name>
- Status:
- Done criteria: (copied from the plan, each with [ ] / [x])
- Evidence: (test output, measurements, demo steps for Bill)
- Bill's review notes:

## Decisions log
| Date | Stage | Proposal or decision | Bill's answer |
|---|---|---|---|
```

## 4. Stages

### Stage 0: Tracker, instructions, repo

**Goal:** the project can be worked on safely by a coding agent.

**Scope:**
- Create the git repository and `docs/stages.md` from the template in §3.
- Copy the architecture document and this plan into `docs/`.
- Create `AGENTS.md` (with `CLAUDE.md` pointing to it) containing: the approval gate rules from §1, the ground rules from §2, and the instruction to read `docs/stages.md` first.

**Out of scope:** any application code, dependencies, or project scaffolding.

**Done criteria:**
- [ ] `docs/stages.md` exists, matches the template, with Stage 0 `ready for review`.
- [ ] `AGENTS.md` and `CLAUDE.md` exist and state the gate rules.
- [ ] Architecture document and plan are in `docs/`.

**Blocking decisions:** none.

### Stage 1: Defer early benchmarking

**Goal:** avoid isolated spike work before the full pipeline exists; validate the integrated system at the end.

**Scope:** remove the early spike scripts, reports, and per-run results. Use `google/gemma-4-12b` as the current local test model; do not compare candidate models before end-to-end validation. Carry the known Smart Turn interruption risk forward for validation with the integrated voice pipeline.

**Out of scope:** standalone model, speech-concurrency, and turn-detection benchmarks.

**Done criteria:**
- [x] Early spike code, reports, and results are removed.
- [x] The known Smart Turn risk is recorded for later end-to-end validation.
- [x] End-to-end pipeline benchmarking is assigned to Stage 11.

**Blocking decisions:** none.

### Stage 2: Skeleton and config

**Goal:** a runnable, supervised service that loads and validates its configuration.

**Scope:** Python package layout; the `config/` folder from §2 with example files; Pydantic schemas for `config/models.yaml`, `config/tools.yaml`, and `config/agents.yaml` matching the architecture (including stubs, pools, decider, built-in service blocks); `${ENV}` secret substitution; validation errors that name the file and field; reload on save that keeps the previous config if the new one is invalid; structured logging; SQLite store with migrations; health endpoint; `launchd` plists for the assistant service; a CLI with `config init` and `config check`.

**Out of scope:** models, audio, web UI.

**Done criteria:**
- [ ] All configuration is read from `config/` (or `ASSISTANT_CONFIG_DIR`); nothing is read from the repository root.
- [ ] `assistant config check` validates the example configs and rejects broken ones with clear messages.
- [ ] Editing a config file reloads it; an invalid edit is rejected and logged.
- [ ] The service starts under `launchd` and restarts when killed.
- [ ] Unit tests pass.

**Local setup:**

```sh
uv sync --extra dev
uv run assistant config init
uv run assistant secrets set LMS_API_KEY   # Keychain and config/.env (docs/secrets.md)
uv run assistant config check
uv run assistant launchd install --load
```

The LaunchAgent listens on `127.0.0.1:8765`; unload it with
`launchctl bootout gui/$(id -u)/com.victoria.voice-assistant`.

**Blocking decisions:** none.

### Stage 3: Web interface and text chat

**Goal:** type to the assistant in a browser and get a streamed reply from the local fast model.

**Scope:** the web interface as a separate process (FastAPI on Uvicorn, `launchd`); household login; serving the example client; proxying the websocket to the assistant service. The example client's text chat view. In the assistant service: a text pipeline using Pipecat's OpenAI LLM service pointed at LM Studio, with the `router` and `router_fallback` roles (via `LLMSwitcher`); model pinning with `lms load`; per-request timing logs.

**Out of scope:** voice, tools, agents, dashboard.

**Done criteria:**
- [ ] Typed messages get streamed replies from the configured fast model.
- [ ] Stopping LM Studio's primary model triggers the fallback model.
- [ ] Killing the web process doesn't affect the assistant service, and vice versa; both restart.
- [ ] The assistant service accepts connections only from localhost.
- [ ] Time to first token is logged per request.

**Blocking decisions:** none. Use `google/gemma-4-12b` as the current local test model; assess integrated performance at Stage 11.

### Stage 4: Tool registry, timers, weather

**Goal:** the fast model resolves everyday requests with tools, asks when unsure, and is testable from text chat.

**Scope:** tool registry generating Pipecat tool schemas; tool kinds `builtin`, `python`, `http`, and `stub` (explicit and automatic); the `reply` option (model, template, silent); built-in tools `clarify` and `offer_agent` (`ask_agent` exists but returns "agents aren't set up yet" until Stage 8); pending questions with expiry and no-model yes/no matching; loop guard; `tools test` and `tools status` commands. Built-in services: **timers** (several, named, persisted, ringing in the web client for now) and **weather** (Open-Meteo, cached). `home_control` and `music` as explicit stubs.

**Out of scope:** search, agents, satellites, notifications beyond timer ringing.

**Done criteria:**
- [ ] `tools test` passes on the example phrases at an accuracy Bill agrees with, and reports failures clearly.
- [ ] Multiple named timers: start, cancel by name, add time, status of one and all; "cancel the timer" with two running asks which.
- [ ] Timers survive a service restart.
- [ ] Calling a stub speaks its notice and shows setup steps in chat; a broken tool definition becomes an automatic stub.
- [ ] Template replies skip the second model call (visible in timing logs).

**Blocking decisions:** none.

### Stage 5: Voice in the browser

**Goal:** talk to the assistant from the web client with good latency.

**Scope:** HTTPS for the web interface; SmallWebRTC voice with signaling proxied by the web interface and audio direct to the assistant service; `WhisperSTTServiceMLX`, `KokoroTTSService`, Silero VAD with Smart Turn; push-to-talk and hands-free modes; live transcripts; in-process safeguards from architecture §4.2 (inference off the event loop, per-model locks, loop-lag monitoring); a latency harness reporting each stage.

**Out of scope:** satellites, interruptions while the assistant speaks (unless Pipecat's built-in handling works without extra code).

**Done criteria:**
- [ ] Voice round trip works from a phone and a computer on the LAN.
- [ ] Latency report for 20 spoken commands, broken down by stage, compared with the 1.5 s target.
- [ ] Short commands ("stop", "pause") end their turn promptly.
- [ ] Two simultaneous voice sessions work without audio stutter.

**Blocking decisions:** HTTPS method (`mkcert` or Tailscale).

### Stage 6: Local web search

**Goal:** current-information questions answered from local search.

**Scope:** SearXNG installed natively under `launchd`, JSON output enabled, bound to localhost; `web_search` (snippets, cache) and `web_read` (`httpx` and `trafilatura`, Playwright fallback with a warm pool and blocked resources); per-tool timeouts with spoken failure messages; web content marked as untrusted when passed to models.

**Done criteria:**
- [ ] Snippet answers and page-read answers both work by voice and text.
- [ ] Measured times for each, compared with the architecture's estimates.
- [ ] SearXNG being down gives "I couldn't search right now", not a guess.

**Blocking decisions:** none.

### Stage 7: Agents and model pools

**Goal:** "Ask Hermes" and "Ask Claude" work, with cost-aware model choice and task tracking.

**Scope:** agent registry; deterministic agent-name matching with aliases and Whisper prompt biasing; `harness` agents (Hermes) and `model` agents (Claude via Pipecat's LLM services in a text-only pipeline; plain SDK loop only if needed, recorded in the decisions log); pools, the decider (`llm_choice` first), spoken overrides, escalation, budgets, and the spend log; task records and lifecycle including cancellation; handoff modes `wait`, `background`, and `auto` with a simple spoken result for now; Hermes running under its own macOS user or an Apple container.

**Out of scope:** notifications beyond a basic spoken result, the dashboard, A2A agents (unless one is ready to test).

**Done criteria:**
- [ ] "Ask Hermes …" and "Ask Claude …" work by voice and text; offering an agent works when no tool fits.
- [ ] The decider's choice, confidence, tokens, and cost are logged; "Ask Claude Opus …" overrides it.
- [ ] Budget limits downgrade and then stop paid calls as configured.
- [ ] Tasks can be listed and cancelled by voice.
- [ ] Hermes cannot read the assistant's config, keys, or personal files.

**Blocking decisions:** Hermes's default model (local or cloud); decider engine (default `llm_choice` unless Bill says otherwise).

### Stage 8: Notifications and task dashboard

**Goal:** long-running work reports back reliably, and its status is visible.

**Scope:** the notification service (priorities, ask-before-reading, grouping, fallbacks, quiet hours, do-not-disturb, expiry); pending indicator in the web client (satellite LEDs come in Stage 12); "any notifications?" and "what did Hermes say?"; agent questions with timeout; Web Push to installed web clients; the task dashboard in the example client with its actions, spend summary, and service health strip; the `notifications` and `tasks` tools.

**Done criteria:**
- [ ] A background task finishing while the web client's room is idle announces itself; while the room is busy, it waits; during quiet hours, it is silent.
- [ ] Pending notifications show a badge in the web client and are read on request.
- [ ] An agent question can be answered by voice, from the notification, or in the dashboard.
- [ ] A phone with the web client installed receives a push when the page is closed.
- [ ] The dashboard shows live status, progress, model, and cost, and supports answer, cancel, retry, re-send, and open.

**Blocking decisions:** quiet hours, and whether agent questions may wake a room at night.

### Stage 9: Home control

**Goal:** smart-home control through Home Assistant as a tool.

**Scope:** replace the `home_control` stub with an MCP tool using Home Assistant's MCP Server integration; direct `ha_service` tools only where latency measurements justify them; `voice` label scoping; confirmation for locks, garage, and alarm.

**Done criteria:**
- [ ] Common light, climate, and scene commands work by voice with template replies.
- [ ] Locks require spoken confirmation.
- [ ] Only entities labelled `voice` can be controlled.

**Blocking decisions:** none.

### Stage 10: Music

**Goal:** Spotify and Plex music by voice in the right room.

**Scope:** the music built-in using `music-assistant-client` against the configured Music Assistant server; room mapping, source order, ducking during voice turns, templated replies. The Alexa relay agent and the music stub's `then: { ask_agent: alexa }` fallback, if Bill wants the stopgap first.

**Done criteria:**
- [ ] "Play … from Plex" and "play … on Spotify" work in the room that heard the request; "in the kitchen" overrides.
- [ ] Pause, resume, skip, volume, and "what's playing" work.
- [ ] Music ducks while the assistant listens and speaks.
- [ ] An unreachable music server gives a clear spoken message.

**Blocking decisions:** which speaker plays music in each room; where Music Assistant runs.

### Stage 11: Hardening

**Goal:** the system is dependable for daily household use.

**Scope:** crash and restart testing for every service; memory monitoring; backups of SQLite and config; health checks feeding the dashboard; log rotation; a short runbook (start, stop, update, restore, add a tool, add an agent).

**Done criteria:**
- [ ] Killing any one service recovers automatically without losing timers, tasks, or notifications.
- [ ] One week of daily use with issues logged and fixed or accepted by Bill.
- [ ] An end-to-end benchmark of representative voice and text interactions reports overall success and per-stage latency.
- [ ] Runbook reviewed by Bill.

**Blocking decisions:** none.

### Stage 12: Satellites (optional)

**Goal:** the Satellite1s work as voice devices for the assistant. Optional: the system is complete and usable from the web client without this stage.

**Scope:**

*Before adapter work, perform targeted satellite compatibility checks and record findings in this stage's evidence:*
1. **Satellite takeover:** connect to one Satellite1 with `aioesphomeapi` while Home Assistant is also connected. Can our client receive voice-assistant audio? What happens to Home Assistant? Can we drive LEDs, play a reply URL, and play an unprompted announcement?
2. **Follow-up listening and "stop":** can we reopen the mic after a reply without the wake word? Does the Satellite1 firmware expose a "stop" wake word for timers?

*Outcome (2026-11-01, decisions log):* the assistant alone connects; Home Assistant doesn't, and never gets the new key. Follow-up listening works; the firmware has a "stop" word while a timer rings (not yet tried by voice). The satellite runs our build of FutureProofHomes' stable firmware: our API key, Bill's wake words, and the clean microphone channel.

*Then:* the ESPHome satellite adapter, designed from those findings: wake-word sessions, audio in, LED states, reply audio via URL, announcements, follow-up listening, timers ringing on the device that set them and stopping by voice or button; the notification pending indicator on the satellite's LEDs. Room mapping per device. One Satellite1 first; others after Bill confirms.

**Out of scope:** the custom satellite protocol and firmware (architecture Appendix A). Configuration changes to the stock firmware (key, wake words, microphone channel) are in scope.

**Done criteria:**
- [ ] Compatibility findings reviewed by Bill, and the adapter approach agreed, before adapter code is written.
- [ ] One satellite handles wake word → command → reply for timers, weather, search, agents, home control, and music.
- [ ] Pending notifications light the satellite.
- [ ] Clarifying questions reopen the mic without the wake word (or the documented fallback works).
- [ ] A timer rings on the right device and stops by voice or button.
- [ ] Home Assistant's own behaviour with the device is documented and acceptable to Bill.
- [ ] After Bill's go-ahead, all satellites are moved over.

**Blocking decisions:** outcome of this stage's compatibility checks.

### Stage 13: Satellites at household scale (optional)

**Goal:** ten or more Satellite1s work together across the house. Any group of them can play one audio stream in sync, and the dashboard shows what each satellite is doing and adds satellites to a stream or removes them. Starts only after Stage 12 is approved, with one satellite fully working.

**Why a stage of its own:** Stage 12 proves one satellite. At ten, problems appear that one device can't show. The 2026-11-02 review (decisions log) found these:
- Turns in different rooms queue for one Whisper and one Kokoro.
- An announcement to several rooms is synthesized once per satellite.
- Two satellites can answer the same words.
- Satellites are only read at startup.
- Each satellite loads its own speech-detection and turn-detection models.
- Rooms, players and satellites are mapped in separate places.
- Nothing shows when a satellite goes offline.
- The architecture still advises against playing music on the satellites' own speakers (§12, Sendspin).

**Scope:**

*Before the audio and dashboard work, perform targeted checks on two satellites and record the findings in this stage's evidence:*
1. **Music to a satellite without Home Assistant:**
   - Our firmware already includes a Sendspin player (FutureProofHomes v0.2.1, `common/sendspin.yaml`; Snapcast was removed).
   - Music Assistant's built-in Sendspin provider finds it over mDNS and connects to it directly. That connection doesn't use the ESPHome API, so the assistant stays the API's only client.
   - Confirm that Music Assistant (2.9.11, the Home Assistant add-on) lists each satellite as a player and plays to it.
   - Check what it names the players, and whether the old Home Assistant players need removing.
   - Check which audio format to prefer for ten streams over Wi-Fi: Opus rather than FLAC.
   - Sendspin is a "technical preview" in Music Assistant, and only one Music Assistant server on the network may use it.
2. **Synchronized groups:**
   - Two satellites in one Music Assistant sync group stay in sync.
   - `players.set_members(target, player_ids_to_add, player_ids_to_remove)` in `music-assistant-client` adds and removes members while the music plays.
   - Known issue: music-assistant/support#4773 (playing to one speaker of a Sendspin group).
3. **Voice while music plays:**
   - The firmware lowers its own music by 20 dB during a voice turn and during announcements.
   - Check that the wake word and transcription still work at normal volume, which is a test of the XMOS echo cancellation on its own playback.
   - Check that ducking during a turn touches only the satellite that heard it, not its whole group.
4. **Load:** with every satellite connected, measure on the Mac mini:
   - the assistant's memory;
   - event-loop lag;
   - how long a turn waits for Whisper and Kokoro when two or three rooms speak at once;
   - the LM Studio router's behaviour with parallel requests.
5. **Which satellite heard it best:**
   - Say the same requests from several places between two satellites, and record what each one sends.
   - Does the speech level against each device's own background noise, over the first half second after the wake word, pick the satellite nearest the speaker?
   - Does arrival order agree?
   - Do the levels change with distance on the microphone channel we use, or does the XMOS processing even them out?
   - Can a firmware configuration change send the wake-word score? It is computed on the device but not sent.

*Then:*
- **Device registry:**
  - One place in `tools.yaml` maps each satellite to its room. Several satellites may share a room.
  - Each room maps to its music player and its Home Assistant area, so "turn off the lights" means the lights in that room.
  - The scattered mappings (`music.rooms[].satellite`, the relay's `targets`) read from it.
- **Fleet management:**
  - Satellites added to or removed from `tools.yaml` connect or disconnect without a restart.
  - Each satellite's connection state goes into the health checks and the dashboard's health strip.
  - An offline satellite raises a low-priority notice.
  - One command builds the firmware once and updates every satellite over Wi-Fi, reporting each device's firmware version.
- **Wake-word arbitration:** one turn per utterance, from the satellite that heard it best. Every candidate's audio is collected for a short window, and the arbitration finishes while the user is still speaking, so it adds no wait.
  - When a satellite starts a turn, open a window (about 0.5 s, configurable) for other satellites reporting the same wake word.
  - Score each candidate on the audio it streamed after the wake word: the speech level against that device's own background noise, plus a configured per-satellite offset for rooms that read loud or quiet.
  - Add the wake-word score if the check finds a way to send it.
  - The satellite with an open question or a ringing timer wins. Otherwise the highest score wins, and near-ties go to the first to report.
  - The others end their turn quietly: their LED ring goes back to idle, and their audio never reaches Whisper.
  - Every decision is logged with all the candidates' scores, for tuning.
- **Concurrent turns:**
  - Queue waits for the shared speech models are logged and shown.
  - Turn-detection models are shared between satellites where Pipecat allows it.
  - Replies to satellites start before the whole reply is synthesized (a streamed URL) if the measurements show the wait matters.
- **Multi-satellite audio:**
  - Satellites become Music Assistant players (Sendspin, if the checks confirm it), and Music Assistant's sync groups play one stream in sync.
  - By voice: "play … everywhere", "play … in the kitchen and the office", "add the bedroom", "take the office out", "stop the music in the kitchen".
  - Ducking during a voice turn ducks only the satellite that heard it.
- **Announcements to several rooms:**
  - "Announce dinner's ready everywhere" or "in the kitchen and the office" synthesizes once and sends the same audio to each satellite: over ESPHome, one device at a time, roughly aligned.
  - Or, if the check shows it works with these satellites, through Music Assistant's `play_announcement` on a group.
  - Timers and notifications keep going only to their own room.
- **Satellite dashboard:** a view in the example client beside the task dashboard, updated live over the client's websocket.
  - For each satellite:
    - its room and whether it's online;
    - its voice state: idle, listening, thinking, replying, or announcing;
    - its latest request and reply;
    - its running timers;
    - what it is playing, at what volume, and in which group.
  - Actions:
    - add a satellite to the stream another one is playing, or remove it;
    - start or stop playback on it;
    - set its volume.

**Out of scope:**
- The custom satellite protocol and firmware (architecture Appendix A).
- Speaker identification.
- Sample-accurate sync for announcements: each satellite fetches the same audio, close enough for speech.
- Intercom (live audio from one satellite to another).

**Done criteria:**
- [ ] Check findings reviewed by Bill, and the music route agreed, before the audio work starts.
- [ ] With every satellite connected, memory, event-loop lag and the queue waits for simultaneous turns are measured on the Mac mini and accepted by Bill. A turn in one room is unaffected by the idle satellites.
- [ ] Words heard by several satellites are answered once, by the one nearest the speaker in at least 9 of 10 tries from mixed positions. Each decision and its scores are logged.
- [ ] A satellite added to or removed from `tools.yaml` connects or disconnects without a restart. An offline satellite shows in the health strip and the dashboard.
- [ ] One command updates the firmware of every satellite and reports each one's version.
- [ ] "Play … in the kitchen and the office" plays in sync. Adding and removing satellites by voice works while the music plays, and ducking affects only the room that is talking.
- [ ] An announcement to several rooms is synthesized once and plays in each.
- [ ] The dashboard shows every satellite's room, online state, voice state, latest request, timers, and what it is playing in which group, updated live. A satellite can be added to or removed from a stream from the dashboard.
- [ ] "Turn off the lights" from a satellite acts on that room's Home Assistant area.

**Blocking decisions:**
- the outcome of this stage's checks (the music route);
- music on the satellites' own speakers, on existing speakers, or both (architecture open question 1);
- whether dashboard controls are open to every household login;
- the wake-word arbitration window;
- the architecture changes proposed in the decisions log (2026-11-02).

### Stage 14: Carried-over fixes and checks

**Goal:** the bugs and open checks left by Stages 10–12 are fixed or settled.

**Why a stage of its own:** Bill approved Stage 12 with these open and moved them here (2026-11-02), so Stage 13 can start.

**Scope:**

*From Stage 12 (satellites):*
- The router model (gemma-4-12b on the M2) fails tool calls: about 4 in 10 "set a timer" requests come back empty even with the retry, the rest write the call as text, and nearly every reply is padded with blank lines to the 200-token limit (13-16 s). Try a replacement for the router and Hermes under the one-copy rule (qwen3-coder-30b is already downloaded), and reword the empty-reply fallback so it can't be taken for a hearing problem.
- By voice on the satellite: search, agents, home control and music (time, weather and timers work); answering a clarifying question without the wake word (the mic reopens, the answer is untested).
- A long Hermes task by voice: "Hermes is working on that for you.", the result as a notification lighting the ring amber, "any notifications?" reading it and turning the ring off.
- Replies to satellites streamed, so playback starts before the whole reply is synthesized.
- The satellite's own wake-word check often rejects "Victoria" ("model predicts 'victoria', but VAD model doesn't").
- The timer reply "1-minute timer set for 1 minute." repeats itself.
- Home Assistant's own behaviour with the device: documented in docs/satellites.md; Bill's acceptance.
- Every satellite moved over, after Bill's go-ahead (with Stage 13's fleet work).
- Pipecat 1.15 deprecates PipelineTask/PipelineRunner (removed in 2.0): move the chat, voice, agent and satellite pipelines to PipelineWorker/WorkerRunner, with a live voice check.

*From Stage 11 (hardening):*
- One week of daily use, with issues logged and fixed or accepted by Bill.
- The end-to-end benchmark of voice and text interactions, on the Mac mini.
- The runbook (start, stop, update, restore, add a tool, add an agent), reviewed by Bill.
- Crash and restart checks on the Mac mini.

*From Stage 10 (music):*
- Sound from Safari after the switch to PCM.
- Spotify.
- The Alexa relay.
- Ducking by voice.

**Done criteria:** each item above is fixed and checked, or Bill accepts it as it is (recorded in the decisions log).

**Blocking decisions:** which router model to try; whether the M2 or the Mac mini is the place to judge it.

## 5. Later (not scheduled)

Each needs its own approved stage before work begins: the backend-neutral satellite protocol and custom ESPHome component (architecture Appendix A); intercom between satellites; the optional decision-engine pre-router (architecture §7.1); speaker identification; A2A agents; additional relay agents.