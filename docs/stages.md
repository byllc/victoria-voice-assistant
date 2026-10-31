# Stage tracker

Rules: see implementation plan §1. Only Bill sets a stage to `approved`.

| # | Stage | Status | Started | Ready for review | Approved by / date |
|---|---|---|---|---|---|
| 0 | Tracker, instructions, repo | approved | 2026-10-25 | 2026-10-25 | Bill / 2026-10-25 |
| 1 | Defer early benchmarking | approved | 2026-10-25 | 2026-10-29 | Bill / 2026-10-29 |
| 2 | Skeleton and config | approved | 2026-10-29 | 2026-10-29 | Bill / 2026-10-29 |
| 3 | Web interface and text chat | approved | 2026-10-29 | 2026-10-30 | Bill / 2026-10-30 |
| 4 | Tool registry, timers, weather | ready for review | 2026-10-30 | 2026-10-30 | |
| 5 | Voice in the browser | not started | | | |
| 6 | Local web search | not started | | | |
| 7 | Agents and model pools | not started | | | |
| 8 | Notifications and task dashboard | not started | | | |
| 9 | Home control | not started | | | |
| 10 | Music | not started | | | |
| 11 | Hardening | not started | | | |
| 12 | Satellites (optional) | not started | | | |

## Stage 0: Tracker, instructions, repo
- Status: approved
- Done criteria:
  - [x] `docs/stages.md` exists, matches the template, with Stage 0 `ready for review`.
  - [x] `AGENTS.md` and `CLAUDE.md` exist and state the gate rules. Also `.github/copilot-instructions.md` for Copilot sessions.
  - [x] Architecture document and plan are in `docs/`.
- Evidence:
  - `docs/stages.md` created from the plan's §3 template; this tracker is that file.
  - `AGENTS.md` contains the approval gate (plan §1) and ground rules (plan §2); `CLAUDE.md` points to it.
  - `.github/copilot-instructions.md` contains the same gate plus build guidance for Copilot.
  - `docs/voice-pipeline-architecture.md` (v2.9) and `docs/implementation_plan.md` (v1.2) are in `docs/`.
  - Demo: open `docs/stages.md` — only Stage 0 is active; open `AGENTS.md` — gate rules present.
- Bill's review notes:

## Stage 1: Defer early benchmarking
- Status: approved
- Done criteria:
  - [x] Early spike code, reports, and results are removed.
  - [x] The known Smart Turn risk is recorded for later end-to-end validation.
  - [x] End-to-end pipeline benchmarking is assigned to Stage 11.
- Evidence:
  - Removed `spikes/` and `docs/spikes-report.md`; performance results are deferred to Stage 11.
  - Current local test model is `google/gemma-4-12b`.
  - Stage 11 now owns end-to-end voice/text validation with per-stage latency reporting.
  - Smart Turn's possible mid-utterance interruption remains a risk to validate with the integrated voice pipeline.
- Bill's review notes: Approved by Bill / 2026-10-29.

## Stage 2: Skeleton and config
- Status: approved
- Done criteria:
  - [x] All configuration is read from `config/` (or `ASSISTANT_CONFIG_DIR`); nothing is read from the repository root.
  - [x] `assistant config check` validates the example configs and rejects broken ones with clear messages.
  - [x] Editing a config file reloads it; an invalid edit is rejected and logged.
  - [x] The service starts under `launchd` and restarts when killed.
  - [x] Unit tests pass.
- Evidence:
  - Python 3.12.12 pinned in `.python-version`; `uv sync --locked --extra dev` succeeded.
  - `uv run pytest -q`: 12 passed.
  - Example validation: `LMS_API_KEY=test-api-key uv run assistant config check --config-dir config/examples` succeeded. Default `uv run assistant config check` also succeeded with local `config/.env`; secret value not recorded.
  - Health app starts with `127.0.0.1` binding; `/health` returned `{"status":"ok","config_revision":1}`.
  - LaunchAgent ran (`runs = 1`, PID 38795); after `kill 38795`, launchd restarted it (`runs = 2`, PID 38885) and `/health` returned healthy. Temporary job unloaded; port 18765 stopped listening.
  - `plutil -lint` passed for LaunchAgent template. Demo: `uv run assistant config init`, set `LMS_API_KEY` in `config/.env`, then follow Stage 2 Local setup commands in `docs/implementation_plan.md`.
- Bill's review notes:

