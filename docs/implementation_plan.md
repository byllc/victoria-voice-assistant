# Home Voice Assistant: Implementation Plan (v1.2)

Owner: Bill · Updated: 2026-10-25 · Companion to: *Home Voice Assistant Architecture* (`voice-pipeline-architecture.md`, currently v2.7)

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

- **Target:** Mac mini, M4 Pro, 64 GB, macOS 26. **Development happens on an M2 Mac** with local environments and local test LLMs (LM Studio); architecture memory figures are for the target machine and M2 measurements are for design validation only. Native processes under `launchd`; no Docker. Apple `container` only for agent sandboxes (Stage 8).
- **Python virtual environments.** Every Python environment (service, web interface, spikes) is a virtual environment managed by uv (`uv venv` + `uv sync` from `pyproject.toml` + lock file, interpreter pinned to 3.12). Never install packages into the system Python. `uv run` is the way to execute everything.
- **Python 3.12+, managed with `uv`, every dependency pinned in the lock file.** The web client uses TypeScript with Vite.
- **Pipecat built-ins first.** Use Pipecat's own feature wherever one exists (architecture §11 lists them). Replace a built-in only with measured evidence, recorded in the decisions log.
- **Secrets** come from environment variables, never from config files or the repo.
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
| 1 | Pipeline risk experiments | not started | | | |
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

### Stage 1: Pipeline risk experiments

**Goal:** answer the questions the rest of the build depends on, with throwaway code, before committing to a design.

**Scope (each a small script under `spikes/`, results in `docs/spikes-report.md`):**
1. **Fast-model tool calling:** in LM Studio, test two or three candidate models on about 30 household phrases with a draft tool set. Record tool-choice accuracy, argument accuracy, and time to first token. Confirm streamed tool calls work.
2. **Speech concurrency:** run `WhisperSTTServiceMLX` and `KokoroTTSService` in one asyncio process with two simultaneous requests. Measure event-loop lag and whether MLX needs a per-model lock.
3. **Turn detection:** Silero VAD plus Smart Turn on short commands ("stop", "skip", "pause") and on long, hesitant ones. Measure end-of-turn delay.

**Out of scope:** satellites (their experiments open Stage 12); production code. Spike code is not reused without being rewritten in a later stage.

**Done criteria:**
- [ ] `docs/spikes-report.md` answers each question with measurements and a recommendation.
- [ ] Any needed architecture changes are proposed in the decisions log.

**Blocking decisions:** none.

### Stage 2: Skeleton and config

**Goal:** a runnable, supervised service that loads and validates its configuration.

**Scope:** Python package layout; the `config/` folder from §2 with example files; Pydantic schemas for `config/models.yaml`, `config/tools.yaml`, and `config/agents.yaml` matching the architecture (including stubs, pools, decider, built-in service blocks); `${ENV}` secret substitution; validation errors that name the file and field; reload on save that keeps the previous config if the new one is invalid; structured logging; SQLite store with migrations; health endpoint; `launchd` plists for the assistant service; a CLI with `config check`.

**Out of scope:** models, audio, web UI.

**Done criteria:**
- [ ] All configuration is read from `config/` (or `ASSISTANT_CONFIG_DIR`); nothing is read from the repository root.
- [ ] `assistant config check` validates the example configs and rejects broken ones with clear messages.
- [ ] Editing a config file reloads it; an invalid edit is rejected and logged.
- [ ] The service starts under `launchd` and restarts when killed.
- [ ] Unit tests pass.

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

**Blocking decisions:** fast-model choice (from Stage 1).

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
- [ ] Runbook reviewed by Bill.

**Blocking decisions:** none.

### Stage 12: Satellites (optional)

**Goal:** the Satellite1s work as voice devices for the assistant. Optional: the system is complete and usable from the web client without this stage.

**Scope:**

*First, experiments (results in `docs/spikes-report.md`, reviewed by Bill before adapter work begins):*
1. **Satellite takeover:** connect to one Satellite1 with `aioesphomeapi` while Home Assistant is also connected. Can our client receive voice-assistant audio? What happens to Home Assistant? Can we drive LEDs, play a reply URL, and play an unprompted announcement?
2. **Follow-up listening and "stop":** can we reopen the mic after a reply without the wake word? Does the Satellite1 firmware expose a "stop" wake word for timers?

*Then:* the ESPHome satellite adapter, designed from those findings: wake-word sessions, audio in, LED states, reply audio via URL, announcements, follow-up listening, timers ringing on the device that set them and stopping by voice or button; the notification pending indicator on the satellite's LEDs. Room mapping per device. One Satellite1 first; others after Bill confirms.

**Out of scope:** the custom satellite protocol and firmware (architecture Appendix A).

**Done criteria:**
- [ ] Experiment results reviewed by Bill, and the adapter approach agreed, before adapter code is written.
- [ ] One satellite handles wake word → command → reply for timers, weather, search, agents, home control, and music.
- [ ] Pending notifications light the satellite.
- [ ] Clarifying questions reopen the mic without the wake word (or the documented fallback works).
- [ ] A timer rings on the right device and stops by voice or button.
- [ ] Home Assistant's own behaviour with the device is documented and acceptable to Bill.
- [ ] After Bill's go-ahead, all satellites are moved over.

**Blocking decisions:** outcome of this stage's experiments.

## 5. Later (not scheduled)

Each needs its own approved stage before work begins: the backend-neutral satellite protocol and custom ESPHome component (architecture Appendix A); the optional decision-engine pre-router (architecture §7.1); speaker identification; A2A agents; additional relay agents.