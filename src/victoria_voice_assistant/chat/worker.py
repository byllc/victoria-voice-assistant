"""Running one pipeline to its end, the way Pipecat 1.15 asks for it (no deprecated calls)."""

from __future__ import annotations

from pipecat.pipeline.worker import PipelineWorker
from pipecat.workers.runner import WorkerRunner


async def run_worker(worker: PipelineWorker) -> None:
    """Run `worker` until its pipeline ends. Each session has its own runner; signals are the service's."""
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()
