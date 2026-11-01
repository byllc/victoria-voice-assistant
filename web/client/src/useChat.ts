import { useCallback, useEffect, useRef, useState } from "react";

import { clientId } from "./push";
import { beep } from "./sound";
import type { Dashboard, LogEntry, NewLogEntry, NotificationInfo, ServerMessage, TimerInfo } from "./types";

export type Timer = { name: string; endsAt: number; state: TimerInfo["state"] };
export type Ringing = { names: string[]; text: string } | null;
export type ChatStatus = "connecting" | "connected" | "thinking" | "disconnected" | "signed out";

let nextId = 1;

/**
 * The chat websocket (`/ws`, proxied by the web interface): typed messages and replies, timers,
 * notifications, the task dashboard and push settings. Voice sessions send the same message shapes
 * (`handleServerMessage`), and their transcripts join the same conversation log.
 */
export function useChat(onSignedOut: () => void) {
  const socket = useRef<WebSocket | null>(null);
  const pendingReply = useRef<number | null>(null);
  const voiceReply = useRef<number | null>(null);
  const chime = useRef<number | null>(null);
  const [status, setStatus] = useState<ChatStatus>("connecting");
  const [log, setLog] = useState<LogEntry[]>([]);
  const [timers, setTimers] = useState<Timer[]>([]);
  const [ringing, setRinging] = useState<Ringing>(null);
  const [notifications, setNotifications] = useState<NotificationInfo[]>([]);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [pushKey, setPushKey] = useState<string | null>(null);

  const append = useCallback((entry: NewLogEntry): number => {
    const id = nextId++;
    setLog((current) => [...current, { ...entry, id } as LogEntry]);
    return id;
  }, []);

  const appendToEntry = useCallback((id: number, text: string, separator = "") => {
    setLog((current) =>
      current.map((entry) =>
        entry.id === id && entry.kind === "victoria"
          ? { ...entry, text: entry.text ? `${entry.text}${separator}${text}` : text }
          : entry,
      ),
    );
  }, []);

  const stopRinging = useCallback(() => {
    setRinging(null);
    if (chime.current !== null) window.clearInterval(chime.current);
    chime.current = null;
  }, []);

  const handleServerMessage = useCallback(
    (message: ServerMessage) => {
      switch (message.type) {
        case "token":
          if (pendingReply.current === null) pendingReply.current = append({ kind: "victoria", text: "" });
          appendToEntry(pendingReply.current, message.text);
          break;
        case "done":
          pendingReply.current = null;
          setStatus("connected");
          break;
        case "error":
          pendingReply.current = null;
          append({ kind: "victoria", text: `Sorry, something went wrong${message.message ? `: ${message.message}` : ""}.` });
          setStatus("connected");
          break;
        case "notice":
          append({ kind: "notice", title: message.title, text: message.text });
          break;
        case "timers": {
          const now = Date.now();
          const next = message.timers.map((t) => ({ name: t.name, endsAt: now + t.remaining_s * 1000, state: t.state }));
          setTimers(next);
          // The server is the source of truth: nothing ringing there means stop here too.
          if (!next.some((t) => t.state === "ringing")) stopRinging();
          break;
        }
        case "timer_ring": {
          const names = message.names;
          const label = names.length === 1 ? `Your ${names[0]} timer is done.` : `Your ${names.join(" and ")} timers are done.`;
          setRinging({ names, text: message.text ?? label });
          if (message.text) append({ kind: "victoria", text: message.text });
          if (chime.current === null) {
            beep();
            chime.current = window.setInterval(beep, 2000);
          }
          break;
        }
        case "timer_ring_stop":
          setTimers((current) => {
            if (!current.some((t) => t.state === "ringing" && !message.names.includes(t.name))) stopRinging();
            return current;
          });
          break;
        case "notifications":
          setNotifications(message.items);
          break;
        case "announce":
          if (message.sound) beep();
          // A speaker in the room says it, and the voice transcript shows it; otherwise show it here.
          // (A single item's card already carries its headline.)
          const sameAsCard = message.items.length === 1 && message.items[0].headline === message.text;
          if (!message.spoken && !sameAsCard) append({ kind: "victoria", text: message.text });
          for (const item of message.items) append({ kind: "notification", item });
          break;
        case "dashboard":
          setDashboard({ tasks: message.tasks, summary: message.summary });
          break;
        case "push_config":
          setPushKey(message.public_key);
          break;
      }
    },
    [append, appendToEntry, stopRinging],
  );

  useEffect(() => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    socket.current = ws;
    setStatus("connecting");
    ws.onopen = () => {
      setStatus("connected");
      ws.send(JSON.stringify({ type: "hello", client_id: clientId }));
    };
    ws.onmessage = (event: MessageEvent<string>) => handleServerMessage(JSON.parse(event.data) as ServerMessage);
    ws.onclose = (event) => {
      if (socket.current !== ws) return;
      socket.current = null;
      stopRinging(); // never keep beeping for a timer this page can no longer hear about
      if (event.code === 1008) {
        setStatus("signed out");
        onSignedOut();
      } else setStatus("disconnected");
    };
    return () => {
      socket.current = null;
      ws.close();
    };
  }, [handleServerMessage, onSignedOut, stopRinging]);

  const send = useCallback((message: Record<string, unknown>) => {
    socket.current?.send(JSON.stringify(message));
  }, []);

  const sendMessage = useCallback(
    (text: string) => {
      if (!socket.current || socket.current.readyState !== WebSocket.OPEN) return false;
      append({ kind: "you", text });
      setStatus("thinking");
      send({ type: "user_message", text });
      return true;
    },
    [append, send],
  );

  // Voice transcripts: what was heard, and the bot's sentences (grouped into one reply per turn).
  const addHeard = useCallback(
    (text: string) => {
      voiceReply.current = null;
      append({ kind: "you", text, heard: true });
    },
    [append],
  );
  const addBotSentence = useCallback(
    (text: string, newTurn: boolean) => {
      if (newTurn || voiceReply.current === null) voiceReply.current = append({ kind: "victoria", text: "" });
      appendToEntry(voiceReply.current, text, " ");
    },
    [append, appendToEntry],
  );

  const clearLog = useCallback(() => setLog([]), []);

  return {
    status,
    log,
    timers,
    ringing,
    notifications,
    dashboard,
    pushKey,
    send,
    sendMessage,
    addHeard,
    addBotSentence,
    append,
    clearLog,
    stopRinging,
    handleServerMessage,
  };
}

export type Chat = ReturnType<typeof useChat>;