## Stage 3: Web interface and text chat
- Status: approved
- Done criteria:
  - [x] Typed messages get streamed replies from the configured fast model (live, M2 dev box, temp config).
  - [~] Stopping LM Studio's primary model triggers the fallback model. Simulated with a dead primary server URL; the real LM Studio model was not unloaded.
  - [x] Killing the web process doesn't affect the assistant service. Not yet verified: restart of each process under launchd.
  - [~] The assistant service accepts connections only from localhost. Unit-tested (`LoopbackOnlyMiddleware`); not tested live from a non-loopback address.
  - [x] Time to first token is logged per request (`llm_first_token`, `ttft_ms`; `llm_response_complete`, `total_ms`).
  - [x] Unit tests pass.
- Evidence:
  - `uv run pytest -q`: 28 passed, 1 skipped (the live LM Studio test, gated by `ASSISTANT_LIVE_TESTS=1`).
  - `npm run build` in `web/client` passes (Vite 8, TypeScript 7).
  - Web login: bad password → 401; correct password → `authenticated: true`. `/ws` through the web process on `:8766` streamed `Hello there!` then `done`.
  - Failover: primary `fast` pointed at a dead URL. The `llm_failover` event fires at about 1.5 s. The `LLMRunFrame` retry on the fallback (`llama-3.2-3b-instruct`) streamed `Hello, it's nice to meet you!`. First token about 0.33 s after the switch. The client shows one reply bubble, because the failed attempt emits no tokens.
  - TTFT log sample: `llm_first_token` `model=fast-small` `ttft_ms=901.0`; `llm_response_complete` `total_ms=1218.7`.
  - Process independence: the assistant service stayed up (`/health` 200) after the web process was stopped.
  - Pinning: `lms ps --json` skips already-loaded models (logs "Model already pinned"). Verified on the M2.
  - Demo: `ASSISTANT_WEB_PASSWORD=<pw> uv run assistant web --port 8766` and `uv run assistant serve`, then open `http://127.0.0.1:8766`, sign in, and type a message. Failover demo: stop the primary model in LM Studio and send a message.
  - Findings (see decisions log): gemma-4-12b reasoning delay and empty replies at `max_tokens: 200`; the Pipecat output transport needs an `OutputTransportMessageFrame` to deliver text to the client.
- Bill's review notes:

## Stage 4: Tool registry, timers, weather
- Status: ready for review
- Done criteria:
  - [x] `tools test` passes on the example phrases at an accuracy Bill agrees with, and reports failures clearly. 20/20 on gemma-4-12b (M2); accuracy threshold is Bill's call.
  - [x] Multiple named timers: start, cancel by name, add time, status of one and all; "cancel the timer" with two running asks which.
  - [x] Timers survive a service restart.
  - [x] Calling a stub speaks its notice and shows setup steps in chat; a broken tool definition becomes an automatic stub.
  - [x] Template replies skip the second model call (visible in timing logs).
