import { useEffect, useState } from "react";

import { Badge } from "@pipecat-ai/voice-ui-kit";

import type { Timer } from "./useChat";

function formatRemaining(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}

/** Live countdowns; the server sends remaining seconds, counted down here in the browser's clock. */
export function Timers({ timers }: { timers: Timer[] }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const interval = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(interval);
  }, []);
  if (timers.length === 0) return null;
  return (
    <ul className="flex flex-wrap gap-2" aria-label="Timers">
      {timers.map((t) => (
        <li key={t.name}>
          <Badge color={t.state === "ringing" ? "destructive" : "secondary"} variant="outline" size="md">
            ⏱ {t.name} {t.state === "ringing" ? "done" : formatRemaining(t.endsAt - Date.now())}
          </Badge>
        </li>
      ))}
    </ul>
  );
}
