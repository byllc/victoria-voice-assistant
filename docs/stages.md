# Stage tracker

Rules: see implementation plan §1. Only Bill sets a stage to `approved`.

| # | Stage | Status | Started | Ready for review | Approved by / date |
|---|---|---|---|---|---|
| 0 | Tracker, instructions, repo | approved | 2026-10-25 | 2026-10-25 | Bill / 2026-10-25 |
| 1 | Defer early benchmarking | approved | 2026-10-25 | 2026-10-29 | Bill / 2026-10-29 |
| 2 | Skeleton and config | approved | 2026-10-29 | 2026-10-29 | Bill / 2026-10-29 |
| 3 | Web interface and text chat | ready for review | 2026-10-29 | 2026-10-30 | |
| 4 | Tool registry, timers, weather | not started | | | |
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
- Status: ready for review
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

## Decisions log
| Date | Stage | Proposal or decision | Bill's answer |
|---|---|---|---|
| 2026-10-25 | 0 | Tool inventory on the Mac mini: git, uv, Node 23, LM Studio (`lms`), launchd, ffmpeg all present; system Python is 3.14 (uv will provision 3.12 per the ground rules). `mkcert` and `tailscale` are both missing — the Stage 5 HTTPS blocking decision needs one of these installed before that stage. No action taken now. | |
| 2026-10-25 | 0 | Approved by Bill. | Bill / 2026-10-25 |
| 2026-10-25 | 0 | Dev happens on an M2 Mac (32 GB RAM, macOS 26.7), not the target Mac mini; local LM Studio is running with 10 models. Ground rules updated (plan §2, AGENTS.md, copilot-instructions): every Python environment is a uv-managed virtualenv (`.venv`, interpreter pinned to 3.12); never install into system Python. Memory/latency measurements on the M2 are for design validation only. | Accepted by Bill (requested) |
| 2026-10-29 | 1 | Bill requested removal of early spike/benchmark work and use of `google/gemma-4-12b` as the current local test model. Full-system benchmarking is deferred to Stage 11. Known risk to revisit: Smart Turn may end a turn during a hesitation and may not recognize some short commands; validate with the integrated voice pipeline before final tuning. | |
| 2026-10-29 | 1 | Approved by Bill. | Bill / 2026-10-29 |
| 2026-10-30 | 3 | Proposal: gemma-4-12b streams reasoning tokens before content. Measured on the M2 with `max_tokens: 200` (current `config/models.yaml`): replies come back empty. With `max_tokens: 1500`: first content token at about 28 s, far above the ~1.5 s tool-path target. Options: (a) a non-reasoning model for `fast`, (b) disable reasoning in LM Studio for this model, (c) raise `max_tokens`. I have not edited `config/models.yaml`. Please choose. | |
| 2026-10-30 | 3 | Finding: Pipecat's websocket output transport forwards only audio, image, and `OutputTransportMessageFrame`. Text frames never reach the serializer. Fix: `ChatProtocolProcessor` emits `OutputTransportMessageFrame` before `transport.output()`. | |
| 2026-10-30 | 3 | Failover: `LLMSwitcher` is sticky per WebSocket connection and does not retry the failed request. On `llm_failover`, the session queues `LLMRunFrame` so the same context reruns on the fallback. Proposal: keep. | |
| 2026-10-30 | 3 | `timeout_s` in `models.yaml` maps to Pipecat `retry_timeout_secs`, which is a stall timeout (no data for N seconds), not a total request timeout. Proposal: keep and document. | |
| 2026-10-30 | 3 | Proposal for household login: a shared password from `ASSISTANT_WEB_PASSWORD`, an HMAC-signed session cookie with a 30-day TTL, HttpOnly, SameSite=strict. No Secure flag until HTTPS in Stage 5. The web process binds 127.0.0.1. | |
| 2026-10-30 | 3 | LM Studio has two `google/gemma-4-12b` instances loaded (`:2` is a duplicate created by loading an already-loaded model). Proposal: unload the duplicate. I have not unloaded it. | |
| 2026-10-30 | 3 | Stage 3 started the web process and the assistant service as separate processes. Pending: launchd install and restart for the web plist (`assistant launchd install-web`) is not yet verified live. | |
