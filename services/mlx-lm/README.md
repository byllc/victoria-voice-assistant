# Decider runtime (mlx-lm)

The decider (architecture §7.1, `decider.type: decision_model`) is a local decision model served by [mlx-lm](https://github.com/ml-explore/mlx-lm)'s OpenAI-compatible server on `127.0.0.1:8090`. mlx-lm is used instead of LM Studio because it can switch the model's thinking off per request and returns token probabilities, which give the decider its confidence.

- **Model:** Tev1-4B-experimental (Together AI), a decision fine-tune of Qwen3.5-4B. The weights are the MLX conversion LM Studio already downloaded; their folder is the `model` of the `decider` entry in `config/models.yaml`.
- **Python environment:** this folder. `pyproject.toml` and `uv.lock` pin mlx-lm; uv builds `.venv` here.
- **Port and host:** taken from the `decider` server's `base_url`. Localhost only.

```sh
uv run assistant decider install                  # build the environment
uv run assistant decider run                      # run it in the foreground
uv run assistant launchd install-decider --load   # or run it under launchd
```

Don't also load Tev1 in LM Studio: that would hold a second 8 GB copy in memory.
