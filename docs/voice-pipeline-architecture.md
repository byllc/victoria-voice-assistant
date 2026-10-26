# Home Voice Assistant Architecture (Draft v2.9)

Owner: Bill · Updated: 2026-10-24 · Status: draft; open items in §13

## 1. Goal

A private, low-latency voice assistant for our household that replaces Amazon Alexa. It answers questions, runs everyday tools (weather, web search, timers, smart-home control, and more), and hands larger jobs to named AI agents on request ("Ask Hermes to plan dinners for the week", "Ask Claude what to make with leeks").

Clients are a web chat client with voice or text, and ESP32 voice satellites with on-device wake word (Future Proof Homes Satellite1 and others). The pipeline is built and used through the web client first; satellites are added last, as an optional phase (see the implementation plan).

**Version 1 must cover** the Alexa features we use most:

- **Weather:** current conditions and forecasts, for home or a named place.
- **Music:** from Spotify and from our Plex music library, played in the room we're in.
- **Timers:** several at once, each with a name ("pasta timer", "laundry timer").
- **Web search** for anything current, and **"Ask \<agent\>"** for bigger jobs.

## 2. Principles

- **Fast model first.** If a tool can resolve the request, the fast model calls it and answers. No agent involved.
- **Agents by name.** "Ask \<agent\> to …" goes straight to that agent. The fast model can also offer an agent when no tool fits, but it never hands off silently.
- **Loose coupling.** Models, tools, and agents are declared in config files and reached through standard protocols (OpenAI-compatible chat, Anthropic API, MCP, A2A, HTTP). Adding or swapping one needs no code change.
- **Any model, any server.** Every model definition names the server it runs on, local (LM Studio) or cloud (Anthropic). Pipeline roles and agents refer to models by name.
- **Smart home is just a tool.** Home Assistant is one tool provider among several, not the centre of the system.
- **Ask rather than guess.** Ambiguous requests get one clarifying question.
- **Confirm before risky actions** (locks, garage, alarm, purchases, sending messages).
- **Private by default.** Audio never leaves the house; STT, TTS, and VAD run in-process. Text goes to a cloud model only when we address a cloud agent by name, or accept an offer to use one.
- **Pipecat built-ins first.** Where Pipecat already provides a feature, use it as-is to start. Replace a built-in only when measurement shows a real need, and record why.
- **Swappable engines.** Speech engines sit behind interfaces so they can be changed after benchmarking.

## 3. Deployment

Version 1 runs on a Mac mini, M4 Pro, 64 GB unified memory.

- **Assistant service** (Python, Pipecat-based): client adapters, pipeline, VAD, STT, TTS, and the model, tool, and agent registries.
- **LM Studio:** serves local models over its OpenAI-compatible API (MLX engine on Apple Silicon).
- **SearXNG** and **Playwright** with headless Chromium: local web search and page reading (§8, built-in web search).
- **Music Assistant (external):** open-source music server that connects to Spotify and Plex and plays to speakers in each room. It runs wherever suits it best (for example as the Home Assistant add-on, or on another Linux machine), not necessarily on the Mac mini. The assistant only needs its URL and token (§8, built-in music).
- **Plex Media Server:** existing music library, wherever it runs today.
- **Web interface:** a separate process that serves the example web client for chat and voice, and handles login and HTTPS (§4.1).
- **Agents:** Hermes and other agent harnesses run on the Mac mini as their own services. Agents that use a local model get it from LM Studio; cloud models are reached directly.

### Runtime: native first, no Docker

Docker Desktop runs every container inside one always-on Linux VM, which costs memory and CPU on a machine that needs both for models. Services run **natively on macOS under `launchd`** by default. Apple's `container` tool is used only where a service needs isolation more than it needs the local network.

| Service | Runs as | Why |
|---|---|---|
| Assistant service | Native Python venv, `launchd` | Needs Metal (MLX Whisper) and direct LAN access to the satellites. |
| LM Studio | Native app, server started at login; models loaded with `lms load` | Needs the GPU; containers have no Metal access. |
| Web interface | Native Python venv, Uvicorn, `launchd`; separate process from the assistant service | A crash or update in the UI never interrupts voice. |
| SearXNG | Native Python venv, served by Granian, `launchd`, bound to localhost | Pure Python, installs cleanly from source. Valkey (its optional cache and rate limiter) isn't needed for a single local client. |
| Playwright / Chromium | Inside the assistant service (Python package plus its own Chromium download) | No separate service needed. |
| Hermes and other agents | Native under their own macOS user, **or** an Apple container | Agents with shell and file tools are the one place isolation matters more than overhead (below). |
| Music Assistant | Not on the Mac mini by default; located by URL in config | See §8, built-in music. |

**Apple containers** run each Linux container in its own lightweight VM. Memory is allocated as the container uses it rather than reserved up front, so a small service costs little, but each container still has its own Linux kernel and a startup of about a second. Practical limits as of today: no host networking, no automatic restart policy (wrap the container in a `launchd` job), macOS 26 as the supported target, and a pre-1.0 tool whose minor versions can break things. That makes them a good sandbox for an agent and a poor fit for anything that needs multicast, the GPU, or low-level LAN access.

**Agent isolation, two options:**
- **Native, separate macOS user:** lowest overhead; the agent's shell and file tools can only reach that user's home folder. Simple to set up, weaker isolation.
- **Apple container:** the agent runs in its own VM with only the folders we mount into it, and reaches LM Studio and the assistant over the container network. Stronger isolation for an agent that runs shell commands. Recommended for Hermes once it's given shell access from voice.

Everything starts under `launchd`, sleep is disabled via `pmset`, and models are loaded at startup and pinned (§7).

**Configuration folder.** All configuration files live in a `config/` folder, never in the repository root: `config/models.yaml` (§7), `config/tools.yaml` (§8), `config/agents.yaml` (§9), `config/searxng/settings.yml`, and `config/.env` for secrets (git-ignored). The service reads `config/` by default, and `ASSISTANT_CONFIG_DIR` points it elsewhere. Throughout this document, `models.yaml`, `tools.yaml`, and `agents.yaml` mean the files in that folder.

| Component | Memory estimate |
|---|---|
| Whisper large-v3-turbo (MLX) | 1–2 GB |
| Kokoro (82M, ONNX) | < 1 GB |
| Fast model, ~30B MoE at 4-bit | ~17–20 GB |
| Fallback fast model (small) | 2–5 GB |
| Agent harnesses (Hermes etc., excluding models) | 1–3 GB |
| SearXNG and a warm Chromium | ~1 GB |
| Web interface | < 0.2 GB |
| OS and headroom | ~10 GB |
| **Left for local agent models** | **~20–25 GB** |

That leaves room for one mid-size local agent model (for example a ~30B model at 4-bit) loaded on demand in LM Studio, or several agents sharing one model. Agents that need a larger model should use a cloud model.

## 4. Components

- **Satellite adapter:** the satellites run ESPHome firmware, and ESPHome devices don't dial out; a client connects *to* them over the ESPHome native API. The adapter does this with `aioesphomeapi`. It subscribes to voice-assistant requests, receives 16 kHz mono audio, drives the LED states, serves replies as an HTTP URL for the device's media player, and pushes announcements (timers, finished agent tasks). Device hardware and firmware choices, including a possible backend-neutral satellite protocol, are covered in Appendix A.
- **Chat adapter:** WebRTC (Pipecat SmallWebRTC) for voice, with echo cancellation; a websocket for text and events. Used by the web client (§4.1) and any future app.
- **Pipeline (Pipecat), in-process:** Silero VAD → `WhisperSTTServiceMLX` → address check → fast model → `KokoroTTSService`, streamed sentence by sentence.
- **Model registry:** `models.yaml`: servers, models, pools of paid models, and the local decision engine (§7).
- **Tool registry:** `tools.yaml` (§8).
- **Agent registry:** `agents.yaml` (§9).
- **Built-in services** (§8): web search, weather, timers, and music control. These are the core Alexa features, so they live in the assistant service with their own config blocks, shared by the fast model and model agents.
- **Task manager and notification service:** tracks background agent tasks and delivers results, questions, and alerts through notifications with priorities, quiet hours, and a pending indicator (§10).
- **Session store:** per-device context (room, recent turns, pending question) and the device registry.

