# Hermes test setup (Apple container)

One way to run [Hermes Agent](https://github.com/NousResearch/hermes-agent) for trying the assistant's `harness` agent. It is a test setup: the assistant doesn't depend on it, and any Hermes installation can be connected instead ([docs/hermes.md](../../docs/hermes.md)).

- **Image:** `Containerfile`. Debian with Python 3.12, a non-root `hermes` user, Hermes v0.22.5 installed by its official installer at a pinned commit (without the browser or computer-use parts), and these settings: LM Studio's `gemma-hermes` model through the Mac at `192.168.64.1:1234`, `model.context_length: 65536`, the shell working in `/work`, and only the `terminal` and `file` toolsets on the API server. Hermes's telemetry is off by default and left off.
- **Isolation:** the only Mac folder shared with the container is the work folder (`~/Services/hermes/work`, mounted at `/work`). Hermes runs as an unprivileged user in its own Linux VM. From the container, the Mac's services bound to `127.0.0.1` (the assistant, web interface, SearXNG, decider) are unreachable; LM Studio is reachable because it serves on all interfaces (key-protected).
- **Network:** the gateway listens on `0.0.0.0` inside the container and is published on the Mac's `127.0.0.1:8642` only. Hermes warns about `0.0.0.0` with a `local` terminal at startup; here the container is the sandbox.
- **Keys:** `run.sh` reads `HERMES_API_KEY` (the gateway key; generate one with `openssl rand -hex 32`) and `LMS_API_KEY` from `config/.env` and passes them in as `API_SERVER_KEY` and `LM_API_KEY`. Note that Hermes therefore holds the LM Studio key.

## Setup

```sh
container system start                       # once; registers Apple's container service with launchd
lms load google/gemma-4-12b --context-length 65536 --identifier gemma-hermes
container build --tag victoria-hermes:0.22.5 --file services/hermes/Containerfile services/hermes
services/hermes/run.sh                       # foreground; Ctrl-C stops it
```

Check: `curl http://127.0.0.1:8642/health`. Then connect the assistant as in [docs/hermes.md](../../docs/hermes.md) (`base_url: http://127.0.0.1:8642/v1`, model `hermes-agent`).

To upgrade Hermes, change `HERMES_COMMIT` in the `Containerfile` to a newer release's commit and rebuild with a new tag (and update `run.sh`).

Apple `container` is pre-1.0 (0.10.0 here) with no restart policy. To keep Hermes running, `services/hermes/install-launchd.sh --load` writes and loads a LaunchAgent (`com.victoria.hermes`) that runs `run.sh` and restarts it if it exits; logs go to `~/Library/Logs/Victoria Voice Assistant/hermes.log`. Stop it with `launchctl bootout gui/$(id -u)/com.victoria.hermes`. Stop any `run.sh` running in a terminal first, since both use the container name `hermes`.
