import { useState } from "react";

import { Button, Collapsible, CollapsibleContent, CollapsibleTrigger, Input } from "@pipecat-ai/voice-ui-kit";

import type { Chat } from "./useChat";
import type { NotificationInfo } from "./types";

// Half-typed answers, by task, kept when a card is redrawn (the dashboard updates live).
const drafts = new Map<string, string>();

/** An agent's question: answered here, or by voice or chat ("Tell Hermes ..."). */
export function AnswerForm({ agent, taskId, chat }: { agent: string; taskId: string; chat: Chat }) {
  const [text, setText] = useState(drafts.get(taskId) ?? "");
  const [sent, setSent] = useState(false);
  return (
    <form
      className="mt-2 flex gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        const answer = text.trim();
        if (!answer) return;
        chat.send({ type: "agent_answer", task_id: taskId, text: answer });
        chat.append({ kind: "you", text: answer });
        drafts.delete(taskId);
        setSent(true);
      }}
    >
      <Input
        className="flex-1"
        placeholder={`Answer ${agent}…`}
        value={text}
        disabled={sent}
        onChange={(event) => {
          setText(event.target.value);
          drafts.set(taskId, event.target.value);
        }}
      />
      <Button type="submit" disabled={sent || !text.trim()}>
        {sent ? "Sent" : "Answer"}
      </Button>
    </form>
  );
}

/**
 * A notification: a question with its answer box, or a result whose full text opens on tap. In the chat
 * log opening it counts as read; in the Alerts list (`inList`) it stays until "Mark read", so it doesn't
 * vanish from under you.
 */
export function NotificationCard({ item, chat, meta, inList = false }: { item: NotificationInfo; chat: Chat; meta?: string; inList?: boolean }) {
  const read = () => chat.send({ type: "notification_ack", ids: [item.id] });
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-current/20 px-3 py-2">
      {meta && <p className="mb-1 text-xs opacity-60">{meta}</p>}
      {item.kind === "question" && item.task_id ? (
        <>
          <p>
            <strong>{item.headline}</strong> {item.full}
          </p>
          <AnswerForm agent={item.source} taskId={item.task_id} chat={chat} />
        </>
      ) : (
        <Collapsible
          open={open}
          onOpenChange={(next) => {
            setOpen(next);
            if (next && !inList) read();
          }}
        >
          <CollapsibleTrigger className="w-full cursor-pointer text-left font-medium">
            {open ? "▾" : "▸"} {item.headline}
          </CollapsibleTrigger>
          <CollapsibleContent>
            <p className="mt-2 whitespace-pre-wrap">{item.full}</p>
            {inList && (
              <Button size="sm" variant="outline" className="mt-2" onClick={read}>
                Mark read
              </Button>
            )}
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}