### 4.1 Web interface and example client

The project includes a web interface that runs as its **own process**, separate from the assistant service. It serves an example web client for text chat and voice, usable from a phone, tablet, or computer. It is both a daily-use client and the reference for anyone building another client.

**Why a separate process:** the UI can crash, be restarted, or be updated without touching voice; the assistant service stays bound to localhost with no direct exposure to the network; and HTTPS, login, and static files stay out of the real-time audio process.

**How the pieces connect:**

| Path | Route | Notes |
|---|---|---|
| Browser → web interface | HTTPS: the client app, login, and session tokens | The only process facing the LAN. |
| Browser → assistant service (text and events) | Websocket, proxied by the web interface (`/ws`) | Chat messages, streamed replies, and live events. |
| Browser → assistant service (voice) | WebRTC. Signaling (`/api/offer`) is proxied by the web interface; audio flows directly between the browser and the assistant service. | Keeps audio off the web process; WebRTC gives echo cancellation and jitter handling. |

The web interface passes a signed session token with each proxied request, so the assistant service knows which household member and which "device" (browser) it is talking to.

**Example client features:**

- **Chat:** typed messages with streamed replies, and the full text of long answers that were summarized for speech.
- **Voice:** push-to-talk and hands-free modes. Turn detection happens on the server (Silero VAD), and live transcripts are shown as text.
- **Clarifying questions** appear as buttons.
- **Timers** with live countdowns, and the ability to cancel them.
- **Task dashboard:** live status of every long-running agent task (below).
- **Notifications:** a list with a badge for pending items, full text of agent results, and Web Push to phones when the page isn't open (§10.3).
- **Stub setup notices** with their full setup steps (§8).
- **Now playing** music, with play, pause, and skip.
- **Room picker:** the browser acts as a device in a chosen room, so "play music here" and timers use that room.

**Task dashboard.** A view in the example client, not a separate app. It shows every agent task, updated live over the client's existing websocket:

| Column | Content |
|---|---|
| Status | Queued, running, waiting for an answer, finished, failed, cancelled, or timed out |
| Request | What was asked, and by which room or device |
| Agent and model | For example "Claude (Sonnet)", including which model the decision engine picked from a pool (§7.1) |
| Time | Started, and elapsed or total |
| Progress | The latest progress event from the agent ("searching the web", "reading 3 pages"); expandable to the full event history |
| Cost | Tokens and estimated cost for paid models, from the spend log |

Actions on a task:

- **Answer** an agent's question inline, when the task is waiting.
- **Cancel** a running task.
- **Retry** a failed task, or **re-send** it to a different agent or a stronger model in the pool.
- **Open** the full result, with its notification marked acknowledged.

Above the list, a summary line shows running and waiting counts, this month's spend against each pool's budget, and a health strip for the services tasks depend on (LM Studio, Hermes and other agents, SearXNG, Music Assistant). Finished tasks stay in a searchable history.

The same information is available by voice through a built-in `tasks` tool: "What's Hermes working on?", "Is the meal plan done?", "Cancel the research task."

**HTTPS on the LAN:** browsers only allow microphone access on secure pages (or `localhost`). Options: a local certificate authority with `mkcert` (install its root certificate on each household device), or Tailscale's HTTPS certificates if the household already uses Tailscale, which also allows safe access from outside the home.

**Login:** a simple household login (or one account per person, if speaker identity is adopted later), with long-lived sessions on trusted devices. The client is not exposed to the internet.

**Build:** the example client is a small TypeScript app (Vite) using Pipecat's JavaScript client SDK with its SmallWebRTC transport. The web interface is a small Python FastAPI app on Uvicorn that serves the built files and proxies to the assistant service. It can be installed as a home-screen web app (PWA) on phones.

### 4.2 Speech in one process: safeguards and turn detection

**Why in-process.** STT, TTS, and VAD run inside the assistant service because Pipecat is built to run them inside its pipeline, giving one thing to deploy and nothing to package and send between processes. Speed is not the main reason: a hop to another local process costs about a millisecond.

**Pitfalls and safeguards:**

| Pitfall | Safeguard |
|---|---|
| **Blocking the event loop.** Pipecat runs on one asyncio loop that carries audio for every device. Python work around inference (Kokoro text preparation, resampling) can hold it and make every stream stutter. | Run all speech inference on background threads. Measure event-loop lag continuously and log any stall over ~20 ms. |
| **Simultaneous requests.** One Whisper and one Kokoro instance serve all devices, and MLX models shouldn't be called from several threads at once. | A lock or queue per model; requests from a second room wait their turn. Log queue time. |
| **One crash stops everything.** A native-code crash (Metal/MLX, ONNX Runtime, speech libraries) takes down every satellite, the chat connection, and the fast path. A leak in one model grows the whole process. | `launchd` restarts the service automatically. Timers and pending agent tasks are persisted in SQLite. Monitor process memory. |
| **Slow restarts.** Restarting to fix tool code also reloads the speech models. | Load models in parallel at startup and accept requests only once they're ready; keep tool reloads (`tools.yaml`) restart-free. |
| **Dependency clashes.** Whisper (MLX), Kokoro (ONNX Runtime and its pronunciation libraries), Pipecat, Playwright, and tool libraries share one Python environment. | Pin every version with a lock file; upgrade one component at a time. |

GPU contention with LM Studio exists whether speech runs in-process or not; separate processes would not change it.

**Escape hatch.** STT and TTS sit behind interfaces. If crashes, dependency clashes, or stuttering appear, either can move to a separate local process over a Unix socket at a cost of about a millisecond per call, without changing the rest of the pipeline.

**Voice activity detection (VAD).** VAD decides, for each ~30 ms slice of audio, whether someone is speaking. It is different from the wake word (one specific phrase, detected on the satellite) and from STT (turning speech into text). Silero VAD is a tiny neural network that runs on the CPU in well under a millisecond per slice. It has three jobs:

- **End of turn:** after the wake word, speech followed by a stretch of silence ends the turn and sends the audio to Whisper.
- **Protecting Whisper:** only speech is transcribed, which prevents most of Whisper's tendency to invent words from silence or noise.
- **Interruptions:** speech detected while the assistant is talking can stop playback and start listening. This depends on the satellite's echo cancellation removing the assistant's own voice first.

**The silence threshold is the key setting.** Too short cuts people off mid-pause ("set a timer for… um…"); too long makes every reply feel slow. With silence alone, this wait is often the largest single part of perceived latency, likely larger than Whisper's time on the M4 Pro.

**Turn detection: Smart Turn on top of VAD (default).** Smart Turn is Pipecat's open-source turn-detection model (BSD-2 licence, about 8 MB int8 on CPU, 23 languages). It works *with* VAD rather than replacing it: VAD finds each pause, and Smart Turn then listens to the last few seconds of the turn and judges from how the speech sounds (intonation, trailing "um"s) whether the speaker has finished.

