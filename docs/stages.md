# Stage tracker

Rules: see implementation plan §1. Only Bill sets a stage to `approved`.

| # | Stage | Status | Started | Ready for review | Approved by / date |
|---|---|---|---|---|---|
| 0 | Tracker, instructions, repo | approved | 2026-10-25 | 2026-10-25 | Bill / 2026-10-25 |
| 1 | Pipeline risk experiments | in progress | 2026-10-25 | | |
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

## Stage 0: Tracker, instructions, repo
- Status: ready for review
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

## Stage 1: Pipeline risk experiments
- Status: in progress
- Done criteria:
  - [ ] `docs/spikes-report.md` answers each question with measurements and a recommendation.
  - [ ] Any needed architecture changes are proposed in the decisions log.
- Evidence: (test output, measurements, demo steps for Bill)
- Bill's review notes:

## Decisions log
| Date | Stage | Proposal or decision | Bill's answer |
|---|---|---|---|
| 2026-10-25 | 0 | Tool inventory on the Mac mini: git, uv, Node 23, LM Studio (`lms`), launchd, ffmpeg all present; system Python is 3.14 (uv will provision 3.12 per the ground rules). `mkcert` and `tailscale` are both missing — the Stage 5 HTTPS blocking decision needs one of these installed before that stage. No action taken now. | |
| 2026-10-25 | 0 | Approved by Bill. | Bill / 2026-10-25 |
| 2026-10-25 | 0 | Dev happens on an M2 Mac (32 GB RAM, macOS 26.7), not the target Mac mini; local LM Studio is running with 10 models. Ground rules updated (plan §2, AGENTS.md, copilot-instructions): every Python environment is a uv-managed virtualenv (`.venv`, interpreter pinned to 3.12); never install into system Python. Memory/latency measurements on the M2 are for design validation only. | Accepted by Bill (requested) |
| 2026-10-25 | 1 | Spike model candidates from the local LM Studio library: `qwen/qwen3-coder-30b` (MoE, 17 GB), `mistralai/devstral-small-2-2512` (24 B), `cbt-gemma-2-9b-mlx` / `llama-3.2-3b-instruct` (small fallback). To be confirmed by the benchmark itself. | |
