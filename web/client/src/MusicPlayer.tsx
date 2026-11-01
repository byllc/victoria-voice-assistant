import { useEffect, useState } from "react";

import { Button, Card, CardContent, Slider } from "@pipecat-ai/voice-ui-kit";

import type { Speaker } from "./useSpeaker";

function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** Where the track is now: the last reported position, moved on by the time since, while playing. */
function position(speaker: Speaker): number | null {
  const now = speaker.now;
  if (!now || now.progressMs === null) return null;
  const moved = speaker.playing ? (Date.now() - now.progressAt) * now.speed : 0;
  return Math.min(now.progressMs + moved, now.durationMs ?? Infinity);
}

/**
 * The music player widget, always a full player: artwork, track, time, previous / play-pause / next, volume.
 * Pressing play (or asking for music) makes this browser a speaker first; the speaker button turns that off.
 * Above the tabs, so it stays put while you switch.
 */
export function MusicPlayer({ speaker }: { speaker: Speaker }) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!speaker.playing) return;
    const interval = window.setInterval(() => tick((n) => n + 1), 500);
    return () => window.clearInterval(interval);
  }, [speaker.playing]);

  if (!speaker.available) return null;

  const now = speaker.now;
  const at = position(speaker);
  const duration = now?.durationMs ?? null;
  const can = (name: string) => speaker.on && (speaker.commands.length === 0 || speaker.commands.includes(name));
  // Play needs this browser to be a speaker; the first press (a tap, so audio is allowed) connects it.
  const playPause = async () => {
    if (!speaker.on) {
      await speaker.start();
      return;
    }
    if (speaker.locked) {
      // Music may already be playing here, silently: this tap is what lets the browser play sound.
      await speaker.unlock();
      if (!speaker.playing) speaker.command("play");
      return;
    }
    speaker.command(speaker.playing ? "pause" : "play");
  };
  const starting = speaker.starting;
  const hint = speaker.locked
    ? "Tap ▶ to hear it here."
    : speaker.on && !now?.title
      ? "Ask for music, like “play Ani DiFranco”, and it plays here."
      : "Ask for music, or press play, and it plays here.";

  return (
    <Card className="py-3">
      <CardContent className="flex items-center gap-3 px-3">
        <div className="size-16 shrink-0 overflow-hidden rounded-md bg-black/10 dark:bg-white/10">
          {starting ? (
            <div className="flex size-full animate-pulse items-center justify-center text-2xl opacity-40">♪</div>
          ) : now?.artwork ? (
            <img src={now.artwork} alt="" className="size-full object-cover" />
          ) : (
            <div className="flex size-full items-center justify-center text-2xl opacity-40">♪</div>
          )}
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex min-w-0 items-start gap-2">
            <div className="min-w-0 flex-1">
              <p className="truncate font-semibold" style={{ color: "var(--color-foreground)" }}>
                {starting?.title ?? now?.title ?? "Nothing playing"}
              </p>
              <p className="truncate text-sm opacity-70">
                {speaker.error ??
                  (starting
                    ? [speaker.locked ? "Starting… tap ▶ to hear it" : "Starting…", starting.artist].filter(Boolean).join(" · ")
                    : speaker.locked || !now?.title
                      ? hint
                      : [now.artist, now.album].filter(Boolean).join(" · "))}
              </p>
            </div>
            <Button
              size="sm"
              variant={speaker.on ? "active" : "ghost"}
              className="shrink-0"
              onClick={() => void speaker.toggle()}
              aria-pressed={speaker.on}
              title={speaker.on ? "This browser is a speaker: tap to stop" : "Make this browser a speaker"}
            >
              {speaker.on ? "🔊 Here" : "🔈 Off"}
            </Button>
          </div>
          <div className="flex items-center gap-2 text-xs tabular-nums opacity-70">
            <span>{starting ? "0:00" : clock(at ?? 0)}</span>
            <div
              className="h-1 flex-1 overflow-hidden rounded-full bg-black/10 dark:bg-white/15"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={duration ?? 0}
              aria-valuenow={at ?? 0}
            >
              {starting ? (
                <div className="h-full w-1/3 animate-pulse rounded-full bg-teal-600/60" />
              ) : (
                <div className="h-full rounded-full bg-teal-600" style={{ width: duration ? `${Math.min(100, ((at ?? 0) / duration) * 100)}%` : "0%" }} />
              )}
            </div>
            <span>{!starting && duration ? clock(duration) : "-:--"}</span>
          </div>
          <div className="flex items-center gap-1">
            <Button size="sm" variant="ghost" isIcon aria-label="Previous" disabled={!can("previous")} onClick={() => speaker.command("previous")}>
              ⏮
            </Button>
            <Button
              size="sm"
              variant="outline"
              isIcon
              aria-label={speaker.locked ? "Play sound here" : speaker.playing ? "Pause" : "Play"}
              onClick={() => void playPause()}
            >
              {speaker.playing && !speaker.locked ? "⏸" : "▶"}
            </Button>
            <Button size="sm" variant="ghost" isIcon aria-label="Next" disabled={!can("next")} onClick={() => speaker.command("next")}>
              ⏭
            </Button>
            <div className="ml-2 flex min-w-0 flex-1 items-center gap-2 sm:max-w-48" aria-label="Volume">
              <span className="text-xs opacity-60">🔈</span>
              <Slider
                value={[speaker.volume]}
                min={0}
                max={100}
                step={1}
                disabled={!speaker.on}
                onValueChange={([value]) => speaker.setVolume(value)}
              />
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
