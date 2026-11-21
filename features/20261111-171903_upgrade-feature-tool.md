# Upgrade tool for feature work

- Status: ready for review
- Phase: current
- Origin: Bill, 2026-11-11 ("next tool we need is upgrade, this tool should be able to ask an agent to add a new feature to victoria")
- Created: 2026-11-11 (UTC)

## Done when

- [x] `upgrade` is available to the fast model from `tools.yaml` and hands a requested Victoria feature to Hermes.
- [x] The work is directed to a dedicated clone and feature branch; Hermes cannot access the live checkout, and the tool instructs it not to push or merge.
- [x] Task creation, result, failure, and cancellation use the existing task dashboard and notification flow.
- [x] Hermes is instructed to report the branch and commit, and summarize changes and tests for Bill to review.
- [x] Tests cover tool routing, task creation, unavailable Hermes, and workspace safety instructions.
- [x] Setup and use are documented.

## Blocking decisions

None. Settled 2026-11-11: use Hermes, a dedicated clean checkout/workspace, an isolated feature branch, and Bill's review before merge. The workspace must not contain secrets, local config, database, logs, or runtime data.

## Evidence and notes

Bill selected Hermes, isolated branch/worktree, and Bill review before merge on 2026-11-11. He approved the dedicated clean checkout approach on 2026-11-11.

Bill accepted keeping the separate `upgrade` tool even though the fast-model tool set becomes 17 active tools (2026-11-11).

Evidence:

- `uv run pytest -q`: 399 passed, 3 skipped. Includes routing to a stand-in Hermes gateway through `AgentRunner`, task completion/dashboard event, setup failures, and required branch/workspace instructions.
- `LMS_API_KEY=test uv run assistant config check --config-dir config/examples`: configuration valid. Expected runtime stubs: Home Assistant, announcements, and music, because those example services aren't configured.
- `LMS_API_KEY=test uv run assistant tools status --config-dir config/examples`: `upgrade` ready; 17 tools active.
- `LMS_API_KEY=test uv run assistant tools status`: Bill's local tools config also reports `upgrade` ready and 17 tools active.
- `git diff --check`: passed.
- No live code-writing request was sent. After Hermes has the isolated clone set up, demo by asking “Add a feature to Victoria that …”; verify feature branch and commit in `~/Services/hermes/work/victoria-voice-assistant`, review them, then use the local-fetch command in `docs/hermes.md`.
