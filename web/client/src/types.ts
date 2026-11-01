// The chat websocket protocol (see chat/serializer.py on the server).

export type TimerInfo = { id: string; name: string; remaining_s: number; state: "running" | "ringing" };

export type NotificationInfo = {
  id: string;
  source: string;
  kind: "result" | "question" | "error" | "notice";
  headline: string;
  full: string;
  priority: "urgent" | "normal" | "low";
  state: "pending" | "delivered";
  task_id: string | null;
  created_at: number;
};

export type TaskState = "queued" | "running" | "waiting" | "finished" | "failed" | "cancelled" | "timed_out";

export type TaskInfo = {
  id: string;
  agent: string;
  label: string;
  model: string | null;
  device: string;
  request: string;
  state: TaskState;
  question: string | null;
  result: string | null;
  error: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  created_at: number;
  updated_at: number;
  progress: { at: number; text: string }[];
  cancel_note: string | null;
  resend: { agents: string[]; models: string[] };
};

export type Health = { name: string; ok: boolean | null; detail: string };

export type Dashboard = {
  tasks: TaskInfo[];
  summary: {
    running: number;
    waiting: number;
    spend: { pool: string; used_usd: number; budget_usd: number }[];
    health: Health[];
  };
};

export type ServerMessage =
  | { type: "token"; text: string }
  | { type: "done" }
  | { type: "error"; message?: string }
  | { type: "notice"; title: string; text: string }
  | { type: "timers"; timers: TimerInfo[] }
  | { type: "timer_ring"; names: string[]; text: string | null }
  | { type: "timer_ring_stop"; names: string[] }
  | { type: "notifications"; items: NotificationInfo[] }
  | { type: "announce"; text: string; sound: boolean; spoken: boolean; items: NotificationInfo[] }
  | ({ type: "dashboard" } & Dashboard)
  | { type: "push_config"; public_key: string | null; problem: string | null }
  | { type: "music_start" };

// One line in the conversation: typed or spoken by you, said by Victoria, a notice, or a notification card.
export type LogEntry =
  | { id: number; kind: "you"; text: string; heard?: boolean }
  | { id: number; kind: "victoria"; text: string }
  | { id: number; kind: "notice"; title: string; text: string }
  | { id: number; kind: "notification"; item: NotificationInfo };

// A log entry before it has an id (one per kind).
export type NewLogEntry = LogEntry extends infer E ? (E extends LogEntry ? Omit<E, "id"> : never) : never;

export const ACTIVE_STATES: TaskState[] = ["queued", "running", "waiting"];