- Evidence:
  - `uv run pytest -q`: 84 passed, 2 skipped (live). `ASSISTANT_LIVE_TESTS=1 uv run pytest -q -k live`: 2 passed (Stage 3 chat + Stage 4 tools test, against LM Studio). `npm run build` passes.
  - `assistant tools test --config-dir config/examples` (gemma-4-12b, `reasoning_effort: none`, M2): **20/20 passed, median 1370 ms per request**. Covers every tool's `examples` plus `tests` (clarify on a missing duration, clarify when two timers run, stub routing, `agent:helper`, `offer_agent`, a plain answer). Failures print `FAIL`, the phrase, what the model called, and `expected X, got Y [source]`. Exit code is non-zero below `--min-accuracy` (default 1.0). The first run was 19/20: "book me a flight" made gemma emit only newlines; rewording the `offer_agent` line in the system prompt fixed it (decisions log).
  - Timers, live over the websocket: "set a pasta timer for 10 minutes" -> "Pasta timer set for 10 minutes."; a second pasta timer -> "pasta 2"; "cancel the timer" with three running -> "Which timer: pasta 2, pasta or laundry?"; "pasta 2" -> "Pasta 2 timer cancelled."; "add 5 minutes to the pasta timer" -> "Added 5 minutes to the pasta timer. 14 minutes 42 seconds left."; status of one and all; a 3 s tea timer rang ("Your tea timer is done."); "stop" silenced it with no model call (`direct_reply handled_by=timer_stop`).
  - Restart: after stopping and restarting the service, the log shows `timers_restored count=2` and the client got both timers with correct remaining time. A timer that finishes while the service is down rings on start if it ended less than `ring_max_s` ago; otherwise it is dropped and logged `timer_missed` (unit-tested).
  - Stubs: "turn off the kitchen lights" spoke "Home control isn't set up yet. I've sent the setup steps to the chat." The chat got a `home_control: setup` notice with the setup text, and the log has `stub_called` with the arguments. Automatic stubs: a python tool with a missing handler, a param of type `banana`, an `mcp` kind, an unknown built-in service, and an entry with an unknown kind and key all become `auto_stub`. They are logged at startup, listed by `assistant tools status` with the reason, and flagged by `assistant config check` as warnings. The rest of the file still loads.
  - Timing logs: template tool -> `tool_call reply=template spoken_directly=true second_llm_call=false llm_call=1`, then only `llm_response_complete llm_call=1`. Weather (reply via model) -> `tool_call second_llm_call=true`, then `llm_first_token llm_call=2 ttft_ms=1696`, `llm_response_complete llm_call=2`.
  - Weather (Open-Meteo, live): "weather in Boston tomorrow" -> "mostly clear with a high of 52 and a low of 36." Geocoding plus forecast took 634 ms on the first call and 102 ms from cache. Only the fields the question needs go to the model.
  - Pending questions: "book me a flight to Denver" -> "I can't do that myself. Want me to ask Helper?"; "yes" -> "Agents aren't set up yet." in 706 ms with no model call (`direct_reply handled_by=pending_yes`).
  - Reload: editing `tools.yaml` while a chat was open logged `config_reloaded` then `tools_reloaded count=9`, and the session's tool list changed with no restart (`LLMSetToolsFrame`).
  - Web client (browser check): timer chips with live countdown; amber ringing banner with a Stop button and a repeating beep; setup notices shown as a separate block.
  - M2 latency (for design only): the first request after a restart takes 8-12 s (the ~940-token system and tool prefix is not cached yet). Template tool replies then take 2-4 s end to end; weather takes 7-9 s with the second model call.
  - Demo: `cp config/examples/tools.yaml config/tools.yaml` (Bill's local copy is still Stage 3's), set `weather.home` lat/lon, and add an agent to `config/agents.yaml` (the local copy has none, so `offer_agent` says "Sorry, I can't do that."). Then `uv run assistant tools status`, `uv run assistant tools test`, `uv run assistant serve` and `uv run assistant web`. Open http://127.0.0.1:8766 and try: "set a pasta timer for 10 minutes", "set a laundry timer for 20 minutes", "cancel the timer", "pasta", "add 5 minutes to the laundry timer", "what timers do I have", "set a tea timer for 10 seconds" (wait for it to ring, then "stop"), "turn off the kitchen lights", "what's the weather tomorrow", "book me a flight", "yes". Restart `assistant serve` with timers running and reload the page.
- Bill's review notes:

## Decisions log
| Date | Stage | Proposal or decision | Bill's answer |
|---|---|---|---|
| 2026-10-25 | 0 | Tool inventory on the Mac mini: git, uv, Node 23, LM Studio (`lms`), launchd, ffmpeg all present; system Python is 3.14 (uv will provision 3.12 per the ground rules). `mkcert` and `tailscale` are both missing — the Stage 5 HTTPS blocking decision needs one of these installed before that stage. No action taken now. | |
| 2026-10-25 | 0 | Approved by Bill. | Bill / 2026-10-25 |
| 2026-10-25 | 0 | Dev happens on an M2 Mac (32 GB RAM, macOS 26.7), not the target Mac mini; local LM Studio is running with 10 models. Ground rules updated (plan §2, AGENTS.md, copilot-instructions): every Python environment is a uv-managed virtualenv (`.venv`, interpreter pinned to 3.12); never install into system Python. Memory/latency measurements on the M2 are for design validation only. | Accepted by Bill (requested) |
| 2026-10-29 | 1 | Bill requested removal of early spike/benchmark work and use of `google/gemma-4-12b` as the current local test model. Full-system benchmarking is deferred to Stage 11. Known risk to revisit: Smart Turn may end a turn during a hesitation and may not recognize some short commands; validate with the integrated voice pipeline before final tuning. | |
| 2026-10-29 | 1 | Approved by Bill. | Bill / 2026-10-29 |
| 2026-10-30 | 3 | Finding: gemma-4-12b streams reasoning tokens before content; with `max_tokens: 200` replies were empty and first content took ~28 s at `max_tokens: 1500`. LM Studio ignores `reasoning_effort: low` for this model (297 reasoning chunks, no content) but honors `none`: first token 0.6-0.8 s, full reply. Model `params` Pipecat has no setting for now go to the request body, so `reasoning_effort: none` in `models.yaml` works (added to `config/examples/models.yaml`). Bill's local `config/models.yaml` still needs that line. | |
| 2026-10-30 | 3 | Finding: Pipecat's websocket output transport forwards only audio, image, and `OutputTransportMessageFrame`. Text frames never reach the serializer. Fix: `ChatProtocolProcessor` emits `OutputTransportMessageFrame` before `transport.output()`. | |
| 2026-10-30 | 3 | Failover: `LLMSwitcher` is sticky per WebSocket connection and does not retry the failed request. On `llm_failover`, the session queues `LLMRunFrame` so the same context reruns on the fallback. Proposal: keep. | |
| 2026-10-30 | 3 | `timeout_s` in `models.yaml` maps to Pipecat `retry_timeout_secs`, which is a stall timeout (no data for N seconds), not a total request timeout. Proposal: keep and document. | |
| 2026-10-30 | 3 | Household login: shared password from the `ASSISTANT_WEB_PASSWORD` process environment only (never `config/.env`, config files, or the command line; Bill's instruction). If unset, the web process still starts in open (insecure) mode: no login, a `web_insecure_mode` warning is logged, and the client shows a red banner "Insecure: please add an administrative password" (Bill's instruction). With a password: HMAC-signed session cookie, 30-day TTL, HttpOnly, SameSite=strict. No Secure flag until HTTPS in Stage 5. Web binds 127.0.0.1. Open: under launchd the plist has no secret, so the web agent needs the variable injected another way (e.g. `launchctl setenv`); not yet decided. | |
| 2026-10-30 | 3 | LM Studio has two `google/gemma-4-12b` instances loaded (`:2` is a duplicate created by loading an already-loaded model). Proposal: unload the duplicate. I have not unloaded it. | |
| 2026-10-30 | 3 | Stage 3 started the web process and the assistant service as separate processes. Pending: launchd install and restart for the web plist (`assistant launchd install-web`) is not yet verified live. | |
| 2026-10-30 | 3 | Approved by Bill (carried-over items: real LM Studio failover test, live non-loopback test, launchd restart of the web plist, web password under launchd). | Bill / 2026-10-30 |
| 2026-10-30 | 4/5 | Bill asked for browser voice chat to test the voice pipeline. Voice is Stage 5. Decision: finish Stage 4 first, then Stage 5. | Bill / 2026-10-30 |
| 2026-10-30 | 4 | Pending questions expire after 60 s in text chat; the architecture's ~10 s (kept as `VOICE_PENDING_S`) is too short to type a reply. Proposal: voice uses 10 s from Stage 5. | |
| 2026-10-30 | 4 | `tools.yaml` additions (no architecture change intended): an `http:` section (`url`, `method`, `headers`, `query`, `body`, with `{param}` placeholders); `required: true` inside a param; python handlers named in `service` as `package.module:function`; `tests[].given.timers` for state-dependent tests; `expect: answer`. An invalid tool entry becomes an automatic stub instead of rejecting the file, as long as it has a `name`. | |
| 2026-10-30 | 4 | Kinds `mcp`, `ha_service` and `ha_script` become automatic stubs ("aren't supported yet") until their stages. `confirm: true` is not implemented: no Stage 4 tool needs it. Proposal: add it with the first side-effect tool (Stage 9, locks). | |
| 2026-10-30 | 4 | All web chat clients share timer device `web`, so a timer rings in every open chat tab. The chime is a beep generated in the browser; `timers.sound` is unused until a device plays audio (Stage 5). | |
| 2026-10-30 | 4 | Finding (browser test): a message sent while the previous request was still generating ran on a context without the first tool result. The model re-ran the timer, so two timers started, and the first reply was held until the second finished. Fix: requests on one chat connection run one at a time; a message that arrives mid-request waits until the model calls, tool calls and spoken replies finish (`TurnPreprocessor` and `TurnTracker`, with a 60 s safety release). Pipecat has no built-in for this in a text pipeline with interruptions off. | |
| 2026-10-30 | 4 | Finding: Pipecat 1.15 `PipelineTask` adds RTVI by default; its messages reached the chat client once urgent transport messages (timers) were serialized. Disabled with `enable_rtvi=False`. Also: the model call after a tool result starts from a context frame pushed upstream, so timing counts it after the LLM, not before it. | |
| 2026-10-30 | 4 | Finding: gemma-4-12b answered "book me a flight to Denver" with 199 newline tokens when the system prompt said only "if no tool fits ... call offer_agent". Naming the cases ("bookings, purchases, research, email ... never refuse in plain text") fixed it (20/20). The system prompt is in code (`chat/prompt.py`), not config, because the architecture doesn't place it in a config file. | |
| 2026-10-30 | 4 | Live tests now need the real `LMS_API_KEY` (LM Studio rejects the test key). A `live_api_key` fixture reads it from `config/.env`. One flaky failure of `test_health_endpoint_and_config_file_watcher` under load (passes alone and on repeat). | |
