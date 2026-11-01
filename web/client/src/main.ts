import { Voice } from "./voice";

type TimerInfo = { id: string; name: string; remaining_s: number; state: "running" | "ringing" };

type NotificationInfo = {
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

type TaskInfo = {
  id: string;
  agent: string;
  label: string;
  model: string | null;
  device: string;
  request: string;
  state: "queued" | "running" | "waiting" | "finished" | "failed" | "cancelled" | "timed_out";
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

type Health = { name: string; ok: boolean | null; detail: string };

type Dashboard = {
  tasks: TaskInfo[];
  summary: {
    running: number;
    waiting: number;
    spend: { pool: string; used_usd: number; budget_usd: number }[];
    health: Health[];
  };
};

type ServerMessage =
  | { type: "token"; text: string }
  | { type: "done" }
  | { type: "error"; message?: string }
  | { type: "notice"; title: string; text: string }
  | { type: "timers"; timers: TimerInfo[] }
  | { type: "timer_ring"; names: string[]; text: string | null }
  | { type: "timer_ring_stop"; names: string[] }
  | { type: "notifications"; items: NotificationInfo[] }
  | ({ type: "dashboard" } & Dashboard)
  | { type: "push_config"; public_key: string | null; problem: string | null }
  | { type: "announce"; text: string; sound: boolean; spoken: boolean; items: NotificationInfo[] };

const loginSection = required<HTMLElement>("login");
const chatSection = required<HTMLElement>("chat");
const loginForm = required<HTMLFormElement>("login-form");
const passwordInput = required<HTMLInputElement>("password");
const loginError = required<HTMLElement>("login-error");
const composer = required<HTMLFormElement>("composer");
const messageInput = required<HTMLInputElement>("message");
const log = required<HTMLElement>("log");
const statusLabel = required<HTMLElement>("status");
const logoutButton = required<HTMLButtonElement>("logout");
const insecureBanner = required<HTMLElement>("insecure-banner");
const timerList = required<HTMLUListElement>("timers");
const ringing = required<HTMLElement>("ringing");
const ringingText = required<HTMLElement>("ringing-text");
const ringingStop = required<HTMLButtonElement>("ringing-stop");
const voiceToggle = required<HTMLButtonElement>("voice-toggle");
const voiceMode = required<HTMLSelectElement>("voice-mode");
const talkButton = required<HTMLButtonElement>("talk");
const voiceState = required<HTMLElement>("voice-state");
const botAudio = required<HTMLAudioElement>("bot-audio");
const inboxToggle = required<HTMLButtonElement>("inbox-toggle");
const badge = required<HTMLElement>("badge");
const inbox = required<HTMLElement>("inbox");
const inboxList = required<HTMLUListElement>("inbox-list");
const inboxEmpty = required<HTMLElement>("inbox-empty");
const inboxClear = required<HTMLButtonElement>("inbox-clear");
const tasksToggle = required<HTMLButtonElement>("tasks-toggle");
const tasksBadge = required<HTMLElement>("tasks-badge");
const dashboardView = required<HTMLElement>("dashboard");
const dashSummary = required<HTMLElement>("dash-summary");
const dashHealth = required<HTMLUListElement>("dash-health");
const dashSearch = required<HTMLInputElement>("dash-search");
const dashTasks = required<HTMLUListElement>("dash-tasks");
const pushToggle = required<HTMLButtonElement>("push-toggle");
const pushHint = required<HTMLElement>("push-hint");
let pushKey: string | null = null;
let board: Dashboard | null = null;
// Task cards the user expanded, kept open across live updates.
const openDetails = new Set<string>();
// Half-typed answers, by task, kept across live updates (the dashboard is redrawn on every change).
const answerDrafts = new Map<string, string>();

let socket: WebSocket | null = null;
let pendingReply: HTMLElement | null = null;
// Each timer's end, in this browser's clock (server sends remaining seconds).
let timers: { name: string; endsAt: number; state: TimerInfo["state"] }[] = [];
let chime: number | null = null;
let audio: AudioContext | null = null;

function required<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`missing element #${id}`);
  return element as T;
}

function showLogin(message = ""): void {
  closeSocket();
  chatSection.hidden = true;
  loginSection.hidden = false;
  loginError.textContent = message;
  passwordInput.value = "";
  passwordInput.focus();
}