- **Settings:** VAD stop after 0.2 s of silence (Pipecat's recommendation with Smart Turn); Smart Turn decides at each pause; if it says "not finished" but silence continues, a fallback timeout ends the turn anyway. Pipecat's default fallback is 3 s, which is long for spoken commands; start around 1.5–2 s and tune.
- **Cost:** one small model run per pause, reported at roughly 10–100 ms on CPU depending on the processor. Expected to be near the low end on the M4 Pro; to be measured.
- **Known weakness:** the project notes that very short clips work poorly, and many home commands are very short ("stop", "skip", "pause"). Test these specifically. If Smart Turn holds them open until the fallback timeout, shorten the fallback or skip Smart Turn for clips under about a second.

## 5. Request flow

1. The wake word fires on-device; audio streams to the satellite adapter (or arrives from the chat client).
2. VAD ends the turn; Whisper transcribes.
3. **Pending question?** If the assistant just asked something, the reply resolves it first. Yes/no is matched without calling a model.
4. **Agent address?** A deterministic match on agent names and aliases ("ask Hermes…", "ask Claude…") sends the rest of the utterance straight to that agent. No model call.
5. **Fast model** gets the transcript, the device's room, recent turns, and the tools, and returns one of:
   - **Tool call** → tool runs → short spoken answer ("It's 54 and cloudy, rain after 4").
   - **Answer** directly, for simple knowledge that doesn't change ("There are 8 ounces in a cup").
   - **Clarify:** one question, then the mic reopens.
   - **Ask agent:** the user implied an agent without the exact phrase ("have Claude look into it").
   - **Offer agent:** no tool fits → "I can't do that myself. Want me to ask Hermes?"
6. **Agent handoff:** see §10.

Target: tool-path reply starts within about 1.5 s of end of speech, plus the tool's own time. Measure each stage.

## 6. Fast model

- **Model:** a mixture-of-experts model with few active parameters (around 3B active, ~30B total, 4-bit MLX) suits Apple Silicon: good tool-calling quality at small-model speed. Benchmark candidates on the test phrases in `tools.yaml`, scoring correct outcome and time-to-first-token.
- **Serving:** LM Studio, referenced through the `router` role in `models.yaml`. Switching models or servers is a config change.
- **Fallback:** if the router model times out or its server is down, the `router_fallback` model takes over.
- **Prompt caching:** system prompt and tool schemas are static and first, so the server can reuse the cached prefix every turn. Dynamic context goes after it.
- **Built-in tools** (always present, not in config): `clarify(question, options)`, `ask_agent(agent, task)`, `offer_agent(agent, summary)`. These keep the output structured, so the pipeline never parses free text to decide what happened.
- **Pending questions:** clarify, offer, and confirm store a question on the session with a ~10 s expiry. At most one clarifying question per request; if still unresolved, offer the default agent. Silence cancels.
- **Follow-up listening:** satellites must reopen the mic after a question without a new wake word. To verify in the device test.
- **Speech style:** replies are short and spoken-friendly; long results are summarized, with the full text sent to the chat client if one is open.

### How tool calling runs

The fast path needs no agent harness. Pipecat's LLM service runs the tool loop:

1. The registry builds a Pipecat `ToolsSchema` from `tools.yaml` and hands it to the LLM context. The fast model is reached through Pipecat's OpenAI LLM service with `base_url` pointed at LM Studio.
2. The model emits a tool call. Pipecat runs the handler, adds the result to the context, and (by default) calls the model again to phrase the spoken reply.
3. Multiple calls in one turn ("turn off the kitchen and dining room lights") run in parallel, and the model is called once after all of them finish.

How each tool kind becomes a Pipecat tool:

| Kind | Mapping |
|---|---|
| `mcp` | Pipecat's `MCPClient.tools()`, one client per MCP server, kept connected. `tools_filter` exposes only the listed tools; `tools_arguments` fixes hidden arguments (such as home location); `tools_output_filters` trims long results before the model sees them. |
| `ha_service`, `http`, `python` | A `FunctionSchema` with a bundled handler generated from the YAML entry. |
| `stub` (explicit or automatic) | A `FunctionSchema` whose handler logs the call, speaks the notice, and sends the setup text to chat, with `run_llm=False`. |
| Built-ins (`clarify`, `ask_agent`, `offer_agent`) | `FunctionSchema` handlers that set session state and speak directly, with `run_llm=False` so no second model call is made. |

Details:

- **Second model call:** re-running the model after a tool result costs a full extra round trip. Each tool has a `reply` option: `model` (default; the model phrases the result, right for weather and search), `template` (speak a fixed string such as "Done" or "{name} is locked", with `run_llm=False`), or `silent`. Most smart-home tools should use `template`.
- **Timeouts:** each tool's `timeout_s` maps to Pipecat's per-tool `timeout_secs`. On expiry the call is cancelled and the model says it couldn't reach the service.
- **Interruptions:** fast-path tools stay synchronous (`cancel_on_interruption`), so a barge-in cancels them cleanly.
- **Loop guard:** a counter on the session caps tool rounds per request (about 3). Past the cap the assistant offers an agent instead of looping.
- **Reload:** when `tools.yaml` changes, the new schema is pushed with `LLMSetToolsFrame`; no restart.
- **Model support:** tool calling depends on the model's chat template. Choose models LM Studio lists as supporting tool use, and confirm streamed tool calls work in the benchmark.

## 7. Model registry (`models.yaml`)

Five parts:

- **Servers:** where models run. Type `openai` covers any OpenAI-compatible server (LM Studio, Ollama, vLLM, Hermes, OpenRouter, LiteLLM). Type `anthropic` uses Anthropic's native API.
- **Models:** a name, the server it runs on, the server's model ID, default parameters, and for paid models a price.
- **Pools:** a set of interchangeable models for one job, from cheapest to most capable. The decision engine picks one per request (§7.1).
- **Decider:** the local decision engine used for model selection and, optionally, routing.
- **Roles:** which model or pool the pipeline uses for each internal job.

```yaml
servers:
  lmstudio:
    type: openai
    base_url: http://127.0.0.1:1234/v1
    api_key: lm-studio                # LM Studio doesn't check it
  hermes:
    type: openai
    base_url: http://127.0.0.1:8642/v1
    api_key: ${HERMES_API_KEY}
  anthropic:
    type: anthropic                   # native API (see note below)
    api_key: ${ANTHROPIC_API_KEY}

models:
  fast:
    server: lmstudio
    model: <30B-MoE-MLX-id>           # chosen after benchmarking
    params: { temperature: 0.2, max_tokens: 200 }
    timeout_s: 5
    pin: true                         # load at startup, never evict
  fast-small:
    server: lmstudio
    model: <small-MLX-id>
    pin: true
  hermes:
    server: hermes
    model: hermes-agent
  claude-haiku:
    server: anthropic
    model: claude-haiku-5-5           # confirm current IDs before use
    price: { input: <usd_per_mtok>, output: <usd_per_mtok> }
  claude-sonnet:
    server: anthropic
    model: claude-sonnet-5-5
    price: { input: <usd_per_mtok>, output: <usd_per_mtok> }
  claude-opus:
    server: anthropic
    model: claude-opus-5-5
    price: { input: <usd_per_mtok>, output: <usd_per_mtok> }

pools:
  claude:                             # "Ask Claude" uses this pool
    members:                          # cheapest first
      - { model: claude-haiku,  good_for: "quick facts, short answers, simple rewrites, unit and date questions" }
      - { model: claude-sonnet, good_for: "explanations, planning, recipes, comparisons, multi-step questions" }
      - { model: claude-opus,   good_for: "hard reasoning, long research, careful writing, high-stakes questions" }
    default: claude-sonnet            # used if the decider is unsure or unavailable
    min_confidence: 0.6
    spoken_overrides:                 # saying these picks a member directly
      opus: claude-opus
      haiku: claude-haiku
    escalate_on_failure: true         # retry one step up if the cheaper model refuses or says it can't
    budget:
      monthly_usd: 30
      downgrade_at: 0.8               # above 80% of budget, never pick above the default
      stop_at: 1.0                    # at 100%, say so and offer a local agent instead

decider:
  type: llm_choice                    # local model answering a constrained multiple-choice question
  model: fast-small
  # type: decision_model              # a dedicated local decision model, when one is chosen
  # server: <local-decision-server>

roles:
  router: fast
  router_fallback: fast-small
  summarizer: fast                    # shortens long agent replies for speech
  pre_router: off                     # optional decision-engine routing stage (§7.1); off until benchmarked
```

Notes:

- **Pinning in LM Studio:** by default, LM Studio unloads idle models after a timeout, and loading a second on-demand model evicts the first. Either could put a multi-second model load in front of a voice request. Models marked `pin: true` are loaded at startup with `lms load` (no idle timeout), which on-demand loading doesn't evict.
- **Claude:** Anthropic offers an OpenAI-compatible endpoint (`https://api.anthropic.com/v1/`), but describes it as intended for testing rather than production, and it doesn't support prompt caching. The native `anthropic` server type is preferred. Some Claude models also reject certain sampling parameters, so each model's `params` are kept per model rather than global.
- **Prices** are entered from the provider's current price list and used only for our own spend tracking.
- **Secrets** come from the environment (`${…}`), never from the file itself.

### 7.1 Decision engine

A decision engine answers a question by choosing from a fixed set of answers with a confidence for each, rather than generating text. It runs locally, so it adds no cost and sends nothing off the machine. It has two jobs.

**Job 1: pick the cheapest paid model that will do.** When a request goes to a pool (for example "Ask Claude …"), the decider sees the request and each member's `good_for` description and is asked: *which is the cheapest model likely to handle this well?* Then:

1. A spoken override wins ("ask Claude Opus to …" picks Opus).
2. Budget rules apply: past `downgrade_at`, nothing above the default; past `stop_at`, no paid call ("We've hit this month's Claude budget. Want me to ask Hermes?").
3. If the top choice's confidence is below `min_confidence`, use the pool's `default`.
4. If `escalate_on_failure` is on and the chosen model refuses or says it can't do the task, retry once with the next member up.

Every choice, its confidence, the tokens used, and the computed cost are logged. "How much have we spent on Claude this month?" is answered from that log, and the log shows whether the decider is choosing well.

**Job 2 (optional): route before the fast model.** With `pre_router` on, the decider first classifies the transcript into the tool names, `clarify`, the agent names, and `none`:

- **Confident, tool needs no arguments** ("stop", "pause the music", "skip", "what timers do I have"): run it directly, with no language-model call.
- **Confident, tool needs arguments:** call the fast model with only that tool, so its prompt is shorter.
- **Two close choices:** ask which one was meant.
- **`none` wins:** offer the default agent.

This stays off until benchmarked against the single-pass fast model on the test phrases. It only helps if it is faster overall.

**Engine types**, behind one interface:

| Type | What it is | Notes |
|---|---|---|
| `llm_choice` | A small local model from LM Studio, asked a multiple-choice question with the answer constrained by a JSON schema (structured output). | Works today with no new software. Confidence comes from token probabilities if the server returns them, otherwise from asking for a confidence. |
| `decision_model` | A dedicated local decision model that returns a probability per answer. | Faster and better calibrated if a good local one is available. The hosted Jev model works this way but isn't local; AWS's open-source Strands Decider 2B has been reported as a local option and needs evaluating. |

## 8. Tool registry (`tools.yaml`)

Tools the fast model (and model agents, §9) may call, reloaded on save, validated before use (an invalid file keeps the previous set). Each tool has a name, a description the model sees, parameters, and a kind:

| Kind | Use |
|---|---|
| `mcp` | A tool from an MCP server. The main way to add capabilities: weather, calendar, music, and smart home (Home Assistant has an MCP Server integration). Only listed tools are exposed. |
| `ha_service` / `ha_script` | Optional direct Home Assistant calls for the most frequent smart-home actions, where latency matters. |
| `http` | A simple REST call with a response template. |
| `python` | A function inside the service (timers, alarms, unit conversions). |
| `builtin` | A core service that is part of the assistant itself: web search, weather, timers, and music (below). |
| `stub` | A planned tool with no implementation yet (below). |

Per-tool options: `confirm`, `enabled`, `timeout_s`, `reply` (`model`, `template`, or `silent`; §6), `examples` (test phrases), `entities` (Home Assistant label scope, for smart-home tools), and for stubs, `notice`, `setup`, and `then`.

### Stub tools

A tool can be declared before it is built. The model sees a stub like any other tool (name, description, parameters), so routing works and can be tested now. When a stub is called:

1. The assistant speaks the short `notice` ("Home control isn't set up yet. I've sent the setup steps to the chat."). No second model call.
2. The full `setup` text goes to the chat client (when one is connected) and always to the service log. It is not read aloud.
3. The call and its arguments are logged. That record shows what people actually ask for and what parameters the model produces, which helps design the real tool.
4. Optionally, `then` follows the notice: `offer_agent` asks "Want me to ask Hermes instead?", and `ask_agent: <name>` hands the request straight to that agent (for example Alexa, §9).

**Automatic stubs.** Any tool whose implementation can't be resolved (a missing Python handler, an unknown MCP server or tool, an unreachable server at startup) becomes a stub instead of failing the whole file. The default notice is "\<name\> isn't set up yet," and the setup text says exactly what is missing ("handler `timers.start` not found"). Startup logs a warning for each one, and `tools status` lists every stub and why. An explicit `kind: stub` is for tools that are planned on purpose.

Turning a stub into a real tool is a change of `kind` plus its connection details. The name, description, parameters, and examples stay the same, so routing and tests carry over.

```yaml
tools:
  - name: weather
    description: Current conditions and forecast for home or a named place.
    kind: builtin
    service: weather
    examples: ["what's the weather tomorrow", "do I need an umbrella", "weather in Boston this weekend"]

  - name: web_search
    description: Search the web for current facts, news, scores, hours. Returns short snippets.
    kind: builtin
    service: search                  # configured under `search:` below
    timeout_s: 4

  - name: web_read
    description: Read one web page from a search result when the snippets don't answer the question.
    kind: builtin
    service: search.read
    params: { url: { type: string } }
    timeout_s: 4

  - name: timer
    description: Start, cancel, change, or check named countdown timers.
    kind: builtin
    service: timers
    params:
      action:   { type: string, description: "start, cancel, add_time, or status" }
      name:     { type: string, description: "e.g. pasta, laundry; optional" }
      duration: { type: string, description: "e.g. 10 minutes, 1 hour 30 minutes" }
    reply: { template: true }          # spoken by the timer service, no second model call
    examples: ["set a pasta timer for 10 minutes", "how long is left on the laundry",
               "cancel the pasta timer", "add 5 minutes to the rice timer", "what timers do I have"]

  - name: music
    description: Play, pause, skip, or change volume of music; say what's playing.
    kind: builtin
    service: music
    params:
      action: { type: string, description: "play, pause, resume, next, previous, volume, now_playing" }
      query:  { type: string, description: "artist, album, track, playlist, or genre" }
      type:   { type: string, description: "artist, album, track, playlist, or radio; optional" }
      source: { type: string, description: "plex or spotify; optional" }
      room:   { type: string, description: "optional; defaults to the room of the device" }
      level:  { type: integer, description: "volume 0-100, for action volume" }
    reply: { template: true }
    examples: ["play some Miles Davis", "play my Discover Weekly on Spotify",
               "play Kind of Blue from Plex in the kitchen", "skip this", "turn it down"]

  - name: home_control               # planned, not built yet
    description: Control or check anything in the home except locks: lights, climate, covers, media, scenes.
    kind: stub
    params:
      action: { type: string, description: "turn_on, turn_off, set, or query" }
      target: { type: string, description: "device, room, or area" }
      value:  { type: string, description: "brightness, temperature, etc." }
    notice: "Home control isn't set up yet. I've sent the setup steps to the chat."
    setup: >
      Pick an implementation and change `kind`: `mcp` with server `homeassistant`
      (Home Assistant's MCP Server integration), or `ha_service` for direct calls.
      Add the Home Assistant URL under mcp_servers and set HA_TOKEN in the environment.
    examples: ["turn off the kitchen lights", "is the heat on upstairs"]

  - name: lock_door
    description: Lock a door.
    kind: ha_service
    service: lock.lock
    entities: { label: voice, domains: [lock] }
    confirm: true
    reply: { template: "{name} is locked." }

search:
  provider: searxng
  url: http://127.0.0.1:8888         # local SearXNG, JSON format enabled
  engines: [duckduckgo, brave, wikipedia]   # fewer, faster engines
  max_results: 3
  snippet_chars: 300
  cache_ttl_s: 600
  read:
    fetcher: auto                    # plain HTTP first, Playwright if the page needs JavaScript
    max_chars: 4000                  # text handed to the model per page
    browser_pool: 2                  # warm Playwright pages kept open
    block: [image, font, media, stylesheet]

weather:
  provider: open-meteo               # no API key needed
  home: { name: Home, lat: 0.0, lon: 0.0 }   # set to our location
  units: imperial
  cache_ttl_s: 600

timers:
  max_per_device: 10
  ring_max_s: 300                    # stop ringing after 5 minutes
  sound: chime.flac

music:
  server: http://<music-assistant-host>:8095   # wherever Music Assistant runs
  token: ${MUSIC_ASSISTANT_TOKEN}
  source_order: [plex, spotify]      # where to look when no source is named
  rooms:                             # satellite → speaker that plays music there
    kitchen: { satellite: kitchen-sat, player: <music-assistant-player-id> }
    office:  { satellite: office-sat,  player: <music-assistant-player-id> }
  duck_level: 20                     # music volume while the assistant listens or speaks

mcp_servers:
  homeassistant: { url: http://ha.local:8123/mcp_server/sse, token: ${HA_TOKEN} }

tests:
  - { say: "turn off the kitchen lights", expect: tool:home_control }  # passes while still a stub
  - { say: "set a timer", expect: clarify }                # no duration given
  - { say: "cancel the timer", expect: clarify }           # when two timers are running
  - { say: "play something relaxing", expect: tool:music }
  - { say: "ask claude what to make with leeks", expect: agent:claude }
  - { say: "book me a flight to Denver", expect: offer_agent }
```

Keep the fast model's active set small (under ~15). A `tools test` command runs the examples and tests after each change.

### Built-in web search

Web search covers anything current (scores, store hours, news, dates) and is likely to be one of the most-used tools. It is also one of the slowest, so it is part of the assistant service rather than a separate MCP server:

- **In-process:** no extra process hop or protocol layer on a latency-sensitive path.
- **Shaped for speech:** results are cut to a few short snippets (title, date, source, ~300 characters) before the model sees them. This keeps the model's second call short, which is the main cost on this path.
- **Cached:** repeated queries within `cache_ttl_s` are answered from a short-lived cache.
- **Shared:** the fast model and model agents use the same handler, timeouts, and logging.

**Local search stack.** Search runs on the Mac mini with no search API accounts:

- **SearXNG** (self-hosted metasearch, running natively on the Mac mini) finds results. The service calls its JSON API, which must be enabled in SearXNG's `settings.yml`. SearXNG sends each query to the upstream engines it is configured for (DuckDuckGo, Brave, Wikipedia, and so on) without cookies, an account, or browser fingerprinting, and spread across engines. Queries still leave the house, and each engine sees our home IP address, but none of them gets a logged-in history tied to us. Fully offline search isn't possible for current information.
- **Page reading** (`web_read`) fetches a result page and extracts its main text:
  1. Plain HTTP with `httpx` and text extraction with `trafilatura`. Fast, and enough for most pages.
  2. **Playwright** (headless Chromium) when the page needs JavaScript to render, or plain extraction returns too little text.

**Keeping it fast enough for voice:**

- **Two steps, not one.** `web_search` returns only snippets, which answer most quick questions (scores, hours, dates). The model calls `web_read` only when the snippets don't answer it, and counts it toward the per-request tool limit.
- **Fewer engines.** SearXNG waits for its slowest engine, so `engines` lists only a few quick ones, with SearXNG's outgoing request timeout set to about 2 seconds.
- **Warm browser.** Playwright keeps one Chromium instance and a small pool of pages open, so a read doesn't pay the browser launch time. Images, fonts, media, and stylesheets are blocked, and each read has a hard timeout.
- **Trimmed text.** A read hands the model at most `max_chars` of extracted text.
- **Expect about 1–2 s for a snippet answer and 2–4 s with a page read**, on top of the model. To be measured.

If SearXNG or a page read fails or times out, the assistant says so ("I couldn't search right now") rather than guessing from model memory.

**Agents use the same local search.** Model agents such as Claude get `web_search` and `web_read` as tools, so their searches also go through SearXNG. An agent can set `search: native` to use its provider's server-side search instead (Anthropic's web search tool, for Claude), which is faster for long research but sends the queries to that provider. The default is `search: builtin`.

