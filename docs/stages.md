# Stage tracker

Rules: see implementation plan §1. Only Bill sets a stage to `approved`.

| # | Stage | Status | Started | Ready for review | Approved by / date |
|---|---|---|---|---|---|
| 0 | Tracker, instructions, repo | approved | 2026-10-25 | 2026-10-25 | Bill / 2026-10-25 |
| 1 | Defer early benchmarking | approved | 2026-10-25 | 2026-10-29 | Bill / 2026-10-29 |
| 2 | Skeleton and config | approved | 2026-10-29 | 2026-10-29 | Bill / 2026-10-29 |
| 3 | Web interface and text chat | approved | 2026-10-29 | 2026-10-30 | Bill / 2026-10-30 |
| 4 | Tool registry, timers, weather | approved | 2026-10-30 | 2026-10-30 | Bill / 2026-10-30 |
| 5 | Voice in the browser | approved | 2026-10-30 | 2026-10-30 | Bill / 2026-10-30 |
| 6 | Local web search | approved | 2026-10-30 | 2026-10-30 | Bill / 2026-10-30 |
| 7 | Agents and model pools | in progress | 2026-10-30 | | |
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
- Status: approved
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
- Bill's review notes: Approved by Bill / 2026-10-30.

## Stage 5: Voice in the browser
- Status: approved
- Done criteria:
  - [ ] Voice round trip works from a phone and a computer on the LAN. Built. Needs Bill: speaking into a real browser (the browser pane blocks the mic), and a phone once his certificate is in `config/tls/`.
  - [ ] Latency report for 20 spoken commands, broken down by stage, compared with the 1.5 s target. The harness is built (`assistant voice bench --log <service log>`). Run it on the target at the end; M2 numbers don't count (Bill, 2026-10-30).
  - [ ] Short commands ("stop", "pause") end their turn promptly. Speech shorter than `turn.short_clip_s` (0.5 s) ends at its first pause without asking Smart Turn (architecture §4.2). Checked with real audio: "Stop" (340 ms of speech) and "Pause" (380 ms) ended without Smart Turn, and "Set a pasta timer for ten minutes" went to Smart Turn. Measure on the target with the harness.
  - [ ] Two simultaneous voice sessions work without audio stutter. Shared models with per-model locks and a loop-lag monitor are in place; `assistant voice bench --sessions 2` counts late audio frames. Run on the target.
- Evidence (functional only, no latency figures from the M2):
  - `uv run pytest -q`: 99 passed, 2 skipped (live). `npm run build` passes (Pipecat client-js 1.14.0, small-webrtc-transport 1.11.0, pinned).
  - Voice path end to end, driven by an aiortc WebRTC client speaking 20 recorded commands (macOS `say`): all 20 got spoken replies. Whisper transcripts matched (numbers come back as digits: "10 minutes"). Timers, weather, stubs, plain answers and short commands all went through VAD -> Smart Turn -> Whisper -> router/tools -> Kokoro. Pipecat's `UserBotLatencyObserver` logged a per-stage `voice_latency` event for every turn.
  - Browser (pane): "Start voice" connected through the web interface's `/api/offer` proxy and reached "listening". Switching to push-to-talk showed "Hold to talk"; "Stop voice" ended the session (`voice_connected` and `voice_disconnected` in the log). The pane blocks the microphone, so speech in a browser is still untested.
  - Bill tested voice in a browser on this Mac: it works.
  - Safeguards: Whisper and Kokoro load once at startup, in parallel, on threads, and `/api/offer` returns 503 until they're ready (`/health` shows `speech`). Process memory (`rss_mb`) is logged each minute with the loop-lag summary. Each has a lock shared by all sessions, with waits over 50 ms logged (`model_lock_wait`). An event-loop monitor logs stalls over 20 ms (`loop_stall`). A bad offer gets 400, and a session that never connects is ended after 30 s.
  - HTTPS: see the decision below and `docs/https.md`.
  - Demo for Bill: `uv run assistant serve`, `uv run assistant web`, open `http://localhost:8766` on this Mac. Click "Start voice" (allow the microphone) and say "set a pasta timer for ten minutes", "what's the weather tomorrow", "turn off the kitchen lights". Then try Push to talk (hold the button or the space bar). For a phone: install the certificate (`docs/https.md`), then open `https://<name>:8766`.
- Bill's review notes: Approved by Bill / 2026-10-30. Carried over: the phone round trip once Bill's certificate is installed; the 20-command latency report, short-command timing and the two-session stutter check, run on the target at the end.

