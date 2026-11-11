# AGENTS.md — Home Voice Assistant

Rules for coding agents working in this repository.

## How work is tracked

Work is organised as features, one file each in `features/`, named `<timestamp>_<feature_slug>.md`. The approval gate now applies to each feature, not to numbered stages. `docs/implementation_plan.md` §1 has the full rules.

- Each feature has a `Phase`: `current` (work on the dev machine), `staging` (on the Mac mini, once it is deployed there), or `later` (not scheduled).
- **All performance testing is staging work** (Bill, 2026-11-11). The dev machine's models are sized for its small memory, so don't benchmark, measure latency or load, or judge model choice on it, and don't count its numbers as evidence.
- **Any feature involving more than one satellite is staging work** (Bill, 2026-11-11: "any featrues that involve more than one satellite need to be in staging"). That includes arbitration, fleet checks, multi-room music and streams. Put a new feature of that kind in `staging`.
- Work only on the feature Bill asks for. He may skip a feature or start another at any time.
- Ask before starting a feature whose blocking decisions are unanswered.
- When a feature's criteria are met, set it to `ready for review`, fill in its evidence, and stop. Never set a feature to `approved`; only Bill does.
- "Next feature" means the earliest timestamp in `features/` in the `current` phase not marked `approved` or `skipped`. When Bill asks for it, name the feature and what it is, then wait for his confirmation before starting.
- When Bill asks what is unfinished, give a numbered list of features not marked `approved` or `skipped`, grouped by phase, and wait for him to choose.
- Don't change the architecture silently. Record proposals in the feature file and wait for Bill.

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
- `features/` — remaining work, one file per feature.
- `docs/stages.md` — history of the stage approvals (discontinued 2026-11-05).

When the two documents disagree, stop and ask.