function showChat(): void {
  loginSection.hidden = true;
  chatSection.hidden = false;
  messageInput.focus();
  connect();
}

function setStatus(text: string): void {
  statusLabel.textContent = text;
}

function appendMessage(role: "you" | "victoria", text: string, heard = false): HTMLElement {
  const entry = document.createElement("p");
  entry.className = heard ? `${role} heard` : role;
  const label = document.createElement("strong");
  label.textContent = role === "you" ? "You: " : "Victoria: ";
  const body = document.createElement("span");
  body.textContent = text;
  entry.append(label, body);
  log.append(entry);
  log.scrollTop = log.scrollHeight;
  return body;
}

function connect(): void {
  closeSocket();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws`);
  socket = ws;
  setStatus("connecting…");
  ws.onopen = () => {
    setStatus("connected");
    ws.send(JSON.stringify({ type: "hello", client_id: clientId }));
  };
  ws.onmessage = (event: MessageEvent<string>) => handleServerMessage(JSON.parse(event.data) as ServerMessage);
  ws.onclose = (event) => {
    if (socket !== ws) return;
    socket = null;
    // Never keep beeping for a timer this page can no longer hear about.
    stopRinging();
    setStatus(event.code === 1008 ? "signed out" : "disconnected");
    if (event.code === 1008) showLogin("Please sign in again.");
  };
}

function closeSocket(): void {
  const ws = socket;
  socket = null;
  ws?.close();
}

function handleServerMessage(message: ServerMessage): void {
  if (message.type === "token") {
    if (!pendingReply) pendingReply = appendMessage("victoria", "");
    pendingReply.textContent += message.text;
    log.scrollTop = log.scrollHeight;
  } else if (message.type === "done") {
    pendingReply = null;
    setStatus("connected");
  } else if (message.type === "error") {
    pendingReply = null;
    appendMessage("victoria", `Sorry, something went wrong${message.message ? `: ${message.message}` : ""}.`);
    setStatus("connected");
  } else if (message.type === "notice") {
    appendNotice(message.title, message.text);
  } else if (message.type === "timers") {
    const now = Date.now();
    timers = message.timers.map((t) => ({ name: t.name, endsAt: now + t.remaining_s * 1000, state: t.state }));
    renderTimers();
    // The server is the source of truth: nothing ringing there means stop here too.
    if (!timers.some((t) => t.state === "ringing")) stopRinging();
  } else if (message.type === "push_config") {
    pushKey = message.public_key;
    void syncPush(message.problem);
  } else if (message.type === "dashboard") {
    board = { tasks: message.tasks, summary: message.summary };
    renderDashboard();
  } else if (message.type === "notifications") {
    renderInbox(message.items);
  } else if (message.type === "announce") {
    if (message.sound) beep();
    // A speaker in the room says it, and the voice transcript shows it; otherwise show it here.
    if (!message.spoken) appendMessage("victoria", message.text);
    for (const item of message.items) appendNotification(item);
  } else if (message.type === "timer_ring") {
    startRinging(message.names, message.text);
  } else if (message.type === "timer_ring_stop") {
    if (!timers.some((t) => t.state === "ringing" && !message.names.includes(t.name))) stopRinging();
  }
}

function appendNotice(title: string, text: string): void {
  const entry = document.createElement("div");
  entry.className = "notice";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const body = document.createElement("p");
  body.textContent = text;
  entry.append(heading, body);
  log.append(entry);
  log.scrollTop = log.scrollHeight;
}

function acknowledge(ids: string[] | "all"): void {
  socket?.send(JSON.stringify({ type: "notification_ack", ids }));
}

// An agent's question: answered here, or by voice or chat ("Tell Hermes ...").
function answerForm(item: NotificationInfo): HTMLFormElement {
  const form = document.createElement("form");
  form.className = "answer";
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = `Answer ${item.source}…`;
  input.required = true;
  const draftKey = item.task_id ?? "";
  input.dataset.task = draftKey;
  input.value = answerDrafts.get(draftKey) ?? "";
  input.addEventListener("input", () => answerDrafts.set(draftKey, input.value));
  const send = document.createElement("button");
  send.type = "submit";
  send.textContent = "Answer";
  form.append(input, send);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || !socket) return;
    socket.send(JSON.stringify({ type: "agent_answer", task_id: item.task_id, text }));
    answerDrafts.delete(draftKey);
    appendMessage("you", text);
    form.querySelectorAll<HTMLInputElement | HTMLButtonElement>("input, button").forEach((el) => (el.disabled = true));
  });
  return form;
}

// The full text, collapsed; opening it marks the notification read (architecture §4.1 "Open").
function notificationDetails(item: NotificationInfo): HTMLElement {
  if (item.kind === "question") {
    const wrapper = document.createElement("div");
    const text = document.createElement("p");
    text.textContent = `${item.headline} ${item.full}`;
    wrapper.append(text, answerForm(item));
    return wrapper;
  }
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = item.headline;
  const body = document.createElement("p");
  body.textContent = item.full;
  details.append(summary, body);
  details.addEventListener("toggle", () => {
    if (details.open) acknowledge([item.id]);
  });
  return details;
}

function appendNotification(item: NotificationInfo): void {
  const entry = document.createElement("div");
  entry.className = "notice notification";
  entry.append(notificationDetails(item));
  log.append(entry);
  log.scrollTop = log.scrollHeight;
}

function renderInbox(items: NotificationInfo[]): void {
  badge.hidden = items.length === 0;
  badge.textContent = String(items.length);
  inboxEmpty.hidden = items.length > 0;
  inboxClear.hidden = items.length === 0;
  inboxList.replaceChildren(
    ...items.map((item) => {
      const entry = document.createElement("li");
      const when = new Date(item.created_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      const meta = document.createElement("span");
      meta.className = "status";
      meta.textContent = `${item.source} · ${when}`;
      entry.append(meta, notificationDetails(item));
      return entry;
    }),
  );
}

const STATE_LABELS: Record<TaskInfo["state"], string> = {
  queued: "Queued",
  running: "Running",
  waiting: "Waiting for an answer",
  finished: "Finished",
  failed: "Failed",
  cancelled: "Cancelled",
  timed_out: "Timed out",
};

function sendTaskAction(action: "cancel" | "retry" | "resend" | "open", task: TaskInfo, extra: Record<string, string> = {}): void {
  socket?.send(JSON.stringify({ type: "task_action", action, task_id: task.id, ...extra }));
}

function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text = ""): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function button(text: string, onClick: () => void): HTMLButtonElement {
  const node = element("button", "", text);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

// A <details> whose open state survives re-rendering.
function keptDetails(key: string, summaryText: string, body: HTMLElement, onOpen?: () => void): HTMLDetailsElement {
  const details = element("details");
  details.open = openDetails.has(key);
  details.append(element("summary", "", summaryText), body);
  details.addEventListener("toggle", () => {
    if (details.open) {
      openDetails.add(key);
      onOpen?.();
    } else openDetails.delete(key);
  });
  return details;
}

function taskCard(task: TaskInfo): HTMLLIElement {
  const card = element("li", `task task-${task.state}`);
  const head = element("div", "task-head");
  head.append(element("span", `pill pill-${task.state}`, STATE_LABELS[task.state]), element("strong", "", task.label));
  const started = new Date(task.created_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const end = task.state === "queued" || task.state === "running" || task.state === "waiting" ? Date.now() / 1000 : task.updated_at;
  const meta = element("span", "status task-time", `${started} · ${duration(end - task.created_at)}`);
  meta.dataset.start = String(task.created_at);
  meta.dataset.live = String(end !== task.updated_at);
  meta.dataset.started = started;
  head.append(meta);
  card.append(head, element("p", "task-request", `${task.request} — from ${task.device}`));

  const latest = task.progress.at(-1);
  if (latest) {
    const history = element("ol", "progress");
    for (const step of task.progress) {
      const when = new Date(step.at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
      history.append(element("li", "", `${when} ${step.text}`));
    }
    card.append(task.progress.length > 1 ? keptDetails(`progress-${task.id}`, latest.text, history) : element("p", "status", latest.text));
  }
  const tokens = task.input_tokens + task.output_tokens;
  if (tokens) {
    const cost = task.cost_usd > 0 ? ` · $${task.cost_usd.toFixed(4)}` : "";
    card.append(element("p", "status", `${tokens.toLocaleString()} tokens${cost}`));
  }
  if (task.error && task.state !== "finished") card.append(element("p", "error", task.error));
  if (task.result) {
    card.append(keptDetails(`result-${task.id}`, "Open result", element("p", "task-result", task.result), () => sendTaskAction("open", task)));
  }

  const actions = element("div", "task-actions");
  if (task.state === "waiting" && task.question) {
    card.append(element("p", "task-question", `${task.agent} asks: ${task.question}`));
    card.append(answerForm({ source: task.agent, task_id: task.id } as NotificationInfo));
  }
  if (task.state === "queued" || task.state === "running" || task.state === "waiting") {
    actions.append(button("Cancel", () => sendTaskAction("cancel", task)));
    if (task.cancel_note) actions.append(element("span", "status", task.cancel_note));
  } else {
    if (task.state !== "finished") actions.append(button("Retry", () => sendTaskAction("retry", task)));
    const targets = [
      ...task.resend.models.map((model) => ({ label: `${task.agent} on ${model}`, extra: { model } })),
      ...task.resend.agents.map((agent) => ({ label: agent, extra: { agent } })),
    ];
    if (targets.length) {
      const select = element("select");
      select.setAttribute("aria-label", "Re-send to");
      select.append(new Option("Re-send to…", ""));
      targets.forEach((target, index) => select.append(new Option(target.label, String(index))));
      select.addEventListener("change", () => {
        const target = targets[Number(select.value)];
        if (target) sendTaskAction("resend", task, target.extra);
        select.value = "";
      });
      actions.append(select);
    }
  }
  card.append(actions);
  return card;
}

function renderDashboard(): void {
  if (!board) return;
  const { summary } = board;
  const active = summary.running + summary.waiting;
  tasksBadge.hidden = active === 0;
  tasksBadge.textContent = String(active);
  const spend = summary.spend.map((s) => `${s.pool} $${s.used_usd.toFixed(2)} of $${s.budget_usd.toFixed(0)} this month`);
  dashSummary.textContent = [`${summary.running} running`, `${summary.waiting} waiting`, ...spend].join(" · ");
  dashHealth.replaceChildren(
    ...summary.health.map((h) => {
      const chip = element("li", `chip ${h.ok === true ? "ok" : h.ok === false ? "down" : "unknown"}`, h.name);
      chip.title = h.detail;
      return chip;
    }),
  );
  const query = dashSearch.value.trim().toLowerCase();
  const shown = board.tasks.filter((t) => !query || `${t.request} ${t.agent} ${t.result ?? ""}`.toLowerCase().includes(query));
  // Keep typing where it was if an answer box had focus.
  const focused = document.activeElement instanceof HTMLInputElement && dashTasks.contains(document.activeElement)
    ? document.activeElement.dataset.task
    : undefined;
  dashTasks.replaceChildren(...(shown.length ? shown.map(taskCard) : [element("li", "status", query ? "No matching tasks." : "No tasks yet.")]));
  if (focused !== undefined) {
    const input = dashTasks.querySelector<HTMLInputElement>(`input[data-task="${CSS.escape(focused)}"]`);
    input?.focus();
    input?.setSelectionRange(input.value.length, input.value.length);
  }
}

// Running tasks' elapsed times tick without waiting for the server.
function tickElapsed(): void {
  for (const meta of dashTasks.querySelectorAll<HTMLElement>(".task-time[data-live='true']")) {
    meta.textContent = `${meta.dataset.started} · ${duration(Date.now() / 1000 - Number(meta.dataset.start))}`;
  }
}

// This browser's own id, so the assistant only pushes to browsers that aren't open (architecture §10.3).
const clientId = (() => {
  try {
    const saved = localStorage.getItem("victoria-client-id");
    if (saved) return saved;
    const made = crypto.randomUUID();
    localStorage.setItem("victoria-client-id", made);
    return made;
  } catch {
    return crypto.randomUUID();
  }
})();

const pushSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window && window.isSecureContext;
const iosNotInstalled = /iPhone|iPad/.test(navigator.userAgent) && !(navigator as Navigator & { standalone?: boolean }).standalone;

function urlBase64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = (value + "=".repeat((4 - (value.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

// The service worker that shows pushes; null where it can't be registered (then no alerts are offered).
const workerRegistration: Promise<ServiceWorkerRegistration | null> = pushSupported
  ? navigator.serviceWorker.register("/sw.js").catch(() => null)
  : Promise.resolve(null);

async function currentSubscription(): Promise<PushSubscription | null> {
  const registration = await workerRegistration;
  return registration ? registration.pushManager.getSubscription() : null;
}

// Show the alerts button when the assistant has push on; keep the server's copy of our subscription fresh.
async function syncPush(problem: string | null): Promise<void> {
  pushToggle.hidden = true;
  pushHint.hidden = true;
  if (!pushKey) return; // push is off on the assistant (problem says why); nothing to offer
  if (!pushSupported || !(await workerRegistration)) {
    pushHint.hidden = false;
    pushHint.textContent = window.isSecureContext ? "This browser can't receive alerts." : "Alerts need the HTTPS address.";
    return;
  }
  if (iosNotInstalled) {
    pushHint.hidden = false;
    pushHint.textContent = "For alerts on iPhone: Share → Add to Home Screen, then open Victoria from there.";
    return;
  }
  pushToggle.hidden = false;
  const subscription = await currentSubscription();
  pushToggle.textContent = subscription ? "Alerts on" : "Phone alerts";
  pushToggle.classList.toggle("active", Boolean(subscription));
  if (subscription) socket?.send(JSON.stringify({ type: "push_subscribe", subscription: subscription.toJSON() }));
  void problem;
}

async function togglePush(): Promise<void> {
  if (!pushKey) return;
  const existing = await currentSubscription();
  if (existing) {
    socket?.send(JSON.stringify({ type: "push_unsubscribe", endpoint: existing.endpoint }));
    await existing.unsubscribe();
  } else {
    if ((await Notification.requestPermission()) !== "granted") {
      pushHint.hidden = false;
      pushHint.textContent = "Alerts are blocked for this site in the browser's settings.";
      return;
    }
    const registration = await workerRegistration;
    if (!registration) return;
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToBytes(pushKey),
    });
    socket?.send(JSON.stringify({ type: "push_subscribe", subscription: subscription.toJSON() }));
  }
  await syncPush(null);
}

function formatRemaining(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}

function renderTimers(): void {
  timerList.hidden = timers.length === 0;
  timerList.replaceChildren(
    ...timers.map((t) => {
      const item = document.createElement("li");
      item.className = t.state === "ringing" ? "ringing-timer" : "";
      item.textContent = `${t.name} ${t.state === "ringing" ? "done" : formatRemaining(t.endsAt - Date.now())}`;
      return item;
    }),
  );
}

function beep(): void {
  // No chime file is served yet; a short two-tone beep stands in for it.
  try {
    audio ??= new AudioContext();
    const start = audio.currentTime;
    for (const [offset, frequency] of [[0, 880], [0.25, 660]] as const) {
      const oscillator = audio.createOscillator();
      const gain = audio.createGain();
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0.2, start + offset);
      gain.gain.exponentialRampToValueAtTime(0.001, start + offset + 0.22);
      oscillator.connect(gain).connect(audio.destination);
      oscillator.start(start + offset);
      oscillator.stop(start + offset + 0.22);
    }
  } catch {
    // Audio may be blocked until the page has had a user gesture; the banner still shows.
  }
}

function startRinging(names: string[], text: string | null): void {
  const label = names.length === 1 ? `Your ${names[0]} timer is done.` : `Your ${names.join(" and ")} timers are done.`;
  ringingText.textContent = text ?? label;
  ringing.hidden = false;
  if (text) appendMessage("victoria", text);
  if (chime === null) {
    beep();
    chime = window.setInterval(beep, 2000);
  }
}

function stopRinging(): void {
  ringing.hidden = true;
  if (chime !== null) window.clearInterval(chime);
  chime = null;
}

async function checkSession(): Promise<void> {
  const response = await fetch("/api/session", { credentials: "same-origin" });
  const body = (await response.json()) as { authenticated: boolean; insecure?: boolean };
  insecureBanner.hidden = !body.insecure;
  logoutButton.hidden = Boolean(body.insecure);
  if (body.authenticated) showChat();
  else showLogin();
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.textContent = "";
  const response = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: passwordInput.value }),
    credentials: "same-origin",
  });
  if (response.ok) showChat();
  else showLogin("Incorrect password.");
});

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = messageInput.value.trim();
  if (!text || !socket || socket.readyState !== WebSocket.OPEN) return;
  appendMessage("you", text);
  setStatus("thinking…");
  socket.send(JSON.stringify({ type: "user_message", text }));
  messageInput.value = "";
});

let voiceReply: HTMLElement | null = null;
const voice = new Voice(
  {
    onUserText: (text) => {
      voiceReply = null;
      appendMessage("you", text, true);
    },
    onBotText: (text, newTurn) => {
      if (newTurn || !voiceReply) voiceReply = appendMessage("victoria", "");
      voiceReply.textContent = `${voiceReply.textContent ?? ""}${voiceReply.textContent ? " " : ""}${text}`.trim();
      log.scrollTop = log.scrollHeight;
    },
    // Voice sessions send notices and timer updates in the chat protocol's shape.
    onServerMessage: (message) => {
      if (message && typeof message === "object" && "type" in message) handleServerMessage(message as ServerMessage);
    },
    setState: (text) => {
      voiceState.textContent = text;
    },
  },
  botAudio,
);

function syncVoiceControls(): void {
  voiceToggle.textContent = voice.connected ? "Stop voice" : "Start voice";
  voiceToggle.classList.toggle("active", voice.connected);
  talkButton.hidden = !(voice.connected && voiceMode.value === "push-to-talk");
}

const unavailable = Voice.unavailableReason();
if (unavailable) {
  voiceToggle.disabled = true;
  voiceMode.disabled = true;
  voiceState.textContent = unavailable;
}

voiceToggle.addEventListener("click", async () => {
  voiceToggle.disabled = true;
  try {
    if (voice.connected) await voice.stop();
    else await voice.start(voiceMode.value as "hands-free" | "push-to-talk");
  } catch {
    // The state line already says why.
  } finally {
    voiceToggle.disabled = false;
    syncVoiceControls();
  }
});

voiceMode.addEventListener("change", () => {
  if (voice.connected) voice.setMode(voiceMode.value as "hands-free" | "push-to-talk");
  syncVoiceControls();
});

function talk(active: boolean): void {
  if (!voice.connected || voiceMode.value !== "push-to-talk") return;
  talkButton.classList.toggle("active", active);
  voice.setMic(active);
}

talkButton.addEventListener("pointerdown", (event) => {
  talkButton.setPointerCapture(event.pointerId);
  talk(true);
});
talkButton.addEventListener("pointerup", () => talk(false));
talkButton.addEventListener("pointercancel", () => talk(false));
window.addEventListener("keydown", (event) => {
  if (event.code === "Space" && !event.repeat && document.activeElement !== messageInput) {
    event.preventDefault();
    talk(true);
  }
});
window.addEventListener("keyup", (event) => {
  if (event.code === "Space" && document.activeElement !== messageInput) talk(false);
});

ringingStop.addEventListener("click", () => {
  socket?.send(JSON.stringify({ type: "timer_stop" }));
  stopRinging();
});

window.setInterval(renderTimers, 1000);

inboxToggle.addEventListener("click", () => {
  inbox.hidden = !inbox.hidden;
  inboxToggle.setAttribute("aria-expanded", String(!inbox.hidden));
});
inboxClear.addEventListener("click", () => acknowledge("all"));
tasksToggle.addEventListener("click", () => {
  dashboardView.hidden = !dashboardView.hidden;
  tasksToggle.setAttribute("aria-expanded", String(!dashboardView.hidden));
});
dashSearch.addEventListener("input", renderDashboard);
pushToggle.addEventListener("click", () => {
  togglePush().catch(() => {
    pushHint.hidden = false;
    pushHint.textContent = "Couldn't turn on alerts in this browser.";
  });
});
window.setInterval(tickElapsed, 1000);

logoutButton.addEventListener("click", async () => {
  if (voice.connected) await voice.stop();
  syncVoiceControls();
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  log.replaceChildren();
  showLogin();
});

void checkSession();
