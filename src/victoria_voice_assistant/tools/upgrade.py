"""Delegate requested Victoria feature work to the isolated Hermes coding workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING

from victoria_voice_assistant.tools.outcome import ToolOutcome

if TYPE_CHECKING:
    from victoria_voice_assistant.agents.runner import AgentRunner

WORKSPACE = "/work/victoria-voice-assistant"


def coding_request(feature: str) -> str:
    return f"""Implement this requested feature in Victoria Voice Assistant:

{feature}

Work only in the dedicated checkout at {WORKSPACE}. Before editing, verify it is the Victoria Voice Assistant
repository on a clean `main` branch. Ensure it contains no local secrets (`config/.env`), user-specific config
(`config/tools.yaml`, `config/models.yaml`, or `config/agents.yaml`), database, logs, or runtime data; tracked
files such as `config/examples/` are allowed. If any check fails, stop without changing files and report why.
Create a new `feature-<short-slug>` branch from `main`; never edit `main`. Follow the repository's AGENTS.md
and feature approval rules. Implement only the requested feature and run its relevant tests.
Commit completed changes on the feature branch with a descriptive message and this exact trailer:
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Never push, merge, or approve the feature. Leave the branch for Bill to review. Report the branch,
commit, summary, changed files, tests and results, and any remaining decisions."""


async def upgrade_feature(request: str, device: str, agents: AgentRunner | None) -> ToolOutcome:
    feature = request.strip()
    if not feature:
        return ToolOutcome(
            result={"error": "feature request is empty"},
            speak="Tell me what feature to add.",
            error=True,
        )
    if agents is None:
        return ToolOutcome(
            result={"error": "agent runner unavailable"},
            speak="The upgrade agent isn't set up yet.",
            chat="Start the assistant with a configured Hermes harness and isolated checkout at "
            f"`{WORKSPACE}`. Setup steps: `docs/hermes.md`.",
            chat_title="upgrade: setup",
            error=True,
        )
    hermes = agents.find("hermes")
    if hermes is None or hermes.kind != "harness":
        return ToolOutcome(
            result={"error": "Hermes harness unavailable"},
            speak="The upgrade agent isn't set up yet.",
            chat="Configure `hermes` as a `harness` agent and provide its isolated checkout at "
            f"`{WORKSPACE}`. Setup steps: `docs/hermes.md`.",
            chat_title="upgrade: setup",
            error=True,
        )
    return await agents.ask("hermes", coding_request(feature), device)
