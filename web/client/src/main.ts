type ServerMessage =
  | { type: "token"; text: string }
  | { type: "done" }
  | { type: "error"; message?: string };

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

let socket: WebSocket | null = null;
let pendingReply: HTMLElement | null = null;

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

function appendMessage(role: "you" | "victoria", text: string): HTMLElement {
  const entry = document.createElement("p");
  entry.className = role;
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
  }
}

async function checkSession(): Promise<void> {
  const response = await fetch("/api/session", { credentials: "same-origin" });
  const body = (await response.json()) as { authenticated: boolean };
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

logoutButton.addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  log.replaceChildren();
  showLogin();
});

void checkSession();
