import { useCallback, useEffect, useRef, useState } from "react";

import { SendspinPlayer } from "@sendspin/sendspin-js";

import type { Chat } from "./useChat";

export type NowPlaying = {
  title: string | null;
  artist: string | null;
  album: string | null;
  artwork: string | null;
  durationMs: number | null;
  // Where the track was, and when (this browser's clock), so the progress bar can run between updates.
  progressMs: number | null;
  progressAt: number;
  speed: number;
};

export type Speaker = {
  /** The web interface can relay Sendspin to Music Assistant (music is configured, signed in). */
  available: boolean;
  on: boolean;
  playing: boolean;
  volume: number;
  now: NowPlaying | null;
  commands: string[];
  error: string | null;
  toggle: () => Promise<void>;
  /** Become a speaker (if not already) and start playing; for the play button. */
  start: () => Promise<void>;
  command: (name: "play" | "pause" | "next" | "previous") => void;
  setVolume: (volume: number) => void;
};

function browserName(): string {
  const agent = navigator.userAgent;
  const browser = /Edg\//.test(agent) ? "Edge" : /Chrome\//.test(agent) ? "Chrome" : /Firefox\//.test(agent) ? "Firefox" : "Safari";
  const device = /iPhone/.test(agent) ? "iPhone" : /iPad/.test(agent) ? "iPad" : /Android/.test(agent) ? "Android" : /Mac/.test(agent) ? "Mac" : "PC";
  return `${browser} on ${device}`;
}

// Artwork comes from Music Assistant over plain HTTP; an HTTPS page loads it through the web interface.
function artworkUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  if (location.protocol === "https:" && url.startsWith("http:")) return `/api/music/artwork?url=${encodeURIComponent(url)}`;
  return url;
}

/**
 * "Play music here": this browser as a Music Assistant speaker over Sendspin (relayed by the web interface at
 * /sendspin). Its id goes to the assistant, so music asked for from here with no place named plays here.
 * Kept above the tabs, so switching tabs doesn't cut the music off.
 */
export function useSpeaker(chat: Chat): Speaker {
  const player = useRef<SendspinPlayer | null>(null);
  // Resolved once the server lists the commands this browser may send: until then the library drops them.
  const ready = useRef<(() => void) | null>(null);
  const wasPlaying = useRef(false);
  const [available, setAvailable] = useState(false);
  const [on, setOn] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [volume, setVolumeState] = useState(100);
  const [now, setNow] = useState<NowPlaying | null>(null);
  const [commands, setCommands] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetch("/api/music", { credentials: "same-origin" })
      .then((response) => (response.ok ? response.json() : { speaker: false }))
      .then((body: { speaker: boolean }) => setAvailable(body.speaker))
      .catch(() => setAvailable(false));
  }, []);

  // The chat socket may reconnect; tell the assistant about this speaker (or that this page can be one) each time.
  useEffect(() => {
    if (chat.status === "connected" && available) {
      chat.send({ type: "music_speaker", player_id: player.current?.isConnected ? player.current.clientId : null, can_play: true });
    }
  }, [chat.status, chat.send, available]);

  useEffect(() => () => player.current?.disconnect("shutdown"), []);

  const toggle = useCallback(async () => {
    setError(null);
    if (player.current) {
      player.current.disconnect("user_request");
      player.current = null;
      setOn(false);
      setPlaying(false);
      setNow(null);
      chat.send({ type: "music_speaker", player_id: null, can_play: true });
      return;
    }
    const created = new SendspinPlayer({
      baseUrl: location.origin, // the web interface relays /sendspin to Music Assistant
      clientName: `Victoria (${browserName()})`,
      productName: "Victoria",
      correctionMode: "quality-local",
      onStateChange: (state) => {
        if (state.serverState?.controller?.supported_commands) {
          ready.current?.();
          ready.current = null;
        }
        const isPlaying = state.groupState?.playback_state === "playing" || state.isPlaying;
        if (isPlaying !== wasPlaying.current) {
          // Started or stopped: measure the running clock from where the track is now, not from the last report.
          wasPlaying.current = isPlaying;
          setNow((current) => {
            if (!current || current.progressMs === null) return current;
            const moved = isPlaying ? 0 : (Date.now() - current.progressAt) * current.speed;
            const at = Math.min(current.progressMs + moved, current.durationMs ?? Infinity);
            return { ...current, progressMs: at, progressAt: Date.now() };
          });
        }
        setPlaying(isPlaying);
        if (state.serverState?.controller?.supported_commands) setCommands(state.serverState.controller.supported_commands);
        if (typeof state.serverState?.controller?.volume === "number") setVolumeState(state.serverState.controller.volume);
        const meta = state.serverState?.metadata;
        if (meta && (meta.progress || meta.title !== undefined)) {
          setNow({
            title: meta.title ?? null,
            artist: meta.artist ?? null,
            album: meta.album ?? null,
            artwork: artworkUrl(meta.artwork_url),
            durationMs: meta.progress?.track_duration ?? null,
            progressMs: meta.progress?.track_progress ?? null,
            progressAt: Date.now(),
            speed: (meta.progress?.playback_speed ?? 1000) / 1000, // reported in thousandths: 1000 is normal speed
          });
        }
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
    chat.send({ type: "music_speaker", player_id: created.clientId, can_play: true });
  }, [chat.send]);

  const start = useCallback(async () => {
    if (!player.current) {
      const settled = new Promise<void>((resolve) => {
        ready.current = resolve;
        window.setTimeout(resolve, 3000); // don't wait forever if no state comes
      });
      await toggle();
      if (!player.current) return;
      await settled;
    }
    player.current?.sendCommand("play", undefined);
  }, [toggle]);

  // Music asked for here while the speaker is off: turn it on, and the assistant plays it once it is listed.
  const starts = useRef(chat.musicStarts);
  useEffect(() => {
    if (chat.musicStarts === starts.current) return;
    starts.current = chat.musicStarts;
    if (!player.current) void toggle();
  }, [chat.musicStarts, toggle]);

  const command = useCallback((name: "play" | "pause" | "next" | "previous") => {
    player.current?.sendCommand(name, undefined);
  }, []);

  const setVolume = useCallback((value: number) => {
    setVolumeState(value);
    player.current?.sendCommand("volume", { volume: value });
  }, []);

  return { available, on, playing, volume, now, commands, error, toggle, start, command, setVolume };
}
