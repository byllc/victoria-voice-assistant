# AGENTS.md — Home Voice Assistant

Rules for coding agents working in this repository.

## First action every session

**Read `docs/stages.md` before doing anything else.** Work only on the single stage marked `in progress`. If no stage is `in progress`, the next stage may start only if the one before it is `approved`.

## Approval gate

Work proceeds in numbered stages. No stage may start until the previous stage has been approved by Bill.

1. **One stage at a time.** Do not write code, scaffolding, stubs, or "preparation" for a later stage, even if it seems harmless. If something later is needed now, note it in the tracker and ask.
2. **When the done criteria are met:** set the stage to `ready for review` in `docs/stages.md`, fill in the evidence section (test output, measurements, demo steps Bill can follow), then **stop and wait**.
3. **Never mark a stage `approved`.** Only Bill changes a stage to `approved` by editing the tracker and adding his name and date. If Bill marks it `changes requested`, address his notes and return it to `ready for review`.
4. **Don't change the architecture silently.** If a stage shows the architecture is wrong or incomplete, record a proposal in the tracker's decisions log and wait for Bill's answer. Do not edit the architecture document unless asked.
5. **Blocking decisions:** each stage lists the open questions it depends on. If one is unanswered when the stage begins, ask before writing code.
6. **Branch per stage** (`stage-NN-short-name`). Merge to `main` only after approval, then tag `stage-NN-approved`.

Statuses: `not started` → `in progress` → `ready for review` → `approved` (or `changes requested` → `in progress`).

## Ground rules

- **Target:** Mac mini, M4 Pro, 64 GB, macOS 26. **Development happens on an M2 Mac** (32 GB, local test LLMs in LM Studio); M2 measurements validate design, they are not the target's numbers.
- **Python 3.12+, always in a uv-managed virtualenv.** Every Python environment (services, one-off scripts) is a `.venv` created by uv from a `pyproject.toml`, interpreter pinned to 3.12, all dependencies in the lock file. Never install into the system Python; run everything with `uv run`.
- **Pipecat built-ins first.** Use Pipecat's own features wherever one exists (architecture §11). Replace a built-in only with measured evidence, recorded in the decisions log.
- **Secrets** are kept in the macOS Keychain and run from `config/.env`: `assistant secrets set NAME` writes both, installing a service rewrites `config/.env` from the Keychain, and the services read only the environment and `config/.env` (so they start after a reboot). `config/.env` is never committed: git-ignored, refused by `.githooks/pre-commit` (`git config core.hooksPath .githooks`). Never put secrets in YAML or code; config names them as `${NAME}`. See `docs/secrets.md`. (Bill, 2026-11-01.)
- **All configuration lives in `config/`**, never in the repository root:

  ```
  config/
    models.yaml        servers, models, pools, decider, roles
    tools.yaml         tools, built-in service settings, MCP servers, tests
    agents.yaml        agents and relays
    searxng/           SearXNG's settings.yml
  ```

  The service reads `config/` by default; `ASSISTANT_CONFIG_DIR` overrides it. Committed example files go in `config/examples/`.
- **Tests:** each stage adds automated tests for what it builds. Tests needing hardware or a running model are marked so they can be skipped in a quick run.
- **Latency is measured, not assumed.** From stage 3 on, every request logs per-stage timings.
- **Config is the interface.** Behaviour the architecture puts in `models.yaml`, `tools.yaml`, or `agents.yaml` is configured there, not hard-coded.

## Documentation

- `docs/voice-pipeline-architecture.md` — what to build and why (source of truth for design).
- `docs/implementation_plan.md` — order, done criteria, and the gate rules in full.
- `docs/stages.md` — live tracker: current stage, evidence, decisions log.

When the two documents disagree, stop and ask.
