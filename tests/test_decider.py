from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import DeciderConfig, ModelsConfig
from victoria_voice_assistant.decider import mlxlm
from victoria_voice_assistant.decider.engine import DecisionModel, build_decider
from victoria_voice_assistant.launchd import render_decider_launch_agent

OPTIONS = {
    "claude-haiku": "quick facts, short answers",
    "claude-sonnet": "explanations, planning, recipes",
    "claude-opus": "hard reasoning, long research",
}


def run(coro):
    return asyncio.run(coro)


def _reply(content: str, logprobs: dict[str, float] | None = None) -> dict:
    choice: dict = {"message": {"role": "assistant", "content": content}}
    if logprobs is not None:
        top = [{"token": token, "logprob": math.log(p)} for token, p in logprobs.items()]
        choice["logprobs"] = {"content": [{"token": content, "logprob": top[0]["logprob"], "top_logprobs": top}]}
    return {"choices": [choice], "usage": {"total_tokens": 120}}


def _decide(handler, options=OPTIONS):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = DecisionModel("http://127.0.0.1:8090/v1", "/models/tev1", client=client)
            return await model.decide("Spoken request: 'soup recipe'", "Which is the cheapest model?", options)

    return run(scenario())


def test_decision_sends_lettered_options_with_thinking_off_and_reads_confidence() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        # "\n" is not an option letter, so it is left out of the renormalisation.
        return httpx.Response(200, json=_reply("B", {"B": 0.6, "A": 0.3, "C": 0.05, "\n": 0.05}))

    decision = _decide(handler)
    body = seen[0]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["logprobs"] is True and body["temperature"] == 0 and body["model"] == "/models/tev1"
    payload = json.loads(body["messages"][1]["content"])
    assert payload["options"] == {
        "A": "claude-haiku: quick facts, short answers",
        "B": "claude-sonnet: explanations, planning, recipes",
        "C": "claude-opus: hard reasoning, long research",
    }
    assert decision.choice == "claude-sonnet" and decision.error is None
    assert decision.confidence == pytest.approx(0.6 / 0.95)
    assert decision.probabilities["claude-haiku"] == pytest.approx(0.3 / 0.95)
    assert decision.tokens == 120


@pytest.mark.parametrize(
    ("handler", "error"),
    [
        (lambda request: httpx.Response(200, json=_reply("", {"A": 0.9})), "not one of the option letters"),
        (lambda request: httpx.Response(200, json=_reply("D", {"D": 0.9})), "not one of the option letters"),
        (lambda request: httpx.Response(200, json=_reply("The answer is B")), "not one of the option letters"),
        (lambda request: httpx.Response(503), "503"),
        (lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")), "refused"),
        (lambda request: httpx.Response(200, json={"unexpected": True}), "choices"),
    ],
)
def test_decider_trouble_gives_no_choice_instead_of_raising(handler, error: str) -> None:
    decision = _decide(handler)
    assert decision.choice is None and decision.confidence == 0.0 and error in (decision.error or "")


def test_answer_without_logprobs_has_zero_confidence() -> None:
    decision = _decide(lambda request: httpx.Response(200, json=_reply("A")))
    assert decision.choice == "claude-haiku" and decision.confidence == 0.0  # the pool default will win


def test_option_count_is_checked() -> None:
    with pytest.raises(ValueError):
        _decide(lambda request: httpx.Response(500), options={"only": "one"})
    with pytest.raises(ValueError):
        _decide(lambda request: httpx.Response(500), options={f"o{n}": "x" for n in range(25)})


def test_decision_model_config_needs_server_and_model() -> None:
    with pytest.raises(ValidationError):
        DeciderConfig(type="decision_model", server="decider")
    DeciderConfig(type="decision_model", server="decider", model="decider")


@pytest.fixture
def example_models(example_config_dir: Path) -> ModelsConfig:
    return load_config(example_config_dir).models


def _models(models: ModelsConfig, tmp_path: Path, base_url: str = "http://127.0.0.1:8090/v1", weights: Path | None = None) -> ModelsConfig:
    data = models.model_dump()
    data["servers"]["decider"]["base_url"] = base_url
    data["models"]["decider"]["model"] = str(weights or tmp_path)
    return ModelsConfig.model_validate(data)


def test_example_config_builds_the_decider(example_models: ModelsConfig, tmp_path: Path) -> None:
    decider = build_decider(_models(example_models, tmp_path))
    assert decider is not None
    assert decider.base_url == "http://127.0.0.1:8090/v1" and decider.model == str(tmp_path)


def test_run_command_serves_the_weights_on_localhost(
    example_models: ModelsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.json").write_text("{}")
    server = tmp_path / "runtime" / ".venv" / "bin" / "mlx_lm.server"
    server.parent.mkdir(parents=True)
    server.write_text("")
    monkeypatch.setattr(mlxlm, "RUNTIME_DIR", tmp_path / "runtime")
    command = mlxlm.run_command(_models(example_models, tmp_path))
    assert command == [str(server), "--model", str(tmp_path), "--host", "127.0.0.1", "--port", "8090"]


def test_run_command_refuses_remote_servers_and_missing_weights(example_models: ModelsConfig, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        mlxlm.run_command(_models(example_models, tmp_path, base_url="http://192.168.1.5:8090/v1"))
    with pytest.raises(FileNotFoundError, match="no MLX model"):
        mlxlm.run_command(_models(example_models, tmp_path, weights=tmp_path / "missing"))


def test_decider_launch_agent_renders(tmp_path: Path) -> None:
    document = render_decider_launch_agent(Path("/usr/bin/python3"), tmp_path, tmp_path / "config", tmp_path / "logs")
    assert document["Label"] == "com.victoria.decider"
    assert document["ProgramArguments"][-2:] == ["decider", "run"]  # type: ignore[index]
    assert document["KeepAlive"] is True
