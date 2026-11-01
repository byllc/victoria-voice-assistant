# Victoria: a private household voice assistant

Victoria is a local-first voice assistant for our home. It is meant to replace Alexa for everyday requests (timers, weather, the time, and later home control, music and search) and to hand bigger requests to AI agents such as Hermes and Claude. Speech recognition, speech synthesis and the everyday model all run on one Mac mini, so audio never leaves the house.

Development happens on an M2 Mac with local test models. The target is a Mac mini (M4 Pro, 64 GB, macOS 26). Speed measured on the M2 isn't representative.

## How it works

Two processes of ours, plus LM Studio for the language models:

```
 browser / phone                     Mac mini
┌───────────────┐   HTTPS + login  ┌──────────────────────────┐
│  web client   │ ───────────────▶ │  web interface (:8766)   │  login, serves the client,
│ (chat, voice) │                  │  FastAPI                 │  proxies chat + signaling
└───────┬───────┘                  └────────────┬─────────────┘
        │ WebRTC audio (LAN only)               │ localhost only
        │                          ┌────────────▼─────────────┐
        └─────────────────────────▶│  assistant service       │  Pipecat pipeline:
                                   │  (:8765, localhost)      │  VAD -> Whisper -> router
                                   │                          │  model + tools -> Kokoro
                                   └────────────┬─────────────┘
                                                │ OpenAI-compatible API
                                   ┌────────────▼─────────────┐
                                   │  LM Studio (:1234)       │  local models
                                   └──────────────────────────┘
                                   ┌──────────────────────────┐
                                   │  SearXNG (:8888, local)  │  web search, no accounts
                                   └──────────────────────────┘
```

