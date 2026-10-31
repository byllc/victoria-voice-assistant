import { Voice } from "./voice";

type TimerInfo = { id: string; name: string; remaining_s: number; state: "running" | "ringing" };

type ServerMessage =
  | { type: "token"; text: string }
  | { type: "done" }
  | { type: "error"; message?: string }
  | { type: "notice"; title: string; text: string }
  | { type: "timers"; timers: TimerInfo[] }
  | { type: "timer_ring"; names: string[]; text: string | null }
  | { type: "timer_ring_stop"; names: string[] };

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
  ws.onopen = () => setStatus("connected");
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

logoutButton.addEventListener("click", async () => {
  if (voice.connected) await voice.stop();
  syncVoiceControls();
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  log.replaceChildren();
  showLogin();
});

void checkSession();
