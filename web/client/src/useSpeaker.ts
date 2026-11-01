import { useCallback, useEffect, useRef, useState } from "react";

import { SendspinPlayer } from "@sendspin/sendspin-js";

import type { Chat } from "./useChat";

export type Speaker = {
  /** The web interface can relay Sendspin to Music Assistant (music is configured, signed in). */
  available: boolean;
  on: boolean;
  playing: boolean;
  title: string | null;
  error: string | null;
  toggle: () => Promise<void>;
};

function browserName(): string {
  const agent = navigator.userAgent;
  const browser = /Edg\//.test(agent) ? "Edge" : /Chrome\//.test(agent) ? "Chrome" : /Firefox\//.test(agent) ? "Firefox" : "Safari";
  const device = /iPhone/.test(agent) ? "iPhone" : /iPad/.test(agent) ? "iPad" : /Android/.test(agent) ? "Android" : /Mac/.test(agent) ? "Mac" : "PC";
  return `${browser} on ${device}`;
}

/**
 * "Play music here": this browser as a Music Assistant speaker over Sendspin (relayed by the web interface at
 * /sendspin). Its player id goes to the assistant, so music asked for from here with no place named plays here.
 */
export function useSpeaker(chat: Chat): Speaker {
  const player = useRef<SendspinPlayer | null>(null);
  const [available, setAvailable] = useState(false);
  const [on, setOn] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [title, setTitle] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetch("/api/music", { credentials: "same-origin" })
      .then((response) => (response.ok ? response.json() : { speaker: false }))
      .then((body: { speaker: boolean }) => setAvailable(body.speaker))
      .catch(() => setAvailable(false));
  }, []);

  // The chat socket reconnects on its own; tell the assistant about this speaker again each time.
  useEffect(() => {
    if (chat.status === "connected" && player.current?.isConnected) {
      chat.send({ type: "music_speaker", player_id: player.current.clientId });
    }
  }, [chat.status, chat.send]);

  useEffect(() => () => player.current?.disconnect("shutdown"), []);

  const toggle = useCallback(async () => {
    setError(null);
    if (player.current) {
      player.current.disconnect("user_request");
      player.current = null;
      setOn(false);
      setPlaying(false);
      setTitle(null);
      chat.send({ type: "music_speaker", player_id: null });
      return;
    }
    const created = new SendspinPlayer({
      baseUrl: location.origin, // the web interface relays /sendspin to Music Assistant
      clientName: `Victoria (${browserName()})`,
      productName: "Victoria",
      correctionMode: "quality-local",
      onStateChange: (state) => {
        setPlaying(state.isPlaying);
        const meta = state.serverState?.metadata;
        setTitle(meta?.title ? [meta.title, meta.artist].filter(Boolean).join(" · ") : null);
      },
    });
    try {
      await created.unlock(); // first, inside the tap, so the browser allows audio
      await created.connect();
    } catch {
      setError("Couldn't reach the music server.");
      created.disconnect("shutdown");
      return;
    }
    player.current = created;
    setOn(true);
    chat.send({ type: "music_speaker", player_id: created.clientId });
  }, [chat.send]);

  return { available, on, playing, title, error, toggle };
}