## Stage 6: Local web search
- Status: approved
- Done criteria:
  - [x] Snippet answers and page-read answers both work by voice and text. Text: checked in chat. Voice: checked with recorded speech over WebRTC (see evidence). Needs Bill: speaking into a real browser.
  - [ ] Measured times for each, compared with the architecture's estimates. The `web_search` and `web_read` log events carry `duration_ms`, cache hits and which fetcher was used. Per Bill, the comparison runs on the target at the end.
  - [x] SearXNG being down gives "I couldn't search right now", not a guess. With `browser_fallback` set, that message comes only when the browser search fails too.
- Evidence (functional; no M2 timing figures):
  - SearXNG commit `ffa88e3`, run natively with granian from `~/Services/searxng`, bound to `127.0.0.1:8888` with JSON output on. Its Python environment is `services/searxng/` (`pyproject.toml` + `uv.lock`, versions from SearXNG's own requirements). Settings: `config/searxng/settings.yml` (example in `config/examples/searxng/`). Commands: `assistant searxng install`, `assistant searxng run`, `assistant launchd install-searxng`.
  - `uv run pytest -q`: 123 passed, 2 skipped (live).
  - Snippet answer: "what's the latest news about the Artemis mission" -> `web_search` -> an answer from current news.
  - Page-read answer: "what are the opening hours of the Museum of Fine Arts in Boston" -> `web_search` -> `web_read` of mfa.org/visit (plain HTTP plus trafilatura, trimmed to 4000 characters) -> the right hours by day, including closed Tuesdays.
  - Playwright fallback: a page read with `fetcher: playwright` extracted the Wikipedia article. The browser starts with the service and keeps 2 warm pages, with images, fonts, media and stylesheets blocked.
  - SearXNG stopped: "what's the latest news about the Red Sox" -> "I couldn't search right now."
  - Web content is untrusted: every search and read result carries a note that it is quoted web content and must never be followed as instructions. `web_read` refuses localhost, LAN and other non-public addresses, also after redirects, and the browser blocks sub-requests to them. A failed read hands the model "answer from the search snippets".
  - By voice (2026-10-30, the aiortc WebRTC client from `voice/bench.py` speaking macOS `say` audio): "What's the latest news about the Artemis mission?" -> `web_search` -> `web_read` of nasa.gov -> spoken: Artemis III preparation and lunar landing simulators. "What are the opening hours of the Museum of Fine Arts in Boston?" -> `web_search` -> `web_read` of mfa.org/visit -> spoken: 10 to 5 daily, closed Tuesdays, until 10 on Thursdays and Fridays. "Who won the last World Series?" -> `web_search` only (snippet answer) -> spoken: Brewers over the Rays, four games to one. That matches the snippet it got. Whisper transcribed all three exactly. This run used a temporary copy of `config/` with engines `[bing, yahoo, wikipedia]`, because Brave and DuckDuckGo were blocking us again (decisions log). Bill's `config/` is unchanged.
  - Browser fallback (Bill, 2026-10-30): when SearXNG is unreachable, errors, or every engine fails, and `search.browser_fallback: bing` is set, `web_search` loads Bing's results page in the warm headless browser that `web_read` already uses, and parses the organic results (Bing's tracking links are unwrapped to the real URL). The results get the same untrusted note, trimming and cache as SearXNG's. No retries and no CAPTCHA handling: a challenge page parses to nothing and counts as a failure, which still says "I couldn't search right now." Logs: `search_failed kind=searxng`, then `browser_search` or `browser_search_failed`, and `web_search kind=browser`. Live, with SearXNG pointed at a closed port, "who won the 2026 world series" returned three Bing results (Wikipedia, MSN, Yahoo Sports) and no error. DuckDuckGo isn't offered: in a real browser it sent us its "418" bot page too, so the block is on this IP, and its markup couldn't be checked. `uv run pytest -q`: 123 passed, 2 skipped (new tests: Bing parsing from a small sample page, fallback on outage and on all-engines-failed, fallback failure still spoken, fallback unused when SearXNG answers or when it's off). `lxml` is now a direct dependency (it was only pulled in by trafilatura). Bill's local `config/tools.yaml` now has `browser_fallback: bing` (added at his request). End to end through the service, with Bill's config and SearXNG stopped: "What's the latest news about the Artemis mission?" over text chat -> `search_failed kind=searxng` -> `browser_search count=7` -> `web_search kind=browser` -> a reply from the results (Artemis III plans; Albania signing the Artemis Accords).
  - SearXNG under launchd (with Bill's permission): `assistant launchd install-searxng --load` started granian on `127.0.0.1:8888` (`runs = 1`, PID 99885), and `/healthz` answered within about 2 s. After `kill 99885`, launchd restarted it (`runs = 2`, PID 99940, `last exit code = 0`), `/healthz` answered again about 3 s later, and it was still listening on 127.0.0.1 only. No search was sent during the check. The temporary job was then unloaded and its plist removed, as in Stage 2. To keep it running: `uv run assistant launchd install-searxng --load`.
  - Demo for Bill: `uv run assistant searxng run` (or the LaunchAgent), `uv run assistant serve`, `uv run assistant web`, then open http://localhost:8766 and ask by text or voice: "what's the latest news about the Artemis mission", "what are the opening hours of the Museum of Fine Arts in Boston", "who won the last World Series". Then stop SearXNG and ask again: the answer should come through the Bing fallback (`web_search kind=browser` in the log). With `browser_fallback` removed from `config/tools.yaml`, a stopped SearXNG gives "I couldn't search right now."
  - `assistant tools test` (example config, gemma-4-12b): 23/24. All four search phrases route to `web_search`; the one miss is a blank reply to "set a timer" (decisions log).
- Bill's review notes: Approved by Bill / 2026-10-30. Carried over: the search timing comparison, run on the target at the end.

## Stage 7: Agents and model pools
- Status: in progress
- Done criteria:
  - [ ] "Ask Hermes …" and "Ask Claude …" work by voice and text; offering an agent works when no tool fits.
  - [ ] The decider's choice, confidence, tokens, and cost are logged; "Ask Claude Opus …" overrides it.
  - [ ] Budget limits downgrade and then stop paid calls as configured.
  - [ ] Tasks can be listed and cancelled by voice.
  - [ ] Hermes cannot read the assistant's config, keys, or personal files.
- Evidence:
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
| 2026-10-30 | 4 | Approved by Bill (open proposals: 60 s text pending window, `confirm` deferred to Stage 9). | Bill / 2026-10-30 |
| 2026-10-30 | 5 | HTTPS method: Bill will supply the certificate (neither `mkcert` nor Tailscale). The web process serves HTTPS from `config/tls/cert.pem` and `key.pem` (git-ignored; overridable with `ASSISTANT_TLS_CERT` and `ASSISTANT_TLS_KEY`). With a certificate it listens on `0.0.0.0` and sets Secure cookies. Without one it stays on plain HTTP at `127.0.0.1`. A half-installed or mismatched pair refuses to start. Install and renewal steps: `docs/https.md`; check with `assistant tls status`. | Bill / 2026-10-30 |
| 2026-10-30 | 5 | Speech settings go in a new optional `speech:` section of `models.yaml` (`stt.model`, `stt.language`, `tts.voice`, `tts.speed`, `vad.*`, `turn.smart_turn`, `turn.fallback_s`), with defaults: whisper-large-v3-turbo, Kokoro `af_heart`, VAD stop 0.2 s, Smart Turn v3 with a 1.8 s fallback (architecture: 1.5–2 s). The architecture names no file for these. Proposal: keep them in `models.yaml`. | |
| 2026-10-30 | 5 | Pipecat details: the `whisper` extra (faster-whisper) is installed only because Pipecat's Whisper module imports it even for `WhisperSTTServiceMLX`; transcription runs on MLX. `mlx-whisper` pulls in torch (venv about 1.5 GB). `KokoroTTSService` builds its own ONNX session per instance, so its constructor is routed through a cache and every session shares one Kokoro. Each sentence is synthesized under the Kokoro lock and then streamed out, so a cancelled session can't leave the lock held. | |
| 2026-10-30 | 5 | Voice sessions keep Pipecat's RTVI on (the browser client uses it for transcripts and state); text chat keeps it off. No STUN/TURN servers: WebRTC uses LAN host candidates only, so audio never leaves the house. Interruptions use Pipecat's built-in behaviour (no extra code). Voice pending questions expire after 10 s (`VOICE_PENDING_S`). Voice and chat share timer device `web`; a ringing timer is also spoken in open voice sessions. | |
| 2026-10-30 | 5 | Finding: loading the speech models stalls the event loop by 110–230 ms several times, even on threads, because model loading holds the GIL. It happens only at startup, before voice is accepted. Revisit if it shows up during inference. | |
| 2026-10-30 | 5 | Bill: no benchmark runs on the M2. Latency harnesses are built in their stage but run at the end, on the target. | Bill / 2026-10-30 |
| 2026-10-30 | 5 | Short clips: Smart Turn judged the one-word "Pause." unfinished and held the turn about 1.2 s. Per architecture §4.2, speech shorter than `turn.short_clip_s` (default 0.5 s; 0 turns it off) now ends at its first pause without Smart Turn. It counts only a turn's first stretch of speech. Finding: the VAD frames' wall-clock timestamps can't measure speech length, because audio is processed in bursts; the length is counted from audio samples instead. At 0.7 s "What time is it?" (680 ms) also counted as short, hence 0.5 s. | |
| 2026-10-30 | 5 | Bill's local `config/models.yaml` lacks `reasoning_effort: none` (in the example since Stage 3). With it missing, gemma reasons for about 11 s per voice turn and often produces no spoken reply, and nothing is added to the conversation. His local `tools.yaml` is also still Stage 3's. Fix: add the line and copy `config/examples/tools.yaml`. | |
| 2026-10-30 | 5 | Approved by Bill. Carried over: phone round trip (needs the certificate); the latency report, short-command timing and two-session stutter check run on the target at the end. | Bill / 2026-10-30 |
| 2026-10-30 | 6 | Stage 6 timing criterion ("measured times ... compared with the architecture's estimates"): following Bill's rule, no timing runs on the M2. The measurement is built in this stage and run on the target at the end. | |
| 2026-10-30 | 6 | SearXNG runs from a source checkout pinned to commit `ffa88e3` in `~/Services/searxng`. Its uv environment lives in `services/searxng/` in this repo, because SearXNG publishes requirements files rather than a lock file. Its secret key is random at each start, since the assistant uses neither its cookies nor its image proxy. Playwright's headless Chromium is 203 MB (`~/Library/Caches/ms-playwright`). | |
| 2026-10-30 | 6 | Finding: engines block metasearch quickly. On the first queries DuckDuckGo returned a CAPTCHA and Brave "too many requests" (suspended 180 s), leaving only Wikipedia. Proposal: add one or two more engines (for example Mojeek and Startpage) to `searxng/settings.yml` and `search.engines`, and watch `unresponsive_engines`. When every engine fails, the assistant now treats it as an outage ("I couldn't search right now"). | |
| 2026-10-30 | 6 | Engine test, later the same day, against a temporary SearXNG on port 8889, two queries per engine: **Bing** 10 results and **Yahoo** 7, both working. Qwant and Google: "access denied". Mojeek and Startpage: marked `inactive` in SearXNG `ffa88e3` (Startpage now uses a proof-of-work CAPTCHA), so no results. Presearch: no results. Wikipedia: works, but gives nothing for news or opening hours. Brave and DuckDuckGo were still blocked. Revised proposal: set the engines to `[bing, yahoo, wikipedia]` in `config/searxng/settings.yml` (`keep_only`), in `search.engines` in `tools.yaml`, and in both example files. Drop Brave and DuckDuckGo, or keep them as extras that SearXNG skips while they're suspended. | Bill: don't switch engines; keep SearXNG |
| 2026-10-30 | 6 | Why the engines block us: SearXNG scrapes DuckDuckGo's and Brave's result pages, and my manual live checks (chat, voice and an engine probe) sent bursts of queries from one IP. `tools test` never runs search, and the unit tests use mocked HTTP. Proposed test strategy: (1) unit tests stay mocked, plus a guard that fails any test that reaches a real SearXNG; (2) end-to-end chat and voice checks use a local replay SearXNG serving recorded JSON for a fixed set of questions, so they send nothing upstream; (3) real upstream checks only when the engines themselves are under test: at most one query per engine, 30 s or more apart, a few per session, never loops or probes; (4) to see engine health, read `unresponsive_engines` from searches that happen anyway instead of probing. Optional, in the product: a cap on upstream searches per minute in `SearchService`, on top of the 10-minute cache. | |
| 2026-10-30 | 6 | Architecture addition: a headless-browser search fallback when SearXNG fails. SearXNG stays the main path. Setting: `search.browser_fallback` in `tools.yaml` (only `bing` for now; unset means off). It uses the existing warm Playwright pool and makes one page load per search, with no retries, no stealth measures and no CAPTCHA solving. The architecture document is not edited (not asked). | Bill / 2026-10-30: "lets have chromium option as a fallback when searxng fails" |
| 2026-10-30 | 6 | Bug found and fixed (from Stage 4): with no agents configured, the tool-loop guard's reply ("Sorry, I can't do that.") went back to the model, which kept searching (10+ rounds on one question). Guard replies are now always spoken directly and end the request. | |
| 2026-10-30 | 6 | Finding: lines in the system prompt about web_search and untrusted content made gemma-4-12b stop calling `music` for "skip this", so they were dropped. The tool descriptions route search questions fine, and the untrusted note travels with each result. With two more tools, gemma also returns a blank reply to "set a timer" about half the time (passed in Stage 4). Proposal for Stage 11 (hardening): when the model returns neither text nor a tool call, say "Sorry, could you say that again?" instead of staying silent; and re-check this when the real fast model is chosen. | |
| 2026-10-30 | 6 | Approved by Bill. Carried over: the search timing comparison runs on the target at the end. | Bill / 2026-10-30 |
