# Pipecat PipelineWorker migration

- Status: in progress
- Phase: current
- Origin: Stage 14
- Created: 2026-11-06 (UTC)
- Started: 2026-11-12, branch `feature-upgrade-feature-tool` (Bill: "ask all the qeustions you need right now and then get to work")

## Done when

- [x] Pipecat 1.15 deprecates `PipelineTask` and `PipelineRunner` (removed in 2.0). Move the chat, voice, agent, and satellite pipelines to `PipelineWorker` and `WorkerRunner`.
- [ ] Live voice check after the move.

## Blocking decisions

None.

## Evidence and notes

Built 2026-11-12, after the Pipecat internals were gathered (`20261111-145253`).

- In Pipecat 1.15 `PipelineTask` is an empty subclass of `PipelineWorker`, and `PipelineRunner` of `WorkerRunner`, so the move is a rename with no change in behaviour. The chat (`chat/session.py`), browser voice (`voice/session.py`), agent (`agents/model_agent.py`) and satellite (`satellite/esphome.py`) pipelines now build a `PipelineWorker` (`pipecat.pipeline.worker`).
- `WorkerRunner.run(worker)` is deprecated too. `chat/worker.py::run_worker` does what Pipecat asks instead (`add_workers`, then `run`), and all four pipelines use it.
- `uv run pytest -q`: 567 passed, 3 skipped, and no Pipecat deprecation warning left in the run (before: one, for `run(worker)`).
- Live, typed chat on the restarted service, 13:57 UTC: a timer set and cancelled, the time; 14:04 UTC: the weather. Both satellites reconnected after each restart.

Still to do: the live voice check (Bill), in the browser and on a satellite.