- **Assistant service** (`assistant serve`): the core, built on [Pipecat](https://github.com/pipecat-ai/pipecat). It accepts connections only from localhost.
  - **Voice:** Silero VAD with Smart Turn decides when you've finished speaking, Whisper (MLX) turns it into text, the router model picks a tool or answers, and Kokoro speaks the reply.
  - **Text chat** uses the same model and tools.
  - **Timers** are kept in SQLite.
- **Web interface** (`assistant web`): the only process facing the network.
  - Household login, and the example web client: text chat, voice (hands-free or push-to-talk), timers with live countdowns.
  - It relays chat to the assistant service and passes on the setup of a voice call; the audio itself goes straight to the assistant service.
- **LM Studio** serves the local models through an OpenAI-compatible API. The `router` model answers everyday requests and calls tools; if it fails, the `router_fallback` model takes over.
- **SearXNG** is a self-hosted metasearch engine, run natively on `127.0.0.1:8888`. `web_search` gets a few short snippets from it, and `web_read` reads one result page (plain HTTP first, a warm headless Chromium for pages that need JavaScript). Web text is passed to the model marked as untrusted.

### What happens when you speak

1. The browser streams your microphone over WebRTC to the assistant service.
2. Silero VAD detects speech. Smart Turn listens at each pause to judge whether you've finished; one-word commands like "stop" end at the first pause.
3. Whisper transcribes the turn.
4. A short "stop" (while a timer rings) or a yes/no answer to a question Victoria just asked is handled at once, with no model call.
5. Otherwise the router model gets the conversation and the tool list, and calls a tool or answers. A tool either speaks its own reply, with no second model call (timers, the clock), or hands its result back to the model to phrase (weather).
6. Kokoro speaks the reply, sentence by sentence.

### Tools

Tools are declared in `config/tools.yaml` and reloaded when the file changes:

- **Kinds:** `builtin` (timers, weather, web search), `python` (a function in this project), `http` (a REST call), and `stub` (planned but not built).
- **Stubs:** a stub speaks a short notice and sends its setup steps to the chat. A tool whose definition is broken or whose code can't be found becomes an automatic stub instead of breaking the file.
- **Built-in tools:** `clarify` (ask one question), `offer_agent` and `ask_agent` are always present.

## Status

Work proceeds in numbered stages, each approved before the next starts. The live tracker is [docs/stages.md](docs/stages.md).

| Stage | | Status |
|---|---|---|
| 0–2 | Tracker, skeleton, configuration, launchd | approved |
| 3 | Web interface and text chat | approved |
| 4 | Tool registry, timers, weather | approved |
| 5 | Voice in the browser | approved |
| 6 | Local web search (SearXNG) | in progress: search works; review pending |
| 7–12 | Agents and model pools, notifications and task dashboard, home control, music, hardening, satellites | not started |

## Requirements

- macOS on Apple Silicon (MLX Whisper needs it).
- [uv](https://docs.astral.sh/uv/). It installs Python 3.12 into the project's `.venv`; nothing goes into the system Python.
- Node.js 23 or later and npm, to build the web client.
- [LM Studio](https://lmstudio.ai) with its `lms` command and a tool-calling model loaded. The examples use `google/gemma-4-12b`.

## Getting started

```sh
# 1. Python environment
uv sync --extra dev

# 2. Configuration: copies the examples into config/
uv run assistant config init
uv run assistant secrets set LMS_API_KEY
#    LM Studio > Developer > API key; stored in the macOS Keychain (docs/secrets.md).
#    Set weather.home (lat/lon) in config/tools.yaml.
uv run assistant config check

# 3. Web client
(cd web/client && npm install && npm run build)

# 4. Web search: SearXNG (pinned commit, in ~/Services/searxng) and the headless browser
uv run assistant searxng install
uv run playwright install --only-shell chromium
```

Then, in three terminals:

```sh
uv run assistant searxng run  # SearXNG on 127.0.0.1:8888
uv run assistant serve        # assistant service on 127.0.0.1:8765
uv run assistant web          # web interface on 127.0.0.1:8766
```

Open <http://localhost:8766>. Type to chat, or click **Start voice** and allow the microphone.

The first `assistant serve` downloads the speech models: Whisper large-v3-turbo (about 1.6 GB) and Kokoro (about 330 MB). `curl http://127.0.0.1:8765/health` shows `"speech": "ready"` once voice can be used.

Things to try: "set a pasta timer for ten minutes", "what timers do I have", "cancel the timer", "what's the weather tomorrow", "what time is it", "what's the latest news about the Artemis mission", "turn off the kitchen lights" (a stub: it sends its setup steps to the chat).

### Login

The web interface reads the household password from the Keychain, never from a file:

```sh
uv run assistant secrets set ASSISTANT_WEB_PASSWORD
```

Then restart the web process. Without it, the web interface runs open and shows a red "Insecure" banner.

### Voice from a phone or another computer

Browsers allow the microphone only on HTTPS pages (or `localhost`). Put a certificate and key in `config/tls/cert.pem` and `config/tls/key.pem`. The web interface then serves HTTPS on all interfaces. Install, renewal and checking (`assistant tls status`) are in [docs/https.md](docs/https.md).

### Running under launchd

```sh
uv run assistant launchd install --load        # assistant service
uv run assistant launchd install-web --load    # web interface
uv run assistant launchd install-searxng --load  # SearXNG
```

Both restart automatically if they exit. Logs go to `~/Library/Logs/Victoria Voice Assistant/`. To restart after a code or certificate change:

```sh
launchctl kickstart -k gui/$(id -u)/com.victoria.voice-assistant
launchctl kickstart -k gui/$(id -u)/com.victoria.voice-assistant-web
launchctl kickstart -k gui/$(id -u)/com.victoria.searxng
```

## Configuration

Everything lives in `config/` (or the directory in `ASSISTANT_CONFIG_DIR`); the committed examples are in `config/examples/`.

| File | What it holds |
|---|---|
| `models.yaml` | LM Studio and other servers, models and their parameters, roles (`router`, `router_fallback`), and `speech:` (Whisper model, Kokoro voice, VAD and turn-detection settings) |
| `tools.yaml` | Tools, built-in service settings (weather, timers, search), and the phrases `tools test` checks |
| `searxng/settings.yml` | SearXNG's settings: localhost only, JSON output, which engines, a short timeout |
| `agents.yaml` | Agents and their aliases (used from Stage 7) |
| `tls/` | The HTTPS certificate and key. Git-ignored. |

Edits to the YAML files are picked up while the service runs. An invalid edit is rejected and logged, and the previous configuration stays in use.

With gemma models, keep `reasoning_effort: none` in each model's `params`. Without it the model reasons first, replies slowly, and often runs out of tokens before saying anything.

## Commands

| Command | What it does |
|---|---|
| `assistant config init` / `check` | Copy the example configuration / validate it (and list automatic stubs) |
| `assistant serve` | Run the assistant service |
| `assistant web` | Run the web interface |
| `assistant tools status` | List tools, which are stubs, and why |
| `assistant tools test` | Send each example phrase to the router model and report what it did |
| `assistant tls status` | Check the HTTPS certificate: key match, names, expiry |
| `assistant voice bench` | Speak 20 commands over WebRTC and report latency by stage (for the target machine) |
| `assistant searxng install` / `run` | Check out the pinned SearXNG and build its environment / run it |
| `assistant launchd install` / `install-web` / `install-searxng` | Write (and with `--load`, start) the LaunchAgents |

Run them with `uv run`, for example `uv run assistant tools status`.

## Development

```sh
uv run pytest -q                                  # quick test run
ASSISTANT_LIVE_TESTS=1 uv run pytest -q -k live   # also tests against the running LM Studio
(cd web/client && npm run build)                  # typecheck and build the client
```

Logs are one JSON object per line, with an `event` field (`tool_call`, `llm_first_token`, `voice_latency`, `timer_started`, …). Every request logs its timings.

Rules for contributors, including coding agents, are in [AGENTS.md](AGENTS.md).

## Documentation

- [docs/voice-pipeline-architecture.md](docs/voice-pipeline-architecture.md): what is being built and why.
- [docs/implementation_plan.md](docs/implementation_plan.md): the stages, their order and what "done" means.
- [docs/stages.md](docs/stages.md): the live tracker, with evidence and the decisions log.
- [docs/https.md](docs/https.md): installing and renewing the HTTPS certificate.
- [docs/secrets.md](docs/secrets.md): secrets (API keys and tokens) in the macOS Keychain; nothing secret is kept in `config/`.
- [docs/push.md](docs/push.md): phone alerts (Web Push) for results and agent questions when the page is closed.
- [docs/claude-setup.md](docs/claude-setup.md): turning on the optional Claude agent (an API key; it's a stub until then).
- [docs/hermes.md](docs/hermes.md): connecting the assistant to a Hermes Agent installation (any; `services/hermes/` is only a test setup).
