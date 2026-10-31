# Setting up Claude

The assistant runs on local models. Claude is optional: an agent you can ask by name ("ask Claude ...") for things the local models handle poorly. Until it has an API key, the `claude` agent is a stub: asking it says "Claude isn't set up yet." and puts a pointer to this page in chat. Nothing else depends on it, and no request leaves the house until it's set up.

## What's already in place

The example config (`config/examples/`) has everything except the key:

- `models.yaml`: an `anthropic` server (`api_key: ${ANTHROPIC_API_KEY:-}`, optional) and three models with prices in USD per million tokens (input/output):

  | Name | Model ID | Price |
  |---|---|---|
  | `claude-haiku` | `claude-haiku-5-5` | $0.10 / $0.50 |
  | `claude-sonnet` | `claude-sonnet-5-5` | $2.00 / $10.00 |
  | `claude-opus` | `claude-opus-5-5` | $4.00 / $20.00 |

  Check current IDs and prices at Anthropic before relying on them; the prices only feed the spend log and budget.
- `models.yaml`: the `claude` pool. The local decider (Tev1 served by mlx-lm, see `services/mlx-lm/README.md`) picks the cheapest member likely to handle each request; below `min_confidence` (0.6) it uses the default, Sonnet. "Ask Claude Opus/Sonnet/Haiku ..." picks a member directly. A reply that declines or a failed call is retried once, one member up. Budget: $30 a month; past 80% nothing above Sonnet, at 100% no paid call (the assistant offers the default agent instead).
- `agents.yaml`: the `claude` agent (aliases "clod", "cloud"), using the pool, with the weather and web search tools, mode `auto` (it says "still working" after 8 s and reports back when done).

## Turning it on

1. Get an API key from the Anthropic Console (console.anthropic.com), ideally in a workspace with its own spend limit.
2. Add it to `config/.env` (git-ignored):

   ```sh
   ANTHROPIC_API_KEY=sk-ant-...
   ```

3. If your `config/models.yaml` and `config/agents.yaml` predate this, copy the `anthropic` server, the three `claude-*` models, the `claude` pool and the `claude` agent from `config/examples/`. `uv run assistant config check` validates them.
4. Start the decider, so requests go to the cheapest suitable model rather than always the default: `uv run assistant decider run` (or `uv run assistant launchd install-decider --load`). Without it, every request uses Sonnet.
5. Restart the assistant service so it reads the new key (`config/.env` is read at start).

Then try, by text or voice: "Ask Claude how many ounces are in a cup" (expect Haiku), "Ask Claude for a leek soup recipe" (Sonnet), "Ask Claude Opus to review this paragraph" (Opus by override).

## Checking spend

Every Claude call is logged with its model, tokens, cost and how the model was chosen: the `spend` event in the service log and the `spend` table in the assistant's SQLite database (`~/Library/Application Support/Victoria Voice Assistant/assistant.sqlite3`). For this month's total:

```sh
sqlite3 ~/Library/Application\ Support/Victoria\ Voice\ Assistant/assistant.sqlite3 \
  "SELECT pool, ROUND(SUM(cost_usd), 4) FROM spend WHERE month = strftime('%Y-%m', 'now', 'localtime') GROUP BY pool"
```

Change the limits under `pools.claude.budget` in `models.yaml`; edits reload without a restart.

## Turning it off

Remove `ANTHROPIC_API_KEY` from `config/.env` and restart: the agent goes back to being a stub. To remove it entirely, delete the `claude` agent from `agents.yaml`.
