from __future__ import annotations

import dataclasses
from typing import Any

from openai import APIConnectionError
from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.service_switcher import ServiceSwitcherStrategyFailover
from pipecat.processors.frame_processor import ErrorCategory
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.llm_service import LLMService
from pipecat.services.openai.llm import OpenAILLMService

from victoria_voice_assistant.config.schema import ModelsConfig


class ChatOpenAILLMService(OpenAILLMService):
    """OpenAI-compatible LLM that marks itself unusable when its server is unreachable.

    The LLM switcher only fails over from services that report ``is_usable=False``.
    A refused or dropped connection is therefore treated as permanent so the
    fallback takes over.
    """

    async def push_error(
        self,
        error_msg: str,
        exception: Exception | None = None,
        fatal: bool = False,
        category: ErrorCategory | None = None,
        force_treat_as_permanent: bool = False,
    ) -> None:
        if isinstance(exception, APIConnectionError):
            force_treat_as_permanent = True
        await super().push_error(
            error_msg=error_msg,
            exception=exception,
            fatal=fatal,
            category=category,
            force_treat_as_permanent=force_treat_as_permanent,
        )


def _settings_values(model: str, params: dict[str, Any], settings_class: Any) -> dict[str, Any]:
    """Params Pipecat has a setting for go to its settings; the rest (e.g. reasoning_effort) go to the request body."""
    known = {field.name for field in dataclasses.fields(settings_class)}
    values: dict[str, Any] = {"model": model}
    extra = {key: value for key, value in params.items() if key not in known}
    values.update({key: value for key, value in params.items() if key in known})
    if extra:
        values["extra"] = extra
    return values


def build_llm(role: str, models: ModelsConfig) -> LLMService:
    model_config = models.models[role]
    server = models.servers[model_config.server]
    if server.type == "anthropic":
        if not server.api_key:
            raise ValueError(f"model {role!r}: the anthropic server needs an api_key (ANTHROPIC_API_KEY)")
        # Pipecat sends each Claude model's lowest documented thinking setting unless params say otherwise.
        return AnthropicLLMService(
            name=role,
            api_key=server.api_key,
            settings=AnthropicLLMService.Settings(
                **_settings_values(model_config.model, model_config.params, AnthropicLLMService.Settings)
            ),
            retry_timeout_secs=model_config.timeout_s,
        )
    settings_values = _settings_values(model_config.model, model_config.params, OpenAILLMService.Settings)
    return ChatOpenAILLMService(
        name=role,
        settings=OpenAILLMService.Settings(**settings_values),
        # LM Studio accepts any bearer value when auth is off; the SDK needs a non-empty key.
        api_key=server.api_key or "not-required",
        base_url=server.base_url,
        retry_timeout_secs=model_config.timeout_s,
    )


def build_router(models: ModelsConfig) -> LLMSwitcher:
    services = [
        build_llm(models.roles.router, models),
        build_llm(models.roles.router_fallback, models),
    ]
    return LLMSwitcher(llms=services, strategy_type=ServiceSwitcherStrategyFailover)
