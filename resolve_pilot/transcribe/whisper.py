"""Local transcription with Whisper.

Two engines, lazy-imported so the package loads without either:

- ``mlx`` — mlx-whisper, fast on Apple Silicon (``pip install '.[transcribe]'``).
- ``faster-whisper`` — CTranslate2, runs on Windows / Linux / Intel Macs, CPU or
  CUDA (``pip install '.[transcribe-cpu]'``).

``engine="auto"`` picks mlx when it is installed, otherwise faster-whisper.
"""
from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "large-v3-turbo"

_MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}


@dataclass
class Segment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TranscriptionResult:
    language: str
    text: str
    segments: list[Segment]

    def to_dict(self) -> dict:
        return {"language": self.language, "text": self.text,
                "segments": [s.to_dict() for s in self.segments]}


def available_engines() -> list[str]:
    engines = []
    if importlib.util.find_spec("mlx_whisper"):
        engines.append("mlx")
    if importlib.util.find_spec("faster_whisper"):
        engines.append("faster-whisper")
    return engines


def resolve_engine(engine: str = "auto") -> str:
    found = available_engines()
    if engine == "auto":
        if found:
            return found[0]
        raise RuntimeError(
            "No Whisper engine installed. Apple Silicon: pip install 'davinci-resolve-mcp-free[transcribe]' "
            "(mlx-whisper). Windows/Linux/Intel: pip install 'davinci-resolve-mcp-free[transcribe-cpu]' "
            "(faster-whisper)."
        )
    if engine not in ("mlx", "faster-whisper"):
        raise ValueError(f"engine must be 'auto', 'mlx' or 'faster-whisper', got {engine!r}")
    if engine not in found:
        raise RuntimeError(f"Whisper engine {engine!r} is not installed. Installed: {found or 'none'}")
    return engine


def model_name_for(engine: str, model: str | None) -> str:
    """Map a short model name ("large-v3-turbo", "tiny") to what each engine expects."""
    m = model or DEFAULT_MODEL
    if engine == "mlx":
        return _MLX_REPOS.get(m, m)
    # faster-whisper takes short names; accept mlx repo ids too for convenience.
    if m.startswith("mlx-community/whisper-"):
        m = m[len("mlx-community/whisper-"):].removesuffix("-mlx")
    return m


_faster_models: dict[str, Any] = {}


def transcribe(
    path: str | Path,
    model: str | None = None,
    language: str | None = None,
    word_timestamps: bool = False,
    engine: str = "auto",
) -> TranscriptionResult:
    """Transcribe an audio/video file. Models download on first use (~1.5 GB for turbo)."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    eng = resolve_engine(engine)
    name = model_name_for(eng, model)

    if eng == "mlx":
        import mlx_whisper  # type: ignore

        kwargs: dict[str, Any] = {"path_or_hf_repo": name, "word_timestamps": word_timestamps}
        if language:
            kwargs["language"] = language
        out = mlx_whisper.transcribe(str(p), **kwargs)
        segs = [
            Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]).strip())
            for s in out.get("segments", [])
        ]
        return TranscriptionResult(
            language=str(out.get("language", language or "")),
            text=str(out.get("text", "")).strip(),
            segments=segs,
        )

    from faster_whisper import WhisperModel  # type: ignore

    if name not in _faster_models:
        _faster_models[name] = WhisperModel(name, device="auto", compute_type="default")
    segments, info = _faster_models[name].transcribe(
        str(p), language=language, word_timestamps=word_timestamps,
    )
    segs = [Segment(start=float(s.start), end=float(s.end), text=s.text.strip()) for s in segments]
    return TranscriptionResult(
        language=str(info.language or language or ""),
        text=" ".join(s.text for s in segs).strip(),
        segments=segs,
    )
