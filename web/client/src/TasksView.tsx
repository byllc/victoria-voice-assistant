import { useEffect, useState } from "react";

import {
  Badge,
  Button,
  Card,
  CardContent,
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  Input,
  LED,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@pipecat-ai/voice-ui-kit";

import { AnswerForm } from "./NotificationCard";
import type { Chat } from "./useChat";
import { ACTIVE_STATES, type TaskInfo, type TaskState } from "./types";

const STATE: Record<TaskState, { label: string; color: "primary" | "warning" | "active" | "destructive" | "inactive" }> = {
  queued: { label: "Queued", color: "primary" },
  running: { label: "Running", color: "primary" },
  waiting: { label: "Waiting for you", color: "warning" },
  finished: { label: "Finished", color: "active" },
  failed: { label: "Failed", color: "destructive" },
  cancelled: { label: "Cancelled", color: "inactive" },
  timed_out: { label: "Timed out", color: "destructive" },
};

function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

const clock = (at: number, seconds = false) =>
  new Date(at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) });

/** The task dashboard (architecture §4.1): every agent task, live, with its actions. */
export function TasksView({ chat }: { chat: Chat }) {
  const [query, setQuery] = useState("");
  const [, setTick] = useState(0);
  useEffect(() => {
    const interval = window.setInterval(() => setTick((n) => n + 1), 1000); // elapsed times tick
    return () => window.clearInterval(interval);
  }, []);
  const board = chat.dashboard;
  if (!board) return <p className="p-4 text-sm opacity-60">Loading…</p>;
  const { summary } = board;
  const wanted = query.trim().toLowerCase();
  const shown = board.tasks.filter((t) => !wanted || `${t.request} ${t.agent} ${t.result ?? ""}`.toLowerCase().includes(wanted));

  return (
    <div className="flex flex-col gap-3 py-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span>{summary.running} running</span>
        <span className="opacity-40">·</span>
        <span>{summary.waiting} waiting</span>
        {summary.spend.map((s) => (
          <span key={s.pool}>
            <span className="opacity-40">· </span>
            {s.pool} ${s.used_usd.toFixed(2)} of ${s.budget_usd.toFixed(0)} this month
          </span>
        ))}
      </div>
      <ul className="flex flex-wrap gap-2" aria-label="Service health">
        {summary.health.map((h) => (
          <li key={h.name} title={h.detail}>
            <Badge variant="outline" color={h.ok === true ? "active" : h.ok === false ? "destructive" : "inactive"} className="gap-1.5">
              <LED on={h.ok === true} className={h.ok === false ? "bg-red-500" : undefined} /> {h.name}
            </Badge>
          </li>
        ))}
      </ul>
      <Input type="search" placeholder="Search tasks…" value={query} onChange={(event) => setQuery(event.target.value)} />
      {shown.length === 0 && <p className="text-sm opacity-60">{wanted ? "No matching tasks." : "No tasks yet."}</p>}
      <ul className="flex flex-col gap-2">
        {shown.map((task) => (
          <li key={task.id}>
            <TaskCard task={task} chat={chat} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function TaskCard({ task, chat }: { task: TaskInfo; chat: Chat }) {
  const active = ACTIVE_STATES.includes(task.state);
  const end = active ? Date.now() / 1000 : task.updated_at;
  const latest = task.progress.at(-1);
  const tokens = task.input_tokens + task.output_tokens;
  const action = (name: "cancel" | "retry" | "resend" | "open", extra: Record<string, string> = {}) =>
    chat.send({ type: "task_action", action: name, task_id: task.id, ...extra });
  const targets = [
    ...task.resend.models.map((model) => ({ key: `model:${model}`, label: `${task.agent} on ${model}`, extra: { model } })),
    ...task.resend.agents.map((agent) => ({ key: `agent:${agent}`, label: agent, extra: { agent } })),
  ];
  const started = new Date(task.created_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

  return (
    <Card className="gap-2 py-3">
      <CardContent className="flex flex-col gap-2 px-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge color={STATE[task.state].color} size="sm">
            {STATE[task.state].label}
          </Badge>
          <strong>{task.label}</strong>
          <span className="ml-auto text-xs opacity-60">
            {started} · {duration(end - task.created_at)}
          </span>
        </div>
        <p className="text-sm">
          {task.request} <span className="opacity-50">— from {task.device}</span>
        </p>
        {latest &&
          (task.progress.length > 1 ? (
            <Collapsible>
              <CollapsibleTrigger className="cursor-pointer text-left text-sm opacity-80">▸ {latest.text}</CollapsibleTrigger>
              <CollapsibleContent>
                <ol className="mt-1 flex flex-col gap-0.5 text-xs opacity-70">
                  {task.progress.map((step, index) => (
                    <li key={index}>
                      {clock(step.at, true)} {step.text}
                    </li>
                  ))}
                </ol>
              </CollapsibleContent>
            </Collapsible>
          ) : (
            <p className="text-sm opacity-80">{latest.text}</p>
          ))}
        {tokens > 0 && (
          <p className="text-xs opacity-60">
            {tokens.toLocaleString()} tokens{task.cost_usd > 0 ? ` · $${task.cost_usd.toFixed(4)}` : ""}
          </p>
        )}
        {task.error && task.state !== "finished" && <p className="text-sm text-red-600">{task.error}</p>}
        {task.state === "waiting" && task.question && (
          <div>
            <p className="text-sm">
              <strong>{task.agent} asks:</strong> {task.question}
            </p>
            <AnswerForm agent={task.agent} taskId={task.id} chat={chat} />
          </div>
        )}
        {task.result && (
          <Collapsible onOpenChange={(open) => open && action("open")}>
            <CollapsibleTrigger className="cursor-pointer text-left text-sm font-medium">▸ Open result</CollapsibleTrigger>
            <CollapsibleContent>
              <p className="mt-1 text-sm whitespace-pre-wrap">{task.result}</p>
            </CollapsibleContent>
          </Collapsible>
        )}
        <div className="flex flex-wrap items-center gap-2">
          {active ? (
            <>
              <Button size="sm" variant="outline" onClick={() => action("cancel")}>
                Cancel
              </Button>
              {task.cancel_note && <span className="text-xs opacity-60">{task.cancel_note}</span>}
            </>
          ) : (
            <>
              {task.state !== "finished" && (
                <Button size="sm" variant="outline" onClick={() => action("retry")}>
                  Retry
                </Button>
              )}
              {targets.length > 0 && (
                <Select
                  value=""
                  onValueChange={(key) => {
                    const target = targets.find((t) => t.key === key);
                    if (target) action("resend", target.extra);
                  }}
                >
                  <SelectTrigger size="sm" className="w-auto">
                    <SelectValue placeholder="Re-send to…" />
                  </SelectTrigger>
                  <SelectContent>
                    {targets.map((target) => (
                      <SelectItem key={target.key} value={target.key}>
                        {target.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
