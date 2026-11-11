# Command history

Every request to the assistant is saved: typed in the web interface, or spoken to the browser or a satellite. Each one keeps the time, the device and its room, what was said, the tools it called and the reply. Architecture §8 has the design.

## Where it is

All of it is in the `history` table of the assistant's database (`~/Library/Application Support/Victoria Voice Assistant/assistant.sqlite3`). Nothing is archived or deleted, so we can see how fast it grows. The nightly backup copies the database, history included.

To see how much is saved:

```bash
uv run assistant history stats
```

## Asking the assistant

The `history` tool looks back 7 days:

- "What did I ask you this morning?"
- "When did I last turn on the bedroom light?"
- "What did I ask the kitchen yesterday?"

How far back it looks is set in `config/tools.yaml`:

```yaml
history:
  search_days: 7
```

It never goes to an agent: `assistant config check` refuses `history` in an agent's `tools`.

## Searching everything

```bash
uv run assistant history search bedroom light
```

```bash
uv run assistant history search --since 2026-10-01 --room kitchen
```

With no words it lists everything, oldest first.
