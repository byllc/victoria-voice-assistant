"""`model` agents (architecture §9): a model with its own system prompt and tools, run outside the live pipeline.

Following the built-ins-first rule, the tool loop runs in a separate text-only Pipecat
pipeline (context aggregators around the LLM service, no transport), using the same tool
handlers as the fast model.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    MetricsFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams, LLMService

from victoria_voice_assistant.tools.registry import ToolContext, ToolEntry

# Model calls that end in tool calls, per task, before the agent is stopped.
MAX_TOOL_ROUNDS = 6


class AgentError(Exception):
    pass


@dataclass
class AgentReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class _Collector(FrameProcessor):
    """After the LLM: keeps the latest reply's text and resolves `done` when a reply makes no tool calls."""

    def __init__(self, done: asyncio.Future, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.done = done
        self.text: list[str] = []
        self.called = False
        self.rounds = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self.text, self.called = [], False
            elif isinstance(frame, LLMTextFrame):
                self.text.append(frame.text)
            elif isinstance(frame, FunctionCallsStartedFrame):
                self.called = True
                self.rounds += 1
                if self.rounds > MAX_TOOL_ROUNDS and not self.done.done():
                    self.done.set_exception(AgentError(f"more than {MAX_TOOL_ROUNDS} tool rounds"))
            elif isinstance(frame, LLMFullResponseEndFrame) and not self.called and not self.done.done():
                self.done.set_result("".join(self.text).strip())
            elif isinstance(frame, MetricsFrame):
                for data in frame.data:
                    if isinstance(data, LLMUsageMetricsData):
                        self.input_tokens += data.value.prompt_tokens
                        self.output_tokens += data.value.completion_tokens
        await self.push_frame(frame, direction)


def _tool_handler(entry: ToolEntry, context: ToolContext):
    async def handler(params: FunctionCallParams) -> None:
        outcome = await entry.handler(dict(params.arguments), context)
        result: Any = outcome.result if outcome.result is not None else {"ok": True}
        if outcome.speak and outcome.result is None:
            result = {"message": outcome.speak}
        await params.result_callback(result)

    return handler


async def run_model_agent(
    llm: LLMService,
    system: str | None,
    request: str,
    tools: list[ToolEntry],
    context: ToolContext,
) -> AgentReply:
    """Run one task to its final reply. Cancelling the caller stops the pipeline."""
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": request})
    schema = ToolsSchema(standard_tools=[entry.schema(_tool_handler(entry, context)) for entry in tools])
    llm_context = LLMContext(messages=messages, tools=schema) if tools else LLMContext(messages=messages)
    pair = LLMContextAggregatorPair(llm_context)
    done: asyncio.Future = asyncio.get_running_loop().create_future()
    collector = _Collector(done)
    pipeline = Pipeline([pair.user(), llm, collector, pair.assistant()])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=False, enable_metrics=True, enable_usage_metrics=True),
        enable_rtvi=False,
        idle_timeout_secs=None,  # the runner's own time limit applies, not Pipecat's voice-idle check
    )

    @task.event_handler("on_pipeline_error")
    async def on_error(_task, frame: ErrorFrame) -> None:
        if not done.done():
            done.set_exception(AgentError(frame.error))

    await task.queue_frame(LLMRunFrame())
    running = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    try:
        text = await done
    finally:
        await task.cancel(reason="agent finished")
        await asyncio.gather(running, return_exceptions=True)
    if not text:
        raise AgentError("the model returned no text")
    return AgentReply(text=text, input_tokens=collector.input_tokens, output_tokens=collector.output_tokens)
