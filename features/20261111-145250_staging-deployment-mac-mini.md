# Staging deployment on the Mac mini

- Status: open
- Phase: staging
- Origin: Bill, 2026-11-11
- Created: 2026-11-11 (UTC)

The assistant installed on the Mac mini (M4 Pro, 64 GB) as a staging deployment. Every other staging feature runs on it.

## Done when

- [ ] The repository, `config/`, secrets (Keychain and `config/.env`) and the database are set up on the Mac mini.
- [ ] `config/models.yaml` there is sized for 64 GB (model choices, context lengths, pinning), not copied from the dev machine's small-memory setup.
- [ ] Every LaunchAgent is installed and healthy: assistant, web interface, SearXNG, Hermes, backup, and the decider (left off on the M2 because it keeps a model loaded).
- [ ] The health strip is green for everything configured, and `assistant tools test` runs.
- [ ] The satellites connect to the Mac mini.

## Blocking decisions

- When the Mac mini is available.
- Whether staging uses a copy of the dev database or starts empty.

## Evidence and notes

Not started.
