import { useEffect, useState } from "react";

import { Button } from "@pipecat-ai/voice-ui-kit";

import { NotificationCard } from "./NotificationCard";
import { currentSubscription, pushState, togglePush, type PushState } from "./push";
import type { Chat } from "./useChat";

/** Unread notifications, newest first, and the phone alerts switch. */
export function AlertsView({ chat }: { chat: Chat }) {
  const [push, setPush] = useState<PushState>({ available: false, subscribed: false, hint: null });
  const [hint, setHint] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    void pushState(chat.pushKey).then(async (state) => {
      if (!current) return;
      setPush(state);
      // Keep the assistant's copy of this browser's subscription fresh.
      if (state.subscribed) {
        const subscription = await currentSubscription();
        if (subscription) chat.send({ type: "push_subscribe", subscription: subscription.toJSON() });
      }
    });
    return () => {
      current = false;
    };
  }, [chat.pushKey, chat.send]);

  const toggle = async () => {
    if (!chat.pushKey) return;
    setHint(null);
    try {
      const result = await togglePush(chat.pushKey);
      if (result.subscribe) chat.send({ type: "push_subscribe", subscription: result.subscribe });
      if (result.unsubscribe) chat.send({ type: "push_unsubscribe", endpoint: result.unsubscribe });
      if (result.hint) setHint(result.hint);
      setPush(await pushState(chat.pushKey));
    } catch {
      setHint("Couldn't turn on alerts in this browser.");
    }
  };

  return (
    <div className="flex flex-col gap-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex-1 text-sm opacity-70">
          {chat.notifications.length ? `${chat.notifications.length} unread` : "No new notifications."}
        </span>
        {push.available && (
          <Button size="sm" variant={push.subscribed ? "active" : "outline"} onClick={() => void toggle()}>
            {push.subscribed ? "Phone alerts on" : "Phone alerts"}
          </Button>
        )}
        {chat.notifications.length > 0 && (
          <Button size="sm" variant="ghost" onClick={() => chat.send({ type: "notification_ack", ids: "all" })}>
            Clear all
          </Button>
        )}
      </div>
      {(hint ?? push.hint) && <p className="text-sm opacity-70">{hint ?? push.hint}</p>}
      <ul className="flex flex-col gap-2">
        {chat.notifications.map((item) => (
          <li key={item.id}>
            <NotificationCard
              item={item}
              chat={chat}
              inList
              meta={`${item.source} · ${new Date(item.created_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`}
            />
          </li>
        ))}
      </ul>
    </div>
  );
}
