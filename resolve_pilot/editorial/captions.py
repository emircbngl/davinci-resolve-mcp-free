"""Captions that follow a rough cut.

A transcript's times belong to its source file. Once a plan keeps some segments, drops
words and reorders the rest, every caption has to move with the clip it is spoken in.
Each clip takes the transcript words whose middle lies inside its source range and puts
them at the clip's place on the timeline, using the exporter's own frame positions
(`fcpxml.layout`), so the captions line up with the timeline Resolve imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from resolve_pilot.editorial.roughcut import RoughCutPlan, plan_to_timeline_spec
from resolve_pilot.exporters.fcpxml import layout
from resolve_pilot.exporters.srt import SrtCue, _wrap_words, wrap_cues
from resolve_pilot.transcribe.whisper import Segment

_SENTENCE_END = (".", "?", "!", "…")
_SENTENCE_MIN_CHARS = 12   # do not end a cue after "OK." alone
MIN_CUE_SECONDS = 0.7      # a caption stays up at least this long when there is room
MAX_CUE_SECONDS = 6.0      # slow speech: a caption does not sit there longer than this
_READING_CHARS_PER_SECOND = 17.0  # fast speech: held this long after it, when there is room
_HOLD_GAP_SECONDS = 0.3    # gaps this short are closed: the caption holds until the next
_PAUSE_SECONDS = 0.3       # a pause (or a cut) ends a cue that already has a line's worth
_NEW_CUE_GAP_SECONDS = 1.0  # a longer silence on the timeline always starts a new cue


@dataclass
class Piece:
    """A word (or, without word timings, a whole segment) at its place on the timeline."""
    start: float
    end: float
    text: str


def source_key(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _middles(words: list) -> list[float]:
    """Where each word is, for deciding which clip plays it: its middle. Whisper now and
    then gives a word no length, stacked on a neighbour's edge (which may be the very point
    of a cut); such a word goes where the next word with a length is, else the previous."""
    mids = [(w.start + w.end) / 2 if w.end > w.start else None for w in words]
    for order in (range(len(mids) - 1, -1, -1), range(len(mids))):
        near = None
        for i in order:
            if mids[i] is None:
                mids[i] = near
            else:
                near = mids[i]
    return [m if m is not None else w.start for m, w in zip(mids, words)]


def timeline_pieces(plan: RoughCutPlan, transcripts: dict[str, list[Segment]]) -> tuple[list[Piece], list[int]]:
    """(what is said on the timeline, in order; clips whose source has no transcript).

    ``transcripts`` maps a source file to its transcript segments.
    """
    if not plan.clips or plan.fps <= 0:
        return [], []
    bounds, fps = layout(plan_to_timeline_spec(plan)).bounds, plan.fps
    known = {source_key(k): v for k, v in transcripts.items()}
    pieces: list[Piece] = []
    missing: list[int] = []
    for i, clip in enumerate(plan.clips):
        segments = known.get(source_key(clip.source_path))
        if segments is None:
            missing.append(i)
            continue
        t0, t1 = bounds[i] / fps, bounds[i + 1] / fps
        a, b = clip.in_seconds, clip.out_seconds
        for seg in segments:
            if seg.end <= a or seg.start >= b:
                continue
            middles = _middles(seg.words)
            for n, unit in enumerate(seg.words or [seg]):
                if seg.words:
                    inside = a <= middles[n] < b
                else:  # a whole segment counts when at least half of it plays
                    shared = min(unit.end, b) - max(unit.start, a)
                    inside = shared > 0 and shared >= (unit.end - unit.start) / 2
                text = unit.text.strip()
                if not inside or not text:
                    continue
                start = min(t1, max(t0, t0 + unit.start - a))
                pieces.append(Piece(start, min(t1, max(start, t0 + unit.end - a)), text))
    return pieces, missing


def cues_from_pieces(pieces: list[Piece], max_chars_per_line: int = 42, max_lines: int = 2,
                     timeline_end: float | None = None) -> list[SrtCue]:
    """Group words into cues. A new cue starts when the lines are full, after a sentence
    (unless it flashed by in under MIN_CUE_SECONDS: then it shares a cue with the next),
    at a pause once there is a line's worth (speech Whisper left unpunctuated), after
    MAX_CUE_SECONDS and after a silence. Cues never overlap; one that is short for its
    text is held longer when nothing follows right away."""
    max_lines = max(1, max_lines)
    groups: list[list[Piece]] = []
    for p in pieces:
        cur = groups[-1] if groups else None
        if cur:
            text = " ".join(x.text for x in cur)
            gap = p.start - cur[-1].end
            full = max_chars_per_line > 0 and len(
                _wrap_words((text + " " + p.text).split(), max_chars_per_line)) > max_lines
            sentence = (cur[-1].text.endswith(_SENTENCE_END) and len(text) >= _SENTENCE_MIN_CHARS
                        and p.start - cur[0].start >= MIN_CUE_SECONDS)
            pause = gap >= _PAUSE_SECONDS and 0 < max_chars_per_line <= len(text)
            too_long = p.end - cur[0].start > MAX_CUE_SECONDS
            if full or sentence or pause or too_long or gap > _NEW_CUE_GAP_SECONDS:
                cur = None
        if cur is None:
            groups.append([p])
        else:
            cur.append(p)
    cues = [SrtCue(g[0].start, g[-1].end, " ".join(x.text for x in g)) for g in groups]
    for i, cue in enumerate(cues):
        limit = cues[i + 1].start if i + 1 < len(cues) else timeline_end
        readable = cue.start + max(MIN_CUE_SECONDS, len(cue.text) / _READING_CHARS_PER_SECOND)
        if limit is None:
            cue.end = max(cue.end, readable)
            continue
        if limit - cue.end <= _HOLD_GAP_SECONDS:  # also when the next one starts before this ends
            cue.end = limit
        else:
            cue.end = max(cue.end, min(limit, readable))
    return wrap_cues([c for c in cues if c.end > c.start], max_chars_per_line, max_lines)


def plan_captions(plan: RoughCutPlan, transcripts: dict[str, list[Segment]],
                  max_chars_per_line: int = 42, max_lines: int = 2) -> tuple[list[SrtCue], dict]:
    """(cues timed to the plan's timeline, report)."""
    pieces, missing = timeline_pieces(plan, transcripts)
    end = None
    if plan.clips and plan.fps > 0:
        end = layout(plan_to_timeline_spec(plan)).bounds[-1] / plan.fps
    cues = cues_from_pieces(pieces, max_chars_per_line, max_lines, end)
    used = {source_key(c.source_path) for c in plan.clips}
    word_level = all(any(s.words for s in segs) for path, segs in transcripts.items()
                     if source_key(path) in used and segs)
    report = {"clips": len(plan.clips), "clips_without_transcript": missing, "word_level": word_level}
    return cues, report