**Page content is untrusted.** Text from web pages can contain instructions aimed at AI models. It is passed to models clearly marked as quoted web content, and pages are never allowed to trigger tools with side effects (locks, purchases, messages) without confirmation.

MCP remains the way to add other capabilities. The built-in services get special treatment only because they are the core Alexa features and are used constantly.

### Built-in weather

- **Provider:** Open-Meteo, which needs no API key, with its geocoding API for named places ("weather in Boston"). The provider sits behind an interface, so the US National Weather Service or a Home Assistant weather entity can replace it.
- **Queries:** current conditions, today, tonight, tomorrow, a named day, the weekend, and "will it rain" style questions over the next few hours.
- **Shaped for speech:** the handler returns only the fields the question needs (temperature, high and low, conditions, chance and timing of rain). The model phrases it in one or two sentences.
- **Cached** for 10 minutes per location.

### Built-in timers

Timers run inside the assistant service, so they work without the internet and ring on the device that set them.

- **Several at once,** up to `max_per_device` per device. Each has a name and an ID.
- **Naming:** the name comes from the request ("pasta timer"). Without one, it is named by duration ("10-minute timer"). A duplicate name gets a suffix and the reply says so ("Second pasta timer, 5 minutes").
- **Actions:** start, cancel, add or remove time, status of one ("how long on the laundry?"), and status of all ("You have two timers: pasta, 4 minutes; laundry, 32 minutes").
- **Ambiguity:** "cancel the timer" with more than one running gets a clarifying question listing them by name.
- **Ringing:** the device plays the chime and speaks the name ("Your pasta timer is done") on repeat until stopped, or for `ring_max_s`. Several timers finishing together are announced together.
- **Stopping:** saying "stop" without the wake word is the goal. ESPHome's micro wake word supports a "stop" model used for timers on other HA voice devices; whether the Satellite1 firmware enables it needs checking in the device test. Fallbacks: the wake word plus "stop", or the device's button.
- **Persistent:** timers are stored in SQLite so a service restart doesn't lose them.
- **Chat client:** shows running timers with a live countdown.

