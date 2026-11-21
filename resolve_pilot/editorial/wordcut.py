"""Word-level cut points and speech-cleanup candidates. Pure functions over a transcript.

Whisper's word edges are approximate. Measured against ffmpeg's silencedetect (Turkish
speech, large-v3-turbo): a word after a pause "starts" up to ~0.5 s early, inside the
pause, and a word before a pause can "end" ~0.3 s early, inside the word. Cutting on
those times clips words. So a cut is placed in the *real* pause next to the word
(`pauses`, from analyzers.speech) and only falls back to Whisper's times where the
speech is continuous or the pauses could not be measured; such cuts are reported as tight.

Words are addressed as "segment.word" ("12.3"), ranges as "12.3-5" (inclusive).
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from resolve_pilot.transcribe.whisper import Segment, Word

# How far a real pause may lie from where Whisper puts the word edge (see above).
_END_LATE, _START_EARLY, _INSIDE = 0.12, 0.5, 0.05


@dataclass(frozen=True)
class Ref:
    """A word and where it sits: segment index, index in the segment, index overall."""
    seg: int
    idx: int
    pos: int
    word: Word

    @property
    def address(self) -> str:
        return f"{self.seg}.{self.idx}"


def flatten(segments: list[Segment]) -> list[Ref]:
    """Every word in order. A segment without words counts as one word (its whole span),
    so transcripts made without word timestamps go through the same code."""
    refs: list[Ref] = []
    for s, seg in enumerate(segments):
        for i, w in enumerate(seg.words or [Word(seg.start, seg.end, seg.text)]):
            refs.append(Ref(s, i, len(refs), w))
    return refs


class Pauses:
    """Measured pauses, searchable by time. ``None`` input means "not measured"."""

    def __init__(self, pauses: list[tuple[float, float]] | None):
        self.measured = pauses is not None
        self._p = sorted(pauses or [])
        self._starts = [a for a, _ in self._p]

    def between(self, before: Word | None, after: Word | None) -> tuple[float, float] | None:
        """The real pause between two neighbouring words (None = start / end of the file)."""
        lo = max(before.end - _END_LATE, before.start + _INSIDE) if before else 0.0
        hi = min(after.start + _START_EARLY, after.end - _INSIDE) if after else math.inf
        hi = max(hi, lo)
        best, best_overlap = None, 0.0
        i = bisect.bisect_right(self._starts, hi)
        while i > 0:
            i -= 1
            a, b = self._p[i]
            if b < lo:
                # Pauses do not overlap each other, so nothing earlier reaches the window.
                break
            overlap = min(b, hi) - max(a, lo)
            if best is None or overlap > best_overlap:
                best, best_overlap = (a, b), overlap
        return best


@dataclass
class Span:
    """One clip: words ``first``..``last`` of a segment and the cut points around them."""
    seg: int
    first: int
    last: int
    start: float
    end: float
    text: str
    start_in_pause: bool
    end_in_pause: bool
    whole_segment: bool


def parse_drop_words(specs: list[str] | None, segments: list[Segment]) -> set[tuple[int, int]]:
    """{"12.3", "12.5-7"} -> {(12, 3), (12, 5), (12, 6), (12, 7)}, validated."""
    drop: set[tuple[int, int]] = set()
    for spec in specs or []:
        m = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\s*-\s*(\d+))?\s*", str(spec))
        if not m:
            raise ValueError(f"drop_words entry {spec!r}: write 'segment.word' or "
                             "'segment.first-last', e.g. '12.3' or '12.3-5'")
        seg, first, last = int(m[1]), int(m[2]), int(m[3] if m[3] is not None else m[2])
        if not 0 <= seg < len(segments):
            raise ValueError(f"drop_words {spec!r}: the transcript has segments 0-{len(segments) - 1}")
        count = len(segments[seg].words)
        if count == 0:
            raise ValueError(f"drop_words {spec!r}: segment {seg} has no word timings "
                             "(transcribe with word_timestamps=true)")
        if not first <= last < count:
            raise ValueError(f"drop_words {spec!r}: segment {seg} has words 0-{count - 1}")
        drop.update((seg, i) for i in range(first, last + 1))
    return drop


def cut_spans(
    segments: list[Segment],
    picked: list[int],
    pauses: list[tuple[float, float]] | None = None,
    drop: set[tuple[int, int]] | None = None,
    pad_seconds: float = 0.2,
    max_pause_seconds: float | None = None,
    duration: float | None = None,
) -> list[Span]:
    """Clips for the picked segments, in that order, with every cut placed between words.

    Dropped words split a segment into several clips. A pause longer than
    ``max_pause_seconds`` between two kept words is shortened to that length (which also
    splits the clip). The pause between two clips is at most ``2 * pad_seconds``; cuts
    never reach into a neighbouring word that is not kept.
    """
    flat = flatten(segments)
    by_seg: dict[int, list[Ref]] = {}
    for r in flat:
        by_seg.setdefault(r.seg, []).append(r)
    found = Pauses(pauses)
    drop = drop or set()

    def edge(ref: Ref, side: int, pad: float) -> tuple[float, bool]:
        """Cut point before (side=-1) or after (side=+1) a kept word."""
        j = ref.pos + side
        other = flat[j].word if 0 <= j < len(flat) else None
        before, after = (other, ref.word) if side < 0 else (ref.word, other)
        gap = found.between(before, after)
        if gap:
            room = min(pad, (gap[1] - gap[0]) / 2)
            return (gap[1] - room, True) if side < 0 else (gap[0] + room, True)
        # No pause there. At the very start / end of the speech there is no neighbour to
        # cut into, so that is not a tight cut; between two words it is.
        if side < 0:
            t = ref.word.start - pad
            return (max(t, (other.end + ref.word.start) / 2) if other else max(0.0, t)), other is None
        t = ref.word.end + pad
        return (min(t, (ref.word.end + other.start) / 2) if other else t), other is None

    spans: list[Span] = []
    for seg in picked:
        kept = [r for r in by_seg.get(seg, []) if (r.seg, r.idx) not in drop]
        runs: list[tuple[list[Ref], float, float]] = []  # words, pad before, pad after
        for r in kept:
            pad_before = pad_seconds
            if runs and runs[-1][0][-1].idx == r.idx - 1:
                gap = found.between(runs[-1][0][-1].word, r.word) if max_pause_seconds is not None else None
                if not gap or gap[1] - gap[0] <= max_pause_seconds + 1e-6:
                    runs[-1][0].append(r)
                    continue
                # Too long a pause between two kept words: keep max_pause_seconds of it.
                pad_before = max_pause_seconds / 2
                runs[-1] = (runs[-1][0], runs[-1][1], pad_before)
            runs.append(([r], pad_before, pad_seconds))
        for words, pad_before, pad_after in runs:
            start, start_ok = edge(words[0], -1, pad_before)
            end, end_ok = edge(words[-1], +1, pad_after)
            if duration:
                end = min(end, duration)
            start = max(0.0, start)
            if end - start <= 1e-3:
                continue
            whole = len(words) == len(by_seg[seg])
            text = segments[seg].text if whole else " ".join(r.word.text for r in words)
            spans.append(Span(seg, words[0].idx, words[-1].idx, start, end, text.strip(),
                              start_ok, end_ok, whole))
    return spans


def cut_report(spans: list[Span], pauses_measured: bool, limit: int = 20) -> dict:
    """How many cuts sit in a real pause, and which do not (they may sound abrupt)."""
    tight = []
    for i, s in enumerate(spans):
        sides = [name for name, ok in (("start", s.start_in_pause), ("end", s.end_in_pause)) if not ok]
        if sides:
            tight.append({"clip": i, "sides": sides, "text": s.text[:40]})
    total = 2 * len(spans)
    in_pause = total - sum(len(t["sides"]) for t in tight)
    out: dict = {"cuts": total, "in_a_pause": in_pause, "tight": total - in_pause}
    if tight:
        out["tight_clips"] = tight[:limit]
        if len(tight) > limit:
            out["more_tight_clips"] = len(tight) - limit
    if not pauses_measured:
        out["note"] = ("The audio's pauses were not measured, so every cut uses Whisper's "
                       "approximate word times. Listen to the cuts.")
    elif tight:
        out["note"] = ("Tight cuts have no pause to sit in (continuous speech): they use "
                       "Whisper's approximate word times and may clip a syllable. Check them, "
                       "or move the cut to a nearby pause.")
    return out


# ── Reading words ───────────────────────────────────────────────────────────

def format_words(segments: list[Segment], pauses: list[tuple[float, float]] | None,
                 offset: int = 0, limit: int = 200, min_pause_seconds: float = 0.3) -> list[str]:
    """One line per segment: ``[i] start-end 0:word 1:word (0.8s) 2:word``.

    ``(0.8s)`` marks a measured pause of that length before the next word, also at the
    start of a line (the pause since the previous segment).
    """
    found = Pauses(pauses)
    rows = list(enumerate(segments))[offset:offset + limit if limit > 0 else None]
    lines = []
    for i, seg in rows:
        if not seg.words:
            lines.append(f"[{i}] {seg.start:.2f}-{seg.end:.2f} {seg.text.strip()} (no word timings)")
            continue
        parts: list[str] = []
        prev: Word | None = segments[i - 1].words[-1] if i > 0 and segments[i - 1].words else None
        for j, w in enumerate(seg.words):
            gap = found.between(prev, w) if prev else None
            if gap and gap[1] - gap[0] >= min_pause_seconds:
                parts.append(f"({gap[1] - gap[0]:.1f}s)")
            parts.append(f"{j}:{w.text}")
            prev = w
        lines.append(f"[{i}] {seg.start:.2f}-{seg.end:.2f} " + " ".join(parts))
    return lines


# ── Candidates for cleaning up speech ───────────────────────────────────────

# Sounds of hesitation in any language: uh, um, erm, hmm, mm, ah, Turkish ı / ıh / ee.
_HESITATION = re.compile(r"u+h+|u+m+|u+h+m+|e+r+m*|h+m+|m{2,}|m+h+m+|a+h+|ı+h*|ı+m+|e{2,}h*")
# Real words that are often filler. Flagged as "discourse": the editor decides.
_DISCOURSE = {
    "en": {"like", "basically", "actually", "literally"},
    "tr": {"şey", "yani", "işte", "hani", "aslında"},
}
_EDGE_PUNCT = re.compile(r"^\W+|\W+$")


def normalize(text: str, language: str | None = None) -> str:
    t = _EDGE_PUNCT.sub("", text)
    if (language or "").lower().startswith("tr"):
        t = t.replace("İ", "i").replace("I", "ı")
    return t.lower()


def _specs(refs: list[Ref]) -> list[str]:
    """Word ranges as drop_words entries, one per segment."""
    runs: list[tuple[int, int, int]] = []
    for r in refs:
        if runs and runs[-1][0] == r.seg and runs[-1][2] == r.idx - 1:
            runs[-1] = (r.seg, runs[-1][1], r.idx)
        else:
            runs.append((r.seg, r.idx, r.idx))
    return [f"{s}.{a}" if a == b else f"{s}.{a}-{b}" for s, a, b in runs]


def scan(segments: list[Segment], pauses: list[tuple[float, float]] | None,
         language: str | None = None, min_pause_seconds: float = 0.7,
         extra_fillers: list[str] | None = None, limit: int = 40) -> dict:
    """Candidates an editor would look at: long pauses, fillers, repeats, retakes.

    Nothing here is a decision. Each entry says what removing it takes (``drop`` entries
    for plan_from_transcript's drop_words, or the segment to leave out).
    """
    has_words = any(s.words for s in segments)
    flat = [r for r in flatten(segments) if segments[r.seg].words]
    words = [normalize(r.word.text, language) for r in flat]
    discourse = _DISCOURSE.get((language or "").lower()[:2], set())
    extra = {normalize(f, language) for f in extra_fillers or []}

    def is_hesitation(w: str) -> bool:
        return bool(w) and (w in extra or _HESITATION.fullmatch(w) is not None)

    fillers = []
    for r, w in zip(flat, words):
        kind = "hesitation" if is_hesitation(w) else "discourse" if w in discourse else None
        if kind:
            entry = {"drop": r.address, "text": r.word.text, "kind": kind, "at": round(r.word.start, 2)}
            if r.word.probability is not None and r.word.probability < 0.6:
                entry["unsure"] = True  # Whisper was not confident this was said
            fillers.append(entry)

    repeats = []
    i = 0
    while i < len(words):
        hit = 0
        for n in (4, 3, 2, 1):
            a, b = words[i:i + n], words[i + n:i + 2 * n]
            if len(b) == n and a == b and all(a) and not all(is_hesitation(w) for w in a):
                hit = n
                break
        if hit:
            first = flat[i:i + hit]
            repeats.append({"drop": _specs(first), "text": " ".join(r.word.text for r in first),
                            "at": round(first[0].word.start, 2)})
            i += hit
        else:
            i += 1

    def content(seg: int) -> list[str]:
        raw = [w.text for w in segments[seg].words] or segments[seg].text.split()
        ws = [normalize(w, language) for w in raw]
        return [w for w in ws if w and not is_hesitation(w) and w not in discourse][:8]

    retakes = []
    for a in range(len(segments)):
        for b in (a + 1, a + 2):
            if b >= len(segments):
                break
            short, long_ = sorted((content(a), content(b)), key=len)
            if len(short) < 3:
                continue
            blocks = SequenceMatcher(None, short, long_[:len(short) + 3], autojunk=False).get_matching_blocks()
            similarity = sum(m.size for m in blocks) / len(short)
            if similarity >= 0.7:
                retakes.append({"earlier": a, "later": b, "similarity": round(similarity, 2),
                                "earlier_text": segments[a].text.strip()[:60],
                                "later_text": segments[b].text.strip()[:60]})
                break

    long_pauses = []
    if pauses and flat:
        mids = [(r.word.start + r.word.end) / 2 for r in flat]
        for a, b in pauses:
            if b - a < min_pause_seconds or b <= flat[0].word.start or a >= flat[-1].word.end:
                continue
            k = bisect.bisect_right(mids, (a + b) / 2)
            if not 0 < k < len(flat):
                continue
            before, after = flat[k - 1], flat[k]
            long_pauses.append({"start": round(a, 2), "end": round(b, 2), "seconds": round(b - a, 2),
                                "after_word": before.address, "before_word": after.address,
                                "inside_segment": before.seg == after.seg})

    def capped(name: str, items: list) -> dict:
        out = {name: items[:limit]}
        if len(items) > limit:
            out[f"more_{name}"] = len(items) - limit
        return out

    return {
        "word_timings": has_words,
        "pauses_measured": pauses is not None,
        **capped("long_pauses", long_pauses),
        "long_pause_seconds_total": round(sum(p["seconds"] for p in long_pauses), 1),
        **capped("fillers", fillers),
        **capped("repeats", repeats),
        **capped("retakes", retakes),
    }
