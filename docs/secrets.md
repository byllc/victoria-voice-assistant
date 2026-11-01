# Secrets

API keys and tokens are **kept in the macOS Keychain** and **run from `config/.env`** (Bill, 2026-11-01). The configuration only names a secret, for example `api_key: ${LMS_API_KEY}` in `models.yaml`.

- **Storing one** (`assistant secrets set NAME`) writes it to both: the login Keychain (service `victoria-voice-assistant`, the secret's name as the account) and `config/.env`.
- **The services read only the process environment and `config/.env`**, never the Keychain, so they start after a reboot before anyone logs in (the login Keychain is locked until then). Values from `config/.env` are read into memory; they aren't exported to child processes.
- **Installing a service** (`assistant launchd install`, `install-web`, and `services/hermes/install-launchd.sh`) first rewrites `config/.env` from the Keychain. `assistant secrets sync` does the same at any time, for example after restoring the Keychain on a new Mac.
- **Without a Keychain** (not a Mac, or `ASSISTANT_KEYCHAIN=0`), `config/.env` is the only store.

## `config/.env` is never committed

- It's git-ignored, along with any `.env` or `.env.*` anywhere in the repository (`.env.example` files excepted).
- The commands only write it after `git check-ignore` confirms git ignores it, and keep it at mode 600.
- A pre-commit hook (`.githooks/pre-commit`) rejects any staged `.env` file, even one forced past `.gitignore`. Turn it on once per clone:

  ```sh
  git config core.hooksPath .githooks
  ```

## Commands

```sh
uv run assistant secrets list             # which are set, and where (never their values)
uv run assistant secrets set HA_TOKEN     # prompts without echoing; or pipe the value in
uv run assistant secrets delete HA_TOKEN  # from both
uv run assistant secrets sync             # rewrite config/.env from the Keychain
```

Restart the assistant (and the web process, for the web password) after changing one. `assistant secrets get NAME` prints a value for a script, as in `services/hermes/run.sh`; it refuses to print to a terminal.

| Name | What it is | Where to get it | Needed |
|---|---|---|---|
| `LMS_API_KEY` | LM Studio's API key | LM Studio > Developer > API key (turn on "Require API key") | Always |
| `ASSISTANT_WEB_PASSWORD` | The household login for the web interface | Choose one. Read by the web process at start | Only if you want a login; without it the web interface is open with a red banner |
| `HERMES_API_KEY` | The Hermes gateway's key; the same value goes in Hermes's `API_SERVER_KEY` | Make one: `openssl rand -hex 32 \| uv run assistant secrets set HERMES_API_KEY` ([hermes.md](hermes.md)) | With a `harness` agent (Hermes) |
| `ANTHROPIC_API_KEY` | Claude | Anthropic Console > API keys ([claude-setup.md](claude-setup.md)) | Only to turn Claude on |
| `HA_TOKEN` | Home Assistant long-lived access token | Home Assistant > your profile > Security > Long-lived access tokens > Create token | For home control (Stage 9) |
| `VAPID_PRIVATE_KEY` | Web Push signing key | Made for you: `uv run assistant push keys` ([push.md](push.md)) | Only for phone alerts |
| `MUSIC_ASSISTANT_TOKEN` | Music Assistant token | Music Assistant settings (Stage 10) | For music, once built |

Not secrets you set:

- `SEARXNG_SECRET`: SearXNG's internal key. `assistant searxng run` makes a new random one at each start unless the environment sets one; nothing depends on it staying the same.
- The HTTPS certificate and key are files in `config/tls/` (git-ignored), not Keychain items ([https.md](https.md)).

## Notes

- **An environment variable with the same name wins** over `config/.env`. Tests use this; they never touch the Keychain (`ASSISTANT_KEYCHAIN=0`).
- **The household web password** (`ASSISTANT_WEB_PASSWORD`) is optional; with none set, the web interface is open (Bill: no password needed on the home network).
- The first time a new program reads a Keychain item, macOS may ask to allow it. Choose **Always Allow**.
