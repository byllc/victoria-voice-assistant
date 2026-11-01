# Connecting Hermes

The assistant talks to [Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research) as a `harness` agent: an agent behind an OpenAI-compatible endpoint that runs its own tools (architecture §9). The assistant doesn't install, start or configure Hermes. It only needs the address of a Hermes gateway, its key, and its model name, so any Hermes installation works: on this Mac, in a container, under another macOS user, or on another machine.

`services/hermes/` holds one way to run Hermes for testing (an Apple container). Nothing in the assistant depends on it.

## What the assistant sends

- `POST <base_url>/chat/completions` with `stream: true`, `model: <model>`, and `Authorization: Bearer <api_key>`.
- One user message: the words after "ask Hermes". No conversation history (`share_context: minimal`) and no tools: Hermes uses its own.
- Cancelling the task ("cancel the Hermes task") closes the request stream. That stops the reply; Hermes may finish work it already started (architecture §10.2).
- A reply is spoken as plain text, shortened by the `summarizer` role when it's long; the full reply goes to chat.

## On the Hermes side

In that installation's `~/.hermes/.env` (or the environment of `hermes gateway`):

```sh
API_SERVER_ENABLED=true
API_SERVER_KEY=<a long random string>      # required, even on localhost
API_SERVER_HOST=127.0.0.1                  # or the address the assistant can reach; see Security
API_SERVER_PORT=8642
```

Then run `hermes gateway` (under `launchd`, systemd or similar so it restarts). Check it: `curl http://<host>:8642/health` and `curl -H "Authorization: Bearer <key>" http://<host>:8642/v1/models` (the model it lists is the name to use below).

Hermes needs a model with at least 64K tokens of context. With LM Studio, load a separate instance for it (for example `lms load google/gemma-4-12b --context-length 65536 --identifier gemma-hermes`) so a long Hermes run doesn't queue behind the assistant's fast model.

## On the assistant side

1. Store the same key: `uv run assistant secrets set HERMES_API_KEY` ([secrets.md](secrets.md)).
2. `config/models.yaml`:

   ```yaml
   servers:
     hermes:
       type: openai
       base_url: http://<host>:8642/v1     # the /v1 matters
       api_key: ${HERMES_API_KEY:-}
   models:
     hermes:
       server: hermes
       model: hermes-agent                 # whatever /v1/models lists
       timeout_s: 300                      # a stall timeout: Hermes can go quiet while its tools run
   ```

3. `config/agents.yaml`:

   ```yaml
   agents:
     - name: hermes
       aliases: [herms]
       kind: harness
       model: hermes
       mode: auto                          # wait up to wait_s, then report back when done
       wait_s: 8
   ```

   To make Hermes the agent offered when no tool fits, set `default: hermes`.

The config reloads on save. "Ask Hermes ..." then goes to that gateway. Without a key the assistant says "Hermes isn't set up yet." and puts the setup step in chat; if the gateway is down it says "I can't reach Hermes right now."

## Security

Voice is an unauthenticated channel: anyone in the house can say "ask Hermes ...". Hermes has file and shell tools, so the protection comes from where Hermes runs, not from the assistant:

- Run Hermes isolated: in a container or VM with only its own work folder mounted, or under its own macOS user. It must not be able to read the assistant's `config/` (keys), the assistant's data folder, or personal files. This is a Stage 7 done criterion, checked against whichever installation is connected.
- Keep the gateway off the LAN: bind it to `127.0.0.1`, or publish a container's port to `127.0.0.1` only. If Hermes runs on another machine, reach it over a private link (for example SSH port forwarding or a VPN) rather than an open port; the bearer key is the only check.
- Give Hermes only the tools it needs (`hermes tools`), and turn off web tools if it shouldn't browse.
