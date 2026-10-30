# Stage tracker

Rules: see implementation plan §1. Only Bill sets a stage to `approved`.

| # | Stage | Status | Started | Ready for review | Approved by / date |
|---|---|---|---|---|---|
| 0 | Tracker, instructions, repo | approved | 2026-10-25 | 2026-10-25 | Bill / 2026-10-25 |
| 1 | Defer early benchmarking | approved | 2026-10-25 | 2026-10-29 | Bill / 2026-10-29 |
| 2 | Skeleton and config | approved | 2026-10-29 | 2026-10-29 | Bill / 2026-10-29 |
| 3 | Web interface and text chat | in progress | 2026-10-29 | | |
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

## Decisions log
| Date | Stage | Proposal or decision | Bill's answer |
|---|---|---|---|
| 2026-10-25 | 0 | Tool inventory on the Mac mini: git, uv, Node 23, LM Studio (`lms`), launchd, ffmpeg all present; system Python is 3.14 (uv will provision 3.12 per the ground rules). `mkcert` and `tailscale` are both missing — the Stage 5 HTTPS blocking decision needs one of these installed before that stage. No action taken now. | |
| 2026-10-25 | 0 | Approved by Bill. | Bill / 2026-10-25 |
| 2026-10-25 | 0 | Dev happens on an M2 Mac (32 GB RAM, macOS 26.7), not the target Mac mini; local LM Studio is running with 10 models. Ground rules updated (plan §2, AGENTS.md, copilot-instructions): every Python environment is a uv-managed virtualenv (`.venv`, interpreter pinned to 3.12); never install into system Python. Memory/latency measurements on the M2 are for design validation only. | Accepted by Bill (requested) |
| 2026-10-29 | 1 | Bill requested removal of early spike/benchmark work and use of `google/gemma-4-12b` as the current local test model. Full-system benchmarking is deferred to Stage 11. Known risk to revisit: Smart Turn may end a turn during a hesitation and may not recognize some short commands; validate with the integrated voice pipeline before final tuning. | |
| 2026-10-29 | 1 | Approved by Bill. | Bill / 2026-10-29 |
