"""Local transcription via mlx-whisper (Apple Silicon).

Lazy-imports mlx_whisper so the package can still load on machines without it.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


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


def transcribe(
    path: str | Path,
    model: str = "mlx-community/whisper-large-v3-turbo",
    language: str | None = None,
    word_timestamps: bool = False,
) -> TranscriptionResult:
    """Transcribe an audio/video file with mlx-whisper.

    On Apple Silicon this runs on the Neural Engine / GPU and is significantly
    faster than CPU whisper. Models are downloaded on first use to ~/.cache.
    """
    try:
        import mlx_whisper  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "mlx-whisper not installed. Install with: "
            "pip install 'resolve-pilot[transcribe]' or pip install mlx-whisper"
        ) from e

    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)

    kwargs: dict[str, Any] = {"path_or_hf_repo": model}
    if language:
        kwargs["language"] = language
    kwargs["word_timestamps"] = word_timestamps

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
