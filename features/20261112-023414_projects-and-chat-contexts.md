# Projects and chat contexts

- Status: ready for review
- Phase: current
- Origin: Bill, 2026-11-11 ("when I refresh the dashboard here is no retention of chat, I tihnk we need a projects feature, a project is a selection of contexts"; "context is a sigle chat transcript, we could have multiple in a project, creating a new context creates a new chat")
- Created: 2026-11-12 (UTC)
- Started: 2026-11-12, branch `feature-upgrade-feature-tool` (Bill: "Yes, accept all seven")

## Done when

- [x] A chat in the dashboard is a **context**: one saved chat transcript. Refreshing the page, or opening the dashboard on another device, brings it back, and the assistant carries on the same conversation (not just the text on screen).
- [x] A **project** groups contexts. The dashboard lists projects and their contexts, opens any of them, and "New chat" creates a new, empty context in the open project.
- [x] Contexts and projects can be renamed and archived.
- [x] Tests: saving and restoring a context (transcript and the model's conversation), switching contexts, a new chat starting empty, archive. Live check: chat, refresh, carry on; two contexts in one project kept apart.

## Blocking decisions

None. Settled 2026-11-12: all seven as proposed (Bill: "Yes, accept all seven").

1. **Where it's kept.** Proposal: SQLite, new tables `projects`, `contexts` and `context_messages` (each user message and reply in order, with time). Everything kept, like the command history. The command history keeps saving every request too, now with the context it was in.
2. **What the model gets back.** Proposal: when a context is opened, its user messages and replies are put back into the conversation, but not tool calls or tool results. So an old weather or clock answer is never repeated (the same rule as [forget-out-of-date-answers](20261111-154338_forget-out-of-date-answers.md)). A long context sends the model only its recent turns, as now. The screen shows the whole transcript.
3. **Projects to start with.** Proposal: one project, "General", holds every context until you make others. A context's name is taken from its first request ("Carriage house shelving"), and you can rename it. The dashboard reopens the context you last had open.
4. **Who sees what.** Proposal: the dashboard has one household password and no per-person accounts, so everyone on the dashboard sees every project and context. Per-person projects would need sign-in for each person, which is a separate feature.
5. **Voice.** Proposal: typed chat only in this feature. Satellites keep their own short conversation, outside projects. Browser voice on the dashboard stays as it is. Saying "switch to the carriage house project", or speaking into the open context, would be a later feature.
6. **What a project holds.** Proposal: only its contexts, for now. Project-wide instructions or sources (a project's Reminders list, its documents, its agent tasks) are a later feature, once projects exist.
7. **Removing things.** Proposal: archive, not delete. An archived context or project leaves the list but stays in the database, and an "Archived" view can reopen it. This is the same choice as the history: keep everything and watch how it grows.

## Notes

- Why it's lost today: the web client keeps the messages in its page state only, and the service starts each chat connection with an empty conversation (`chat/session.py`, `LLMContext` with only the system prompt). Nothing ties a connection to an earlier conversation.
- The browser already has a stable id (`client_id`, sent in `hello`). The dashboard would use it to reopen the last context.
- The document panel (right half) stays as it is. Whether a project keeps its own documents is part of decision 6.
- No satellites involved, so this is current-phase work.

## Evidence

Built 2026-11-12:

- Tables (migration `0010_projects.sql`): `projects`, `contexts`, `context_messages` (each request and reply in order; a request also keeps the names of the tools it called), `client_contexts` (which context each browser had open), and `history.context_id`.
- `projects.py`: `ProjectStore` (General made on first use, contexts named from their first request, listing with archived ones flagged and empty ones hidden, archive and unarchive, never delete) and `Transcript` (saves a session's requests, tool names and replies; a new chat's context is created with its first request).
- `conversation()` gives the model back the last 20 requests and replies, with no tool calls or results, leaving out any exchange whose request called a tool with `forget_after_s`, and starting on a request.
- `chat/contexts.py` (`ChatContexts`), wired into typed chat only (`chat/session.py`):
  - `hello` reopens the browser's last context, else the latest one, else a new chat.
  - `open_context` restores the transcript to the screen and the conversation to the model, and clears any open question and forget marks.
  - `new_chat` empties the conversation.
  - Rename and archive for contexts and projects; `new_project` opens a new chat in it. Archiving the open chat or project starts a new chat.
  - No switching while a request is running ("Wait for the reply first.").
  - Changes go to every open client.
  - Message shapes are checked in `chat/serializer.py`.
- Saving hooks: requests in `TurnPreprocessor._handled` (before the history, so the history gets the new context's id), tool names in `ToolRuntime`, replies in `_reply`, `HistoryTap`, and agent results. Satellites and browser voice don't save (decision 5).
- Web client: `ChatsBar.tsx` in the Chat tab: the open chat's project and name, New chat, and a list of projects and their chats with + Chat, Rename, Archive, New project and an Archived view (restore). `useChat.ts` handles `projects` and `context` (a context with its messages replaces the log). `npm run build` passes (typecheck included).
- Docs: architecture v2.15 (§4.1 "Saved chats: projects and contexts", §11), new `docs/projects.md`.
- Tests: `tests/test_projects.py` (37). `uv run pytest -q`: 519 passed, 3 skipped (the count includes the other session's groceries tests).

Live, typed chat over the service's own `/ws` after restarting both services, 2026-11-12 ~02:50 UTC, one browser id throughout:

- New chat: "My canoe is called Bluebird." → saved as context 8, named "My canoe is called Bluebird".
- Reconnected with the same browser id (a refresh): context 8 came back with its transcript; "What is my canoe called?" → "Your canoe is named Bluebird."
- New chat: "What is my canoe called? …" → "I don't know what your canoe is called." (context 9, kept apart; both listed under General).
- Reopened context 8: all four messages shown; "And what was it called again?" → "It is named Bluebird."
- Renamed 9 to "Canoe, second chat" and archived both. In the database both are archived, and history rows 102-105 carry context ids 8 and 9.
- A first visit from a new browser id opened the most recently used context (another device carries on the latest chat).
- Two earlier probe chats (6, 7) were archived after the check.

Found on the way:

- A first message worded "Remember this: … Just say okay." got two empty replies from the dev model (`empty_reply`). That's the small model and the phrasing, not this feature: a plain "My canoe is called Bluebird." worked in a new chat. Other sessions were using the model at the same moment.
- The other session working on groceries restarted the service twice during the checks (close code 1012). The checks were rerun.

Not verified:

- The new chats bar hasn't been seen in a browser. The browser pane doesn't trust the dashboard's self-signed certificate, so only the protocol was checked live. Bill to look at the Chat tab: the chat's name, New chat, the list, rename, archive and Archived.
- Opening the dashboard on a second device (the live check used a second browser id over the same protocol).
