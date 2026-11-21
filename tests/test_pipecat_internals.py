"""Pipecat's private names the assistant depends on (voice/pipecat_internals.py).

When this fails after a Pipecat upgrade, voice is broken even though everything imports: fix
`voice/pipecat_internals.py` for the new Pipecat before going further.
"""

from __future__ import annotations

import ast
from pathlib import Path

from victoria_voice_assistant.voice import pipecat_internals
from victoria_voice_assistant.voice.pipecat_internals import (
    INSTANCE_INTERNALS,
    PromptedWhisperSTT,
    ShortClipSmartTurn,
    ShortClipStopStrategy,
    missing_internals,
)

SOURCE = Path(pipecat_internals.__file__).parents[1]


def test_pipecat_still_has_the_internals_we_use() -> None:
    missing = missing_internals()
    assert not missing, "This Pipecat no longer has:\n  " + "\n  ".join(missing)


def test_pipecat_instances_still_carry_the_attributes_we_read() -> None:
    from pipecat.services.whisper.stt import WhisperSTTServiceMLX
    from pipecat.transcriptions.language import Language

    built = {
        "PromptedWhisperSTT": PromptedWhisperSTT(
            prompt="Victoria.", settings=WhisperSTTServiceMLX.Settings(model="whisper-test", language=Language.EN)
        ),
        "ShortClipStopStrategy": ShortClipStopStrategy(turn_analyzer=ShortClipSmartTurn(), short_clip_s=1.0),
    }
    assert set(built) == set(INSTANCE_INTERNALS)
    missing = [f"{name}.{attr}" for name, attrs in INSTANCE_INTERNALS.items() for attr in attrs if not hasattr(built[name], attr)]
    assert not missing, f"Pipecat no longer sets: {', '.join(missing)}"
    for field in ("model", "language", "temperature", "no_speech_prob"):
        assert hasattr(built["PromptedWhisperSTT"]._settings, field), f"Whisper settings lost {field!r}"


def test_the_shared_models_are_still_wired_into_pipecat() -> None:
    import pipecat.audio.turn.smart_turn.local_smart_turn_v3 as smart_turn
    import pipecat.audio.vad.silero as silero
    import pipecat.services.kokoro.tts as kokoro_tts

    assert kokoro_tts.Kokoro is pipecat_internals.shared_kokoro
    assert smart_turn.ort is pipecat_internals.shared_onnx and silero.onnxruntime is pipecat_internals.shared_onnx


def test_no_other_module_overrides_or_imports_pipecat_private_names() -> None:
    """A class derived from Pipecat that defines a private method Pipecat also has, or an import of a
    private Pipecat name, belongs in pipecat_internals.py."""
    import importlib

    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        if path.name == "pipecat_internals.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("pipecat"):
                for alias in node.names:
                    imported[alias.asname or alias.name] = node.module
                    if alias.name.startswith("_"):
                        found.append(f"{path.name}: imports {node.module}.{alias.name}")
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if not (isinstance(base, ast.Name) and base.id in imported):
                    continue
                parent = getattr(importlib.import_module(imported[base.id]), base.id)
                for member in node.body:
                    name = getattr(member, "name", "")
                    if name.startswith("_") and not name.startswith("__") and hasattr(parent, name):
                        found.append(f"{path.name}: {node.name}.{name} overrides {base.id}.{name}")
    assert not found, "Pipecat internals used outside voice/pipecat_internals.py:\n  " + "\n  ".join(found)