Timer replies are spoken by the timer service itself from templates, so they don't need a second model call.

### Built-in music

Music runs through **Music Assistant**, an open-source music server, rather than calling Spotify and Plex directly:

- **Spotify:** the Spotify Web API only controls playback on Spotify Connect devices, and the satellites aren't Connect devices. It also restricts development-mode apps: since February 2026 they need a Premium account owner and allow 5 users, which is fine for a household but limits what a home-built app can do. Music Assistant already has a Spotify provider that streams Spotify to any of its players.
- **Plex:** Music Assistant has a Plex provider that reads the library from the existing Plex Media Server. (Plexamp is a Plex player app; the library lives on the Plex server, which is what Music Assistant uses.)
- **Speakers:** Music Assistant plays to Sonos, AirPlay, Google Cast, Snapcast, Squeezelite, DLNA, Home Assistant media players, and Sendspin (its own protocol, with an ESPHome component for ESP32 devices). Rooms can be grouped for synced playback.

How the assistant uses it:

- **Control** goes through Music Assistant's Python client (`music-assistant-client`) over its API: search, play, pause, skip, volume, now playing.
- **Search order:** if no source is named, the library in `source_order` is searched first (Plex), then Spotify. "On Spotify" or "from Plex" in the request overrides it.
- **Room:** music plays in the room of the device that heard the request, using the `rooms` map. "In the kitchen" overrides it. The satellite's small speaker can be the player, but a real speaker in each room is better for music.
- **Ducking:** when the wake word fires, music in that room drops to `duck_level` and returns to normal after the reply.
- **Replies** are templated ("Playing Kind of Blue by Miles Davis from Plex"), with no second model call.

