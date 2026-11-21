# Pipecat internals in one place

- Status: in progress
- Phase: current
- Origin: Architecture review, 2026-11-11
- Created: 2026-11-11 (UTC)
- Started: 2026-11-12, branch `feature-upgrade-feature-tool` (Bill: "ask all the qeustions you need right now and then get to work")

The assistant reaches into Pipecat 1.15's internals in several places, so an upgrade can break voice without any error at import.

## Done when

- [x] Every use of a Pipecat private name or replaced module attribute is listed in this file: the shared Kokoro and ONNX sessions and `_ensure_model_files` (`voice/models.py`), the copied `run_stt` with its `initial_prompt` (`voice/models.py`), and the overridden `_process_speech_segment`, `_handle_vad_user_*` and `_vad_user_speaking` (`voice/session.py`).
- [x] They live in one module, each with a note on why Pipecat's public interface wasn't enough.
- [x] A test fails with a clear message when a Pipecat upgrade removes or renames one of them.
- [ ] No behaviour change: the full test run passes, and a live voice check.

## Blocking decisions

None open. Done before the PipelineWorker migration (Bill, 2026-11-12: "Before it").

## Evidence and notes

Built 2026-11-12, before the PipelineWorker migration as decided.

Every use of a Pipecat private name or replaced module attribute, all now in `src/victoria_voice_assistant/voice/pipecat_internals.py`:

| What | Pipecat name | Why the public interface wasn't enough |
|---|---|---|
| One Kokoro for all sessions | `pipecat.services.kokoro.tts.Kokoro` (replaced with a cached factory), `_ensure_model_files`, `KOKORO_CACHE_DIR` | `KokoroTTSService` builds its own ONNX session per instance (about 300 MB) and takes no ready-made one |
| One ONNX session per model file | `onnxruntime` in `pipecat.audio.vad.silero`, `ort` in `pipecat.audio.turn.smart_turn.local_smart_turn_v3` (both replaced with a caching wrapper) | Each VAD and Smart Turn analyzer loads its own session; neither takes a shared one |
| Whisper's initial prompt | A copy of `WhisperSTTServiceMLX.run_stt`; it uses `_settings`, `_handle_transcription`, `_user_id`, `start_processing_metrics`, `stop_processing_metrics` | The service doesn't expose `initial_prompt` |
| Short clips end their turn without Smart Turn | `LocalSmartTurnAnalyzerV3._process_speech_segment` (overridden); `TurnAnalyzerUserTurnStopStrategy._handle_vad_user_started_speaking`, `_handle_input_audio`, `_handle_vad_user_stopped_speaking` (overridden) and `_vad_user_speaking` (read) | The stop strategy has no public hook for "this stretch of speech was short" |

- `voice/models.py` and `voice/session.py` now import these from the one module (`PromptedWhisperSTT`, `ShortClipSmartTurn`, `ShortClipStopStrategy`, `shared_kokoro`, `ensure_kokoro_files`, `shared_onnx`). The code was moved, not rewritten.
- `tests/test_pipecat_internals.py` (4 tests): each listed name still exists in the installed Pipecat, with a message naming what breaks; built objects still carry the instance attributes we read; the shared models are still wired in; and no other module imports a private Pipecat name or overrides a private Pipecat method.
- `uv run pytest -q`: 567 passed, 3 skipped. Typed chat works after a restart, and both satellites reconnected (14:04 UTC).

Still to do: the live voice check (Bill). Typed chat doesn't go through any of the moved code.
