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
 * The music player widget: this browser as a speaker, with what's playing, artwork, progress and controls.
 * Off: a single "Play music here" button. Above the tabs, so it stays put while you switch.
 */
export function MusicPlayer({ speaker }: { speaker: Speaker }) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!speaker.playing) return;
    const interval = window.setInterval(() => tick((n) => n + 1), 500);
    return () => window.clearInterval(interval);
  }, [speaker.playing]);

  if (!speaker.available) return null;
  if (!speaker.on) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <Button size="sm" variant="outline" onClick={() => void speaker.toggle()}>
          ♪ Play music here
        </Button>
        <span className="opacity-60">{speaker.error ?? "Turn this browser into a speaker for music you ask for."}</span>
      </div>
    );
  }

  const now = speaker.now;
  const at = position(speaker);
  const duration = now?.durationMs ?? null;
  const can = (name: string) => speaker.commands.length === 0 || speaker.commands.includes(name);

  return (
    <Card className="py-3">
      <CardContent className="flex items-center gap-3 px-3">
        <div className="size-16 shrink-0 overflow-hidden rounded-md bg-black/10 dark:bg-white/10">
          {now?.artwork ? (
            <img src={now.artwork} alt="" className="size-full object-cover" />
          ) : (
            <div className="flex size-full items-center justify-center text-2xl opacity-40">♪</div>
          )}
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="min-w-0">
            <p className="truncate font-semibold">{now?.title ?? "Nothing playing"}</p>
            <p className="truncate text-sm opacity-70">
              {now?.title ? [now.artist, now.album].filter(Boolean).join(" · ") : "Ask for music and it plays here."}
            </p>
          </div>
          {duration ? (
            <div className="flex items-center gap-2 text-xs tabular-nums opacity-70">
              <span>{clock(at ?? 0)}</span>
              <div className="h-1 flex-1 overflow-hidden rounded-full bg-black/10 dark:bg-white/15" role="progressbar" aria-valuemin={0} aria-valuemax={duration} aria-valuenow={at ?? 0}>
                <div className="h-full rounded-full bg-teal-600" style={{ width: `${Math.min(100, ((at ?? 0) / duration) * 100)}%` }} />
              </div>
              <span>{clock(duration)}</span>
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-1">
            <Button size="sm" variant="ghost" isIcon aria-label="Previous" disabled={!can("previous")} onClick={() => speaker.command("previous")}>
              ⏮
            </Button>
            <Button
              size="sm"
              variant="outline"
              isIcon
              aria-label={speaker.playing ? "Pause" : "Play"}
              onClick={() => speaker.command(speaker.playing ? "pause" : "play")}
            >
              {speaker.playing ? "⏸" : "▶"}
            </Button>
            <Button size="sm" variant="ghost" isIcon aria-label="Next" disabled={!can("next")} onClick={() => speaker.command("next")}>
              ⏭
            </Button>
            <div className="ml-2 flex min-w-24 flex-1 items-center gap-2 sm:max-w-40" aria-label="Volume">
              <span className="text-xs opacity-60">🔈</span>
              <Slider value={[speaker.volume]} min={0} max={100} step={1} onValueChange={([value]) => speaker.setVolume(value)} />
            </div>
            <Button size="sm" variant="ghost" className="ml-auto" onClick={() => void speaker.toggle()} title="Stop being a speaker">
              Off
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
