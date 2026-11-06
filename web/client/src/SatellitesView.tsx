import { useEffect, useMemo, useState } from "react";

import { Badge, Card, CardContent, LED } from "@pipecat-ai/voice-ui-kit";

import type { Chat } from "./useChat";
import type { SatelliteInfo, SatelliteState } from "./types";

const STATE: Record<SatelliteState, { label: string; color: "primary" | "warning" | "active" | "inactive" }> = {
  idle: { label: "Idle", color: "inactive" },
  listening: { label: "Listening", color: "active" },
  thinking: { label: "Thinking", color: "primary" },
  replying: { label: "Replying", color: "active" },
  announcing: { label: "Announcing", color: "warning" },
};

function remaining(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

const clock = (at: number) => new Date(at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

/** The satellite view (architecture §4.1, Stage 13): what each satellite is doing, live. */
export function SatellitesView({ chat }: { chat: Chat }) {
  const satellites = chat.dashboard?.satellites ?? [];
  const received = useMemo(() => Date.now() / 1000, [chat.dashboard]); // timers count down from here
  const [, setTick] = useState(0);
  useEffect(() => {
    const interval = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(interval);
  }, []);
  if (!chat.dashboard) return <p className="p-4 text-sm opacity-60">Loading…</p>;
  if (satellites.length === 0) return <p className="p-4 text-sm opacity-60">No satellites set up.</p>;
  const online = satellites.filter((s) => s.online).length;

  return (
    <div className="flex flex-col gap-3 py-2">
      <p className="text-sm">
        {online} of {satellites.length} online
      </p>
      <ul className="flex flex-col gap-2">
        {satellites.map((satellite) => (
          <li key={satellite.name}>
            <SatelliteCard satellite={satellite} elapsed={Date.now() / 1000 - received} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function SatelliteCard({ satellite, elapsed }: { satellite: SatelliteInfo; elapsed: number }) {
  const state = STATE[satellite.state];
  return (
    <Card className="gap-2 py-3">
      <CardContent className="flex flex-col gap-2 px-4">
        <div className="flex flex-wrap items-center gap-2">
          <LED on={satellite.online} className={satellite.online ? undefined : "bg-red-500"} />
          <strong>{satellite.name}</strong>
          {satellite.room && satellite.room !== satellite.name && <span className="text-sm opacity-60">in the {satellite.room}</span>}
          {satellite.online && satellite.state === "idle" ? (
            <span className="text-xs opacity-60">Idle</span>
          ) : satellite.online ? (
            <Badge color={state.color} size="sm">
              {state.label}
            </Badge>
          ) : (
            <Badge color="destructive" size="sm">
              Offline
            </Badge>
          )}
          {satellite.changed_at > 0 && <span className="ml-auto text-xs opacity-60">{clock(satellite.changed_at)}</span>}
        </div>
        {satellite.request && (
          <p className="text-sm">
            <span className="opacity-50">Heard: </span>
            {satellite.request}
          </p>
        )}
        {satellite.reply && (
          <p className="text-sm">
            <span className="opacity-50">Said: </span>
            {satellite.reply}
          </p>
        )}
        {satellite.timers.length > 0 && (
          <ul className="flex flex-wrap gap-2" aria-label="Timers">
            {satellite.timers.map((timer) => (
              <li key={timer.id}>
                <Badge variant="outline" color={timer.state === "ringing" ? "warning" : "primary"} size="sm">
                  {timer.name}: {timer.state === "ringing" ? "ringing" : remaining(timer.remaining_s - elapsed)}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