**Location is configuration.** The music tool expects Music Assistant to be running somewhere on the network and identified by `music.server` and `music.token`. It doesn't care whether that's the Home Assistant add-on, another Linux machine, or the Mac mini. Running it outside the Mac mini also sidesteps its need for host networking (it discovers speakers with mDNS and other multicast) and keeps it off the machine running the models.

If the server isn't configured or can't be reached at startup, the music tool becomes an automatic stub (§8, stub tools): "Music isn't set up yet," with setup steps sent to the chat client naming the missing setting. Until then, the stub can relay music requests to Alexa (§9, relaying to Alexa).

## 9. Agent registry (`agents.yaml`)

An agent is anything the assistant can hand a request to by name. Agents never see audio or devices. Four kinds:

| Kind | What it is | Who runs the tool loop |
|---|---|---|
| `model` | A model from `models.yaml` with its own system prompt and an optional list of tools from `tools.yaml`. "Ask Claude" is this kind. | The assistant service |
| `harness` | An external agent behind an OpenAI-compatible endpoint, referenced as a model. Hermes is this kind. | The agent |
| `a2a` | An external agent speaking the Agent2Agent protocol, which is built for long-running tasks and push notification of results. | The agent |
| `relay` | Another voice assistant that takes a text command and responds on its own device, with nothing returned to us. "Ask Alexa" is this kind (below). | The other assistant |

```yaml
default: hermes                        # used for "offer agent"

agents:
  - name: hermes
    aliases: [hermes, herms]
    kind: harness
    model: hermes                      # from models.yaml
    mode: auto                         # wait up to wait_s, then background
    wait_s: 8

  - name: claude
    aliases: [claude, clod, cloud]     # common mis-transcriptions
    kind: model
    pool: claude                       # decider picks Haiku, Sonnet, or Opus per request (§7.1)
    system: You answer by voice for our household. Be brief and concrete.
    tools: [weather, web_search, web_read]
    search: builtin                    # local SearXNG; `native` would use Anthropic's search
    share_context: minimal             # task and room only, no history
    mode: wait

  - name: research
    aliases: [research, researcher]
    kind: a2a
    url: http://127.0.0.1:9000
    mode: background

  - name: alexa
    aliases: [alexa, alexis]
    kind: relay
    via: ha_service                    # Alexa Media Player in Home Assistant
    service: media_player.play_media
    content_type: custom               # "run this as if spoken"
    targets:                           # satellite room → Echo in that room
      kitchen: media_player.kitchen_echo
      office:  media_player.office_echo
    reply: "Asking Alexa."
```

**Contract.** Every kind maps to the same internal interface:

