# SearXNG runtime

The assistant's web search uses a local [SearXNG](https://github.com/searxng/searxng) instance, run natively (no Docker) on `127.0.0.1:8888`.

- **Source:** a checkout of SearXNG at the commit pinned in `victoria_voice_assistant/search/searxng.py` (`SEARXNG_COMMIT`). It lives in `~/Services/searxng`; `ASSISTANT_SEARXNG_DIR` overrides that.
- **Python environment:** this folder. `pyproject.toml` and `uv.lock` pin SearXNG's dependencies, copied from its `requirements.txt` and `requirements-server.txt` at that commit. uv builds `.venv` here.
- **Settings:** `config/searxng/settings.yml` (example in `config/examples/searxng/`).

```sh
uv run assistant searxng install              # check out the pinned commit and build the environment
uv run assistant searxng run                  # run it in the foreground
uv run assistant launchd install-searxng --load   # or run it under launchd
```

To upgrade SearXNG, change `SEARXNG_COMMIT`, copy the new requirement versions into `pyproject.toml`, and run `uv lock` here.
