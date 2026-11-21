import { useState } from "react";

import { Button } from "@pipecat-ai/voice-ui-kit";

import type { Chat } from "./useChat";
import type { ProjectInfo } from "./types";

/**
 * Saved chats (projects and contexts): the open chat's name, New chat, and a list of projects
 * with their chats. Archiving hides a chat or project; the Archived view brings it back.
 */
export function ChatsBar({ chat, busy }: { chat: Chat; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const [archived, setArchived] = useState(false);
  const current = chat.current;
  const project = chat.projects.find((p) => p.id === current?.project_id);

  const ask = (question: string, value = "") => {
    const answer = window.prompt(question, value);
    return answer && answer.trim() ? answer.trim() : null;
  };

  const openChat = (id: number) => {
    chat.openContext(id);
    setOpen(false);
  };

  const shown: ProjectInfo[] = archived
    ? chat.projects
        .map((p) => (p.archived ? p : { ...p, contexts: p.contexts.filter((c) => c.archived) }))
        .filter((p) => p.archived || p.contexts.length > 0)
    : chat.projects.filter((p) => !p.archived).map((p) => ({ ...p, contexts: p.contexts.filter((c) => !c.archived) }));

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="min-w-0 flex-1 truncate rounded-md px-2 py-1 text-left text-sm hover:bg-black/5 dark:hover:bg-white/10"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          title="Your saved chats"
        >
          <span className="opacity-60">{project?.name ?? "General"} / </span>
          <span className="font-medium">{current?.name ?? "New chat"}</span>
          <span className="ml-1 opacity-60">{open ? "▴" : "▾"}</span>
        </button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => chat.newChat(current?.project_id)}>
          New chat
        </Button>
      </div>
      {open && (
        <div className="max-h-80 overflow-y-auto rounded-lg border border-current/15 p-2 text-sm">
          {shown.length === 0 && <p className="px-2 py-3 opacity-60">{archived ? "Nothing archived." : "No saved chats yet."}</p>}
          <ul className="flex flex-col gap-3">
            {shown.map((p) => (
              <li key={p.id}>
                <div className="flex items-center gap-1">
                  <span className="flex-1 truncate font-semibold">{p.name}</span>
                  {archived ? (
                    p.archived && (
                      <Button size="sm" variant="ghost" onClick={() => chat.archiveProject(p.id, false)}>
                        Restore
                      </Button>
                    )
                  ) : (
                    <>
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => { chat.newChat(p.id); setOpen(false); }}>
                        + Chat
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => { const name = ask("Rename project", p.name); if (name) chat.renameProject(p.id, name); }}>
                        Rename
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => { if (window.confirm(`Archive “${p.name}” and its chats?`)) chat.archiveProject(p.id); }}>
                        Archive
                      </Button>
                    </>
                  )}
                </div>
                <ul className="mt-1 flex flex-col">
                  {p.contexts.map((c) => (
                    <li key={c.id} className="flex items-center gap-1">
                      <button
                        type="button"
                        disabled={busy && !archived}
                        className={`min-w-0 flex-1 truncate rounded-md px-2 py-1 text-left hover:bg-black/5 disabled:opacity-50 dark:hover:bg-white/10 ${c.id === current?.id ? "bg-black/5 font-medium dark:bg-white/10" : ""}`}
                        onClick={() => {
                          if (archived) chat.archiveContext(c.id, false);
                          openChat(c.id);
                        }}
                        title={archived ? "Restore and open" : "Open"}
                      >
                        {c.name}
                      </button>
                      {!archived && (
                        <>
                          <Button size="sm" variant="ghost" onClick={() => { const name = ask("Rename chat", c.name); if (name) chat.renameContext(c.id, name); }}>
                            Rename
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => chat.archiveContext(c.id)}>
                            Archive
                          </Button>
                        </>
                      )}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
          <div className="mt-3 flex items-center gap-2 border-t border-current/10 pt-2">
            {!archived && (
              <Button size="sm" variant="outline" onClick={() => { const name = ask("New project name"); if (name) { chat.newProject(name); setOpen(false); } }}>
                New project
              </Button>
            )}
            <span className="flex-1" />
            <Button size="sm" variant="ghost" onClick={() => setArchived((value) => !value)}>
              {archived ? "Back to chats" : "Archived"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
