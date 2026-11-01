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

/** Music the assistant is starting here, shown until the music server's own track details arrive. */
export type Starting = { title: string; artist: string | null };

export type Speaker = {
  /** The web interface can relay Sendspin to Music Assistant (music is configured, signed in). */
  available: boolean;
  on: boolean;
  playing: boolean;
  volume: number;
  now: NowPlaying | null;
  starting: Starting | null;
  commands: string[];
  error: string | null;
  /** Connected, but the browser hasn't allowed sound yet (Safari waits for a tap): the play button allows it. */
  locked: boolean;
  toggle: () => Promise<void>;
  /** Become a speaker (if not already) and start playing; for the play button. */
  start: () => Promise<void>;
  /** Allow sound, from a tap. */
  unlock: () => Promise<void>;
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
  const connecting = useRef(false); // one player at a time, however many taps or requests arrive meanwhile
  const [available, setAvailable] = useState(false);
  const [on, setOn] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [volume, setVolumeState] = useState(100);
  const [now, setNow] = useState<NowPlaying | null>(null);
  const [commands, setCommands] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [locked, setLockedState] = useState(false);
  const [starting, setStartingState] = useState<Starting | null>(null);
  // The same, for the player's state handler, with the title showing when it began (to tell the new track's apart).
  const startingRef = useRef<{ previous: string | null } | null>(null);
  const nowTitle = useRef<string | null>(null);
  const setStarting = useCallback((value: Starting | null) => {
    startingRef.current = value ? { previous: nowTitle.current } : null;
    setStartingState(value);
  }, []);
  const isLocked = useRef(false); // the same, for the tap handler
  const setLocked = useCallback((value: boolean) => {
    isLocked.current = value;
    setLockedState(value);
  }, []);

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

  // A browser plays sound only once a tap or key press has allowed it, and Safari holds it back otherwise.
  // So every tap or key press in the page allows it ahead of time: a player made (not connected) and
  // unlocked in that moment, ready for music asked for later by chat or voice; or the connected one unlocked.
  const primed = useRef<SendspinPlayer | null>(null);
  const makePlayer = useCallback(
    (): SendspinPlayer =>
      new SendspinPlayer({
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
          const began = isPlaying && !wasPlaying.current;
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
          // What was starting has arrived: the music server's details for a new track, or playback began.
          if (startingRef.current && ((meta?.title && meta.title !== startingRef.current.previous) || began)) {
            startingRef.current = null;
            setStartingState(null);
          }
          if (meta?.title !== undefined) nowTitle.current = meta.title ?? null;
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
      }),
    [],
  );
  useEffect(() => {
    if (!available) return;
    const allow = () => {
      if (player.current) {
        if (isLocked.current) void player.current.unlock().then(() => setLocked(false), () => undefined);
        return;
      }
      if (!primed.current && !connecting.current) primed.current = makePlayer();
      void primed.current?.unlock().catch(() => undefined); // starts the browser's audio inside this tap
    };
    const events = ["pointerdown", "keydown", "touchend", "click"] as const;
    for (const name of events) document.addEventListener(name, allow, { capture: true });
    return () => {
      for (const name of events) document.removeEventListener(name, allow, { capture: true });
    };
  }, [available, makePlayer]);

  const toggle = useCallback(async () => {
    setError(null);
    if (connecting.current) return;
    if (player.current) {
      player.current.disconnect("user_request");
      player.current = null;
      setOn(false);
      setPlaying(false);
      setNow(null);
      setStarting(null);
      setLocked(false);
      chat.send({ type: "music_speaker", player_id: null, can_play: true });
      return;
    }
    const created = primed.current ?? makePlayer();
    primed.current = null;
    connecting.current = true;
    try {
      // Allowing sound needs a tap; Safari leaves the promise waiting until one comes rather than failing.
      // So connect without waiting for it: music can start, and the play button allows sound.
      let unlocked = false;
      const unlocking = created.unlock().then(() => {
        unlocked = true;
        setLocked(false);
      });
      unlocking.catch(() => undefined);
      await Promise.race([unlocking, new Promise((resolve) => window.setTimeout(resolve, 500))]);
      setLocked(!unlocked);
      await created.connect();
    } catch {
      setError("Couldn't reach the music server.");
      created.disconnect("shutdown");
      return;
    } finally {
      connecting.current = false;
    }
    player.current = created;
    setOn(true);
    chat.send({ type: "music_speaker", player_id: created.clientId, can_play: true });
  }, [chat.send, makePlayer, setLocked]);

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

  // Music asked for here: show it at once, and turn the speaker on if it's off (the assistant plays it once
  // Music Assistant lists this browser). A failure replaces it with the reason.
  const handled = useRef(chat.music?.n ?? 0);
  useEffect(() => {
    if (!chat.music || chat.music.n === handled.current) return;
    handled.current = chat.music.n;
    const message = chat.music.message;
    if (message.type === "music_error") {
      setStarting(null);
      setError(message.text);
      return;
    }
    setError(null);
    setStarting({ title: message.title, artist: message.artist });
    if (!player.current) void toggle();
    // Never stuck on "Starting": the same track asked for again brings no new details, and a failure says so itself.
    const n = chat.music.n;
    window.setTimeout(() => {
      if (handled.current === n && startingRef.current) setStarting(null);
    }, 45_000);
  }, [chat.music, toggle, setStarting]);

  const unlock = useCallback(async () => {
    await player.current?.unlock();
    setLocked(false);
  }, []);

  const command = useCallback((name: "play" | "pause" | "next" | "previous") => {
    player.current?.sendCommand(name, undefined);
  }, []);

  const setVolume = useCallback((value: number) => {
    setVolumeState(value);
    player.current?.sendCommand("volume", { volume: value });
  }, []);

  return { available, on, playing, volume, now, starting, commands, error, locked, toggle, start, unlock, command, setVolume };
}
