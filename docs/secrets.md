# Secrets

Secrets live in the macOS login **Keychain**, never in the project or in `config/` (Bill, 2026-11-01). The configuration only *names* a secret, for example `api_key: ${LMS_API_KEY}` in `models.yaml`; the assistant reads the value from the Keychain into memory when it loads the configuration.

Each secret is a Keychain item with service `victoria-voice-assistant` and the secret's name as the account. Keychain Access shows them under that name.

## Commands

```sh
uv run assistant secrets list             # which are set (never their values)
uv run assistant secrets set HA_TOKEN     # prompts without echoing; or pipe the value in
uv run assistant secrets delete HA_TOKEN
```

Restart the assistant service after changing one (configuration is read at start and on YAML edits).

`assistant secrets get NAME` prints a value for a script, as in `services/hermes/run.sh`; it refuses to print to a terminal.

| Name | What it is |
|---|---|
| `LMS_API_KEY` | LM Studio's API key (LM Studio > Developer) |
| `HERMES_API_KEY` | The Hermes gateway's key ([hermes.md](hermes.md)) |
| `ANTHROPIC_API_KEY` | Claude, when turned on ([claude-setup.md](claude-setup.md)) |
| `HA_TOKEN` | Home Assistant long-lived access token |
| `VAPID_PRIVATE_KEY` | Web Push signing key, created by `assistant push keys` ([push.md](push.md)) |
| `MUSIC_ASSISTANT_TOKEN` | Music Assistant token |

## Moving from `config/.env`

Older setups kept secrets in `config/.env`, which is no longer read. Move them in one step; each value is checked as it's stored, then the file is deleted:

```sh
uv run assistant secrets import-env config/.env
```

## Notes

- **An environment variable with the same name wins** over the Keychain. Tests use this (with `ASSISTANT_KEYCHAIN=0` they never touch the Keychain), and so can a one-off run.
- **The household web password** (`ASSISTANT_WEB_PASSWORD`) still comes only from the web process's environment; with none set, the web interface is open (Bill: no password needed on the home network).
- **Under launchd** the services read the login Keychain, which is unlocked while you're logged in. On a Mac mini that reboots unattended, turn on automatic login for the account that runs the assistant, or the services start without their secrets (they log `keychain_unavailable`).
- The first time a new program reads an item, macOS may ask to allow it. Choose **Always Allow**.