- **Request:** `task` (the user's words), `context` (room, device, speaker if known, recent turns, limited by `share_context`), `reply_style` (spoken, short), `task_id`.
- **Events back:** `progress`, `question` (becomes a pending question on the device), `result`, `error`. A `relay` agent only ever returns "sent" or `error`.

### Relaying to Alexa

"Ask Alexa to play some jazz" sends the words after "ask Alexa" to the Echo in the same room, as if someone had spoken them to it. The Echo answers or plays music itself. This is a bridge while our own tools are being built, and a way to reach anything Alexa already does well.

- **How:** Home Assistant's community **Alexa Media Player** integration can send an Echo a text command (`media_player.play_media` with `media_content_type: custom`). It can also start music directly with content types `SPOTIFY` or `AMAZON_MUSIC`. The relay agent calls this through Home Assistant.
- **Room:** the `targets` map picks the Echo in the room of the satellite that heard the request.
- **Reply:** our assistant says only "Asking Alexa." Alexa's answer and any music come from the Echo, not the satellite, and nothing comes back to us.
- **No Amazon audio:** only the text of the command goes to Amazon, the same as typing it into the Alexa app.

**Using Alexa as the music fallback.** Until Music Assistant is set up, the music stub can hand off to Alexa instead of only giving setup steps:

```yaml
  - name: music
    kind: stub
    notice: "Music isn't set up here yet."
    then: { ask_agent: alexa }         # "Asking Alexa." and relay the request
```

So "play some Miles Davis" works today through the Echo, and switching `kind` to `builtin` later moves it to Music Assistant with no change to how we ask. A stub's `then` can be `offer_agent` (ask first) or `ask_agent` (hand off directly).

**Caveats:** Alexa Media Player uses Amazon's unofficial app API. Its own documentation warns Amazon may cut off access at any time, and individual commands have broken after Amazon changes. It also needs an Amazon login in Home Assistant and a Home Assistant install with the integration. Treat it as a convenience, not something the core design depends on.

**Model agents run outside the live pipeline.** A `model` agent such as Claude may run for a while or in the background, so its tool loop runs in a small agent runner rather than in the live voice pipeline. Following the built-ins-first principle, the runner first tries Pipecat's own LLM services (OpenAI and Anthropic) in a separate text-only pipeline, falling back to a plain loop on the `openai` or `anthropic` SDK only if that proves awkward for background tasks. It uses the same tool handlers from the registry, so a tool defined once works for both the fast model and model agents. Its result comes back as a `result` event like any other agent.

Adding a new cloud model as an agent ("ask Gemini") is a server entry, a model entry, and an agent entry. No code.

Permissions belong to each agent's own configuration. Voice is an unauthenticated channel, so agents reached from voice should run with a restricted profile (no shell, confirmation for side effects).

## 10. Agent handoff, background tasks, and notifications

### 10.1 Handoff

1. Kokoro says a short acknowledgement ("Asking Claude").
2. Depending on the agent's `mode`:
   - `wait`: stream the result when it arrives.
   - `background`: "I'll let you know when it's done." The device returns to idle.
   - `auto`: wait up to `wait_s`, then switch to background.
3. Background results, agent questions, and errors become **notifications** (below). They are never just spoken into an empty room and lost.

### 10.2 Task lifecycle

Every handoff creates a task record in SQLite with its agent, model, origin room, timestamps, progress events, cost, and result. Its states are `queued`, `running`, `waiting` (for an answer), `finished`, `failed`, `cancelled`, and `timed_out`; state changes are published as events to the web client's task dashboard (§4.1) and to the notification service.

**Cancelling** depends on the agent kind: A2A agents support cancellation in the protocol; `model` agents are stopped by the assistant's own runner; `harness` agents behind an OpenAI-compatible endpoint are cancelled by closing the request stream, which stops the reply but may not stop work the agent already started. The dashboard says which applies.

### 10.3 Notification service

A notification service, part of the task manager, owns everything the assistant needs to tell us unprompted. Every notification is stored in SQLite before delivery, so a restart doesn't lose it.

**Sources:** agent results, agent questions, agent errors, timers (§8), and system notices (a stub tool that was called, a budget limit reached, a music server that went offline).

**Each notification has:** source, a one-sentence spoken summary, the full text, a priority, a target (the device or room that started it; a person once speaker identity exists), an expiry, and a state: `pending`, `delivered`, `acknowledged`, or `expired`.

**Priorities and delivery:**

| Priority | Examples | Delivery |
|---|---|---|
| `urgent` | Timer ringing; an agent question that blocks a task | Immediately on the target device. Waits only for the assistant to finish speaking; ducks music. Ignores quiet hours (timers) or falls back to chat during them (questions). |
| `normal` | Agent result; agent error | When the target device is idle: a chime, then the summary and an offer ("Hermes has finished the meal plan. Want to hear it?"). If someone is mid-conversation there, wait until the turn ends. During quiet hours: no sound, chat and indicator only. |
| `low` | System notices; stub calls | Chat and indicator only; never spoken unless asked. |

**Ask before reading.** By default a result is announced as one sentence plus an offer, not read out in full: long answers are tiring to listen to, and a room may have other people in it. "Yes" reads the spoken summary; the full text always goes to the chat client. Configurable per agent (`ask_first`, `speak_summary`, or `chime_only`).

**Pending indicator, Alexa-style.** While a notification is pending for a room, that satellite shows a notification LED colour, and the web client shows a badge. Then:

- "Any notifications?" or "What did I miss?" reads pending items for the room, newest first, and marks them acknowledged.
- "What did Hermes say?" replays the last result from that agent.
- After any other request in that room, the assistant adds one short line: "By the way, you have a message from Hermes." This happens once per notification, not after every reply.

**Grouping:** several notifications ready at the same time are announced together ("Two updates: Hermes finished the meal plan, and Claude answered your question about leeks").

**Fallbacks:** if the target satellite is offline, use another satellite in the same room; failing that, the chat client and a phone push.

**Phone push:** the web client can be installed as a home-screen app and receive Web Push notifications, so results reach a phone even when the page isn't open. This needs the HTTPS set-up from §4.1; on iPhones, Web Push works only for web apps added to the home screen.

**Agent questions while away:** an agent question waits up to `question_timeout_min` for an answer (by voice in that room, from the notification prompt, or in chat). If nobody answers, the agent is told "no answer" and decides whether to continue with a default or stop.

**Quiet hours and do-not-disturb:** configured quiet hours, plus a voice command ("Do not disturb until 8 a.m."), switch `normal` delivery to silent. Timers still ring.

**Expiry:** unacknowledged notifications expire after `expire_after_h` and remain in the chat history.

```yaml
notifications:                    # in tools.yaml, alongside the other built-in services
  quiet_hours: { start: "22:00", end: "07:00" }
  default_style: ask_first        # ask_first | speak_summary | chime_only
  mention_pending_after_reply: true
  question_timeout_min: 30
  expire_after_h: 24
  web_push: true
```

The service is exposed to the fast model as one more built-in tool, `notifications` (list, read, dismiss, do-not-disturb), so the voice commands above go through the normal tool path.

## 11. Library choices (Apple Silicon)

**Pipecat built-ins used as-is:**

| Need | Pipecat built-in |
|---|---|
| Speech-to-text | `WhisperSTTServiceMLX` |
| Text-to-speech | `KokoroTTSService` |
| Voice activity detection | Silero VAD analyzer |
| Turn detection | Smart Turn v3 (`LocalSmartTurnAnalyzerV3`) |
| Fast model and tool calling | OpenAI LLM service (`base_url` → LM Studio), function calling, `LLMSetToolsFrame` |
| Router fallback (`fast` → `fast-small`) | `LLMSwitcher` |
| MCP tools | `MCPClient` |
| Browser voice | SmallWebRTC transport (server) and the JavaScript client SDK |
| Text chat and the future satellite protocol | Websocket transport with a frame serializer |

**Built by us, because Pipecat has no equivalent:** the ESPHome satellite adapter, the tool, model, and agent registries, the decision engine, the built-in services (search, weather, timers, music), the task manager, and the web interface. Model agents ("Ask Claude") run outside the live voice pipeline; try Pipecat's LLM services in a text-only pipeline for them first, and fall back to a plain SDK loop only if that proves awkward for background tasks.

**Libraries:**


- **STT:** Pipecat `WhisperSTTServiceMLX`; `whisper.cpp` with Metal as a fallback. Not `faster-whisper` on the Mac (no Metal backend).
- **TTS:** Pipecat `KokoroTTSService` (ONNX Runtime); `mlx-audio` Kokoro if first-audio is too slow.
- **Local model server:** LM Studio with the MLX engine. `mlx-lm` server or Ollama work as drop-in alternatives because the config only needs a base URL.
- **VAD and turn detection:** Silero via ONNX Runtime, with Pipecat's Smart Turn v3 (local ONNX, int8 CPU build) on top.
- **Satellites:** `aioesphomeapi`.
- **Fast-path tool calling:** Pipecat's OpenAI LLM service (with `base_url` set to LM Studio) and Pipecat's `MCPClient`.
- **Agent runner clients:** `openai` Python client for all `openai` servers; `anthropic` Python SDK for the native Anthropic server type.
- **Tools:** official MCP Python SDK (under Pipecat's `MCPClient` and in the agent runner); Pydantic for config validation; `watchfiles` for reload.
- **Runtime:** `launchd` jobs for every service; Homebrew for system packages (`ffmpeg`, Python); Apple `container` only for sandboxed agents.
- **Search:** SearXNG (native, Granian) via its JSON API; `httpx` and `trafilatura` for page reading; `playwright` (async Python API, Chromium) for JavaScript-heavy pages; an in-memory TTL cache.
- **Weather:** `httpx` against Open-Meteo.
- **Music:** external Music Assistant server; `music-assistant-client` Python package in the assistant service.
- **Timers:** asyncio scheduling with SQLite persistence.
- **Agents:** A2A Python SDK.
- **Decision engine:** `llm_choice` uses LM Studio's structured output with a JSON schema; spend log in SQLite.
- **Web interface:** FastAPI on Uvicorn; example client built with Vite using Pipecat's JavaScript client SDK and its SmallWebRTC transport.

## 12. Risks and decisions

- **Satellite ownership (highest risk for the satellite phase; doesn't affect the core pipeline):** the satellites were set up with Home Assistant, whose ESPHome integration is already connected to each one. My understanding is that voice-assistant requests go to a single subscribed client, so Home Assistant and this service would compete. Test with one Satellite1 first. Likely outcome: this service owns voice and Home Assistant's voice pipeline is disabled for that device. Fallback: customize the Satellite1's ESPHome YAML.
- **Model eviction:** an unpinned model in LM Studio can be unloaded and cost seconds to reload. Pin the router models and check memory when adding local agent models.
- **Follow-up listening and announcements on satellites:** clarify, confirm, agent questions, and background results all need the device to reopen its mic or play unprompted audio. Include both in the device test.
- **Agent name recognition:** Whisper may mishear agent names ("Claude" as "cloud"). List aliases, and pass agent names to Whisper as an initial prompt to bias recognition.
- **Cloud data and cost:** cloud agents receive request text and whatever `share_context` allows. Set a monthly spend limit on the Anthropic account.
- **Search speed and reliability:** self-hosted SearXNG is slower than a paid search API, and upstream engines sometimes rate-limit or block metasearch traffic, which shows up as empty or partial results. Keep several engines configured, watch SearXNG's engine errors, and cache aggressively.
- **Page reading:** some sites block headless browsers or put content behind consent banners. Treat a failed read as "snippets only" rather than an error.
- **Voice is unauthenticated:** anyone near a satellite, or audio from a TV, can issue commands, including cloud requests that cost money. Confirmation for risky tools, restricted agent profiles, and (later) speaker ID mitigate this.
- **Tool and agent latency:** web search and cloud models depend on external services. Per-tool and per-model timeouts with a spoken fallback ("I couldn't reach Claude").
- **Music and the microphone:** the Satellite1's echo cancellation can only remove audio it plays itself. Music from a separate speaker in the same room will make wake-word detection and transcription harder. Ducking helps; test with music playing at normal volume.
- **Sendspin is experimental:** ESPHome marks its Sendspin component as experimental, and it isn't yet known whether it coexists with the voice assistant on one device. Prefer existing speakers for music in version 1.
- **Spotify policy:** Spotify has tightened its developer API repeatedly. Going through Music Assistant keeps that dependency in one maintained project rather than in our code.
- **Timer "stop" word:** if the Satellite1 can't listen for "stop" without the wake word, stopping a ringing timer is clumsier. Check in the device test.
- **Tool sprawl:** the test command and the ~15-tool guideline are the guardrails.
- **Single speech process:** a crash or a blocked event loop affects every device at once. Safeguards in §4.2.
- **Shared GPU:** agents run on the same Mac mini, so a background agent generating with a local model competes with Whisper and the fast model for the GPU. A voice request during a long agent run will be slower. Measure this; if it matters, give the fast path priority by pausing background agent work during a voice turn, or move heavy agents to a cloud model.
- **Agents on the same machine:** Hermes has shell and file tools. Each agent runs under its own macOS user or in an Apple container, with access only to its own working folder, and binds its API to localhost (or the container network).
- **Alexa relay is unofficial:** it relies on Amazon's private app API via Alexa Media Player and can stop working without notice. Nothing in the core design depends on it.
- **Music depends on another machine:** if the host running Music Assistant is down, music stops working while everything else keeps going. The tool reports it ("I can't reach the music server") rather than failing silently.
- **Apple container maturity:** pre-1.0, no restart policies, and networking quirks after sleep. Use it only for agent sandboxes, pin the version, and supervise containers with `launchd`.
- **Decider mistakes:** picking too cheap a model gives a weak answer; too expensive wastes money. `escalate_on_failure`, the budget caps, and the decision log limit both. Review the log for the first few weeks.
- **Microphone access needs HTTPS:** browsers only allow the microphone on secure pages, so the web interface must be served over HTTPS on the LAN (§4.1).
- **Library churn:** MLX, Pipecat, MCP, and A2A move quickly. Pin versions.

## 13. Open questions

1. Which speakers should play music in each room: the satellites themselves, or existing speakers (Sonos, AirPlay, Cast)?
2. Which model should Hermes use by default: a local model in LM Studio (private, competes for the GPU) or a cloud model (faster for long jobs, costs money)?
3. Should the system identify who is speaking (per-person calendars, lists, permissions), or treat the household as one user?
4. HTTPS on the LAN: a local certificate authority (`mkcert`) or Tailscale certificates? Tailscale also gives safe access from outside the home.
5. Which local engine should the decider use: a small LM Studio model (`llm_choice`, works now) or a dedicated local decision model once one is evaluated?
6. Quiet hours: what times, and should agent questions ever wake a room at night?

## Appendix A. Satellite device options

The satellites are outside the backend. The backend only needs a device that can detect a wake word, stream 16 kHz audio up, play audio back, and accept announcements. This appendix records the options for meeting that, and is not a decision.

**The lock-in is the firmware, not the hardware.** The Satellite1 is a capable voice board: an ESP32-S3, an XMOS XU316 audio processor with echo cancellation, a four-microphone array, and an amplifier, all running open-source ESPHome firmware. What ties it to Home Assistant is ESPHome's voice-assistant protocol, not the device.

### A.1 A backend-neutral satellite protocol (proposal)

A small, documented websocket protocol between devices and the assistant would let any device work with the assistant, whatever firmware it runs:

| Direction | Messages |
|---|---|
| Device → assistant | `hello` (device ID, token, capabilities), `wake` (wake word, timestamp), audio frames (16 kHz 16-bit PCM, or Opus), `button`, `playback_done` |
| Assistant → device | `state` (idle, listening, thinking, speaking; drives LEDs), audio frames or a URL to play, `listen` (reopen the mic without a wake word), `announce`, `notify_pending` (indicator on or off), `timer_ring` / `timer_stop`, `volume`, `duck` |

Pipecat's websocket transport takes a pluggable message serializer, so this protocol would be one serializer in the satellite adapter, alongside the ESPHome native API adapter. It would also remove the device-ownership risk in §12, because the device would talk only to the assistant.

### A.2 Options

| Option | What changes | Pros | Cons |
|---|---|---|---|
| **1. Stock firmware** (current plan) | Nothing on the device. The assistant connects over the ESPHome native API, as Home Assistant does. | No reflashing; works today. | Tied to ESPHome's Home Assistant-shaped voice protocol; competes with Home Assistant for the device. |
| **2. Satellite1 with a custom ESPHome component** (recommended next step) | A small C++ external component replaces only the voice transport with the protocol in A.1. | Keeps Future Proof Homes' board support (XMOS, wake word, LEDs, amplifier); low power; no new hardware; no Home Assistant dependency. | Some C++ work; must track ESPHome releases. |
| **3. pipecat-esp32** | ESP-IDF firmware from the Pipecat team that connects an ESP32-S3 straight to a Pipecat backend over SmallWebRTC (MIT licence). | Native fit with the backend. | Early: tested only on ESP32-S3-BOX-3 and M5Stack CoreS3; no wake word; would need porting to the Satellite1's XMOS audio path. Worth watching. |
| **4. Other ESP32 voice hardware** | A different board running option 2 or 3. | Seeed ReSpeaker XVF3800 + XIAO ESP32-S3: stronger XMOS echo cancellation and beamforming, four mics, 5 W amplifier, Arduino/ESP-IDF programmable, community ESPHome support. Espressif ESP32-S3-BOX-3: screen, reference platform for pipecat-esp32. | Same class as the Satellite1; doesn't remove lock-in by itself. |
| **5. Small Linux satellite** | A Raspberry Pi (Zero 2 W or larger) with a USB mic array such as the XVF3800, running a Python client. | Fully programmable; can run a Pipecat client, local wake word (openWakeWord), or the A.1 protocol; good for a screen or a better speaker. | Roughly 1–3 W versus about 0.5–1 W for an ESP32 (rough figures); longer boot; SD card and OS upkeep. Best for one or two rooms, not everywhere. |

### A.3 Suggested path

1. Keep the stock firmware with the ESPHome native API adapter, so nothing stops working.
2. Define the A.1 protocol and add it to the satellite adapter.
3. Write the ESPHome component (option 2) for one Satellite1 and test it in one room.
4. If it works, move the remaining satellites over. Any device bought later only needs to speak the protocol.