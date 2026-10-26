# Copilot instructions — Home Voice Assistant

A private, low-latency voice assistant for the household (replaces Alexa), built on a Mac mini (M4 Pro, 64 GB, macOS 26). Pipeline: browser/ESP32 client → Silero VAD → Smart Turn → Whisper (MLX) → local fast LLM (LM Studio) with tool calling → Kokoro TTS. Larger jobs hand off to named agents (Claude pool, Hermes).

**Read `docs/stages.md` first, every session, before writing any code.**

## Approval gate (binding)

Work is organized into numbered stages in `docs/stages.md`, tracked per `docs/implementation_plan.md` §1.

- Work **only** on the stage marked `in progress`. If none is `in progress`, you may start the next stage only if the previous one is `approved`.
- **One stage at a time.** Never write code, scaffolding, or stubs for a later stage, even "harmless" prep. If you need something from a later stage now, record it in the decisions log in `docs/stages.md` and ask Bill.
- When the stage's done criteria are met: set the stage to `ready for review` in `docs/stages.md`, fill in its Evidence section (test output, measurements, demo steps), then **stop and wait**.
- **Never set a stage to `approved`.** Only Bill does that. If Bill writes `changes requested`, fix his notes and return the stage to `ready for review`.
- Never edit `docs/voice-pipeline-architecture.md` unless Bill asks. If you find the architecture wrong or incomplete, propose the change in the decisions log and wait.
- If a stage has a blocking decision that is unanswered, ask before writing code.
- Branch per stage (`stage-NN-short-name`); merge to `main` and tag `stage-NN-approved` only after Bill approves.

## Ground rules

- Python 3.12+ with **uv**, always in a **uv-managed virtualenv** (`.venv` from `pyproject.toml`, pinned interpreter, locked deps). Never the system Python; use `uv run`. Web client: **TypeScript + Vite**.
- Native processes under **launchd**; no Docker. Apple `container` only for agent sandboxes (stage 8+).
- Dev box is an **M2 Mac (32 GB)** with local test LLMs in LM Studio; the deployment target is a Mac mini M4 Pro (64 GB). M2 numbers are for design validation only.
- **Pipecat built-ins first** (see architecture §11: `WhisperSTTServiceMLX`, `KokoroTTSService`, Silero VAD, Smart Turn, OpenAI LLM service, `LLMSwitcher`, `MCPClient`, SmallWebRTC). Replace a built-in only with measured evidence, recorded in the decisions log.
- All config lives in **`config/`** (`models.yaml`, `tools.yaml`, `agents.yaml`, `config/searxng/`, `config/.env`). Never in the repo root. Service default is `config/`, overridable by `ASSISTANT_CONFIG_DIR`. Committed examples go in `config/examples/`.
- Secrets only from environment variables (`config/.env`, git-ignored).
- Config is the interface: anything the architecture places in `models.yaml`/`tools.yaml`/`agents.yaml` is data, not code.
- Each stage adds automated tests for what it builds; hardware/model-dependent tests are skippable in quick runs.
- From stage 3 on, every request logs **per-stage latency** (end of speech → reply start; target ~1.5 s on the tool path). Measure, don't assume.
- Keep the fast model's active tool set under ~15 tools.

## Docs

| File | Role |
|---|---|
| `docs/voice-pipeline-architecture.md` | What to build and why. Source of truth for design. Do not edit without Bill. |
| `docs/implementation_plan.md` | Build order, per-stage scope/done criteria/blocking decisions. |
| `docs/stages.md` | Live tracker: current stage, evidence, decisions log. **Read first.** |
| `docs/spikes-report.md` | Experiment results (stage 1, stage 12). |

If the architecture and the plan disagree, stop and ask.

## Building guidance

- The library landscape (Pipecat, MLX Whisper, Smart Turn, SmallWebRTC, SearXNG, Music Assistant, MCP/A2A SDKs) moves fast. **Before introducing or changing a library, API, or architectural pattern, search the web/docs for the current recommended usage** rather than relying on memory. Pin whatever you land on.
- Keep it simple. Prefer the smallest implementation that meets the stage's done criteria; complexity is a defect until the measurements say otherwise.
- Web searches, tool calls, and cloud calls belong to the configured components — don't ad-hoc-call external services from code that the architecture assigns to a specific built-in or tool.
- When a spike or implementation contradicts an architecture assumption (latency figures, library capabilities, protocol behaviour), record the finding in the decisions log in `docs/stages.md` with the measurement, and propose the change there instead of editing the architecture.
