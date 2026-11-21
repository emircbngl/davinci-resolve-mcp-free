"""Local transcription with Whisper.

Two engines, lazy-imported so the package loads without either:

- ``mlx`` — mlx-whisper, fast on Apple Silicon (``pip install -e '.[transcribe]'``).
- ``faster-whisper`` — CTranslate2, runs on Windows / Linux / Intel Macs, CPU or
  CUDA (``pip install -e '.[transcribe-cpu]'``).

``engine="auto"`` picks mlx when it is installed, otherwise faster-whisper.
"""
from __future__ import annotations

import importlib.util
import threading
from dataclasses import dataclass, field
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
class Word:
    """One spoken word. Whisper's word edges are approximate (measured: up to ~0.5 s off
    next to a pause), so cuts are snapped to real pauses, see editorial.wordcut."""
    start: float
    end: float
    text: str
    probability: float | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}
        if self.probability is not None:
            d["p"] = round(self.probability, 2)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Word":
        return cls(float(d["start"]), float(d["end"]), str(d["text"]),
                   float(d["p"]) if d.get("p") is not None else None)


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)  # empty without word_timestamps

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"start": self.start, "end": self.end, "text": self.text}
        if self.words:
            d["words"] = [w.to_dict() for w in self.words]
        return d


def _words(raw: Any) -> list[Word]:
    """Words from either engine: mlx gives dicts, faster-whisper objects; empty text dropped."""
    out: list[Word] = []
    for w in raw or []:
        get = w.get if isinstance(w, dict) else (lambda k, _w=w: getattr(_w, k, None))
        text = str(get("word") or "").strip()
        start, end = get("start"), get("end")
        if not text or start is None or end is None:
            continue
        prob = get("probability")
        out.append(Word(float(start), max(float(start), float(end)), text,
                        float(prob) if prob is not None else None))
    return out


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
            "No Whisper engine installed. In the resolve-pilot checkout run "
            "pip install -e '.[transcribe]' on Apple Silicon (mlx-whisper), or "
            "pip install -e '.[transcribe-cpu]' on Windows/Linux/Intel (faster-whisper)."
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
_faster_models_lock = threading.Lock()


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
            Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]).strip(),
                    words=_words(s.get("words")))
            for s in out.get("segments", [])
        ]
        return TranscriptionResult(
            language=str(out.get("language", language or "")),
            text=str(out.get("text", "")).strip(),
            segments=segs,
        )

    from faster_whisper import WhisperModel  # type: ignore

    with _faster_models_lock:
        if name not in _faster_models:
            _faster_models[name] = WhisperModel(name, device="auto", compute_type="default")
        whisper_model = _faster_models[name]
    segments, info = whisper_model.transcribe(
        str(p), language=language, word_timestamps=word_timestamps,
    )
    segs = [Segment(start=float(s.start), end=float(s.end), text=s.text.strip(),
                    words=_words(getattr(s, "words", None))) for s in segments]
    return TranscriptionResult(
        language=str(info.language or language or ""),
        text=" ".join(s.text for s in segs).strip(),
        segments=segs,
    )
