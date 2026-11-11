# Pipecat internals in one place

- Status: open
- Phase: current
- Origin: Architecture review, 2026-11-11
- Created: 2026-11-11 (UTC)

The assistant reaches into Pipecat 1.15's internals in several places, so an upgrade can break voice without any error at import.

## Done when

- [ ] Every use of a Pipecat private name or replaced module attribute is listed in this file: the shared Kokoro and ONNX sessions and `_ensure_model_files` (`voice/models.py`), the copied `run_stt` with its `initial_prompt` (`voice/models.py`), and the overridden `_process_speech_segment`, `_handle_vad_user_*` and `_vad_user_speaking` (`voice/session.py`).
- [ ] They live in one module, each with a note on why Pipecat's public interface wasn't enough.
- [ ] A test fails with a clear message when a Pipecat upgrade removes or renames one of them.
- [ ] No behaviour change: the full test run passes, and a live voice check.

## Blocking decisions

- Whether this is done with the PipelineWorker migration (`20261106-011053`) or before it.

## Evidence and notes

Not started.
