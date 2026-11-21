"""Word-level cut points and speech-cleanup candidates. Pure functions over a transcript.

Whisper's word edges are approximate. Measured on a recording with known pauses
(large-v3-turbo): a word after a pause "starts" up to ~0.5 s early, inside the pause,
and a word before a pause can "end" ~0.3 s early, inside the word. Cutting on those
times clips words. So a cut is placed where the voice actually dips between the two
words: the valley in the audio's voice-band level (`levels`, from analyzers.speech).

A valley that is wide and deep enough is a pause, and the cut keeps `pad_seconds` of
it. Otherwise the speech is continuous there: the cut goes to the quietest point
between the words and is reported as tight. Without levels, cuts fall back to
Whisper's times and every one is reported as tight.

Words are addressed as "segment.word" ("12.3"), ranges as "12.3-5" (inclusive).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from resolve_pilot.transcribe.whisper import Segment, Word

# How far the real gap may lie from where Whisper puts the word edges (see above), and
# how far before the first / after the last word to look for quiet.
_END_LATE, _START_EARLY, _INSIDE, _EDGE_REACH = 0.12, 0.5, 0.05, 1.0
# Words further apart than this (by Whisper's times) get a cut point each, within
# _NEAR_WORD of the word, instead of one shared valley: what lies between is not speech.
_FAR_APART, _NEAR_WORD = 0.65, 0.5
# The valley is everything around the quietest point that stays within 6 dB of it, or,
# in a clean recording, everything 20 dB below the neighbouring words (at 12 dB the
# valley began ~50 ms early, on the fading tail of the word before it).
_ABOVE_MINIMUM_DB, _BELOW_WORDS_DB = 6.0, 20.0
# A valley is a pause from this width and depth. Measured on handheld vlog footage:
# real gaps are a median 240 ms wide and 8-12 dB below the words, the dips inside
# continuous speech a median 90 ms wide.
_PAUSE_SECONDS, _PAUSE_DEPTH_DB = 0.12, 8.0


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


@dataclass(frozen=True)
class Valley:
    """Where the voice dips between two words."""
    start: float
    end: float
    is_pause: bool
    lowest: float  # the quietest moment in it

    @property
    def seconds(self) -> float:
        return self.end - self.start


class Sound:
    """The audio's voice-band level every `hop` seconds. ``None`` means "not measured"."""

    def __init__(self, levels: list[float] | None, hop: float = 0.01):
        self.measured = levels is not None
        self._lv = levels or []
        self._hop = hop

    def _blocks(self, a: float, b: float) -> list[float]:
        i = min(len(self._lv) - 1, max(0, int(a / self._hop)))
        return self._lv[i:max(i + 1, math.ceil(b / self._hop))]

    def edge(self, before: Word | None, after: Word | None, side: int = 0) -> Valley | None:
        """The valley to cut in between two neighbouring words (None = start / end of
        the file). When the words are far apart (music, laughter, a long silence between
        them), ``side`` picks the valley next to one of them: +1 right after ``before``,
        -1 right before ``after``; whatever lies between the two is then left out."""
        lv, hop = self._lv, self._hop
        if not lv or (before is None and after is None):
            return None
        words = (self._blocks(before.start, before.end) if before else []) + \
                (self._blocks(after.start, after.end) if after else [])
        # How loud the two words are: the upper quartile, because Whisper's span for a
        # word can be half pause.
        loud = sorted(words)[len(words) * 3 // 4]
        last = len(lv) - 1

        def index(t: float) -> int:
            return min(last, max(0, round(t / hop)))

        # The window is Whisper's own gap between the two words, widened only where
        # the sound says the word edge is wrong: while it is still quiet where the
        # next word is said to have started (Whisper pulls a word's start back into
        # the pause before it), and where it was already quiet before the previous
        # word is said to have ended.
        if before:
            i0, stop = index(before.end), index(max(before.end - _END_LATE, before.start + _INSIDE))
            while i0 > stop and lv[i0 - 1] <= loud - _PAUSE_DEPTH_DB:
                i0 -= 1
        else:
            i0 = index(after.start - _EDGE_REACH)
        if after:
            i1, stop = index(after.start), index(min(after.start + _START_EARLY, after.end - _INSIDE))
            while i1 < stop and lv[i1] <= loud - _PAUSE_DEPTH_DB:
                i1 += 1
        else:
            i1 = index(before.end + _EDGE_REACH) + 1
        boundary = ((before.end if before else after.start) + (after.start if after else before.end)) / 2
        if before and after and after.start - before.end > _FAR_APART:
            if side > 0:
                i1, boundary = index(before.end + _NEAR_WORD), before.end
            elif side < 0:
                i0, boundary = index(after.start - _NEAR_WORD), after.start
        i0 = min(i0, last)
        i1 = min(len(lv), max(i1, i0 + 1))
        low = min(lv[i0:i1])
        k = lv.index(low, i0, i1)
        # The quietest 30 ms; among equals, the one nearest to where Whisper puts the
        # boundary (so a flat stretch does not move the cut away from it).
        lowest = min(range(i0, i1), key=lambda j: (
            round(sum(lv[max(i0, j - 1):min(i1, j + 2)]) / len(lv[max(i0, j - 1):min(i1, j + 2)])),
            abs((j + 0.5) * hop - boundary)))
        quiet = max(low + _ABOVE_MINIMUM_DB, loud - _BELOW_WORDS_DB)
        # Quiet before the first word may run back to the start of the file, and after
        # the last word on to its end; between two words it stays in the window.
        left, right = (i0 if before else 0), (i1 if after else len(lv))
        a, b = k, k + 1
        while a > left and lv[a - 1] <= quiet:
            a -= 1
        while b < right and lv[b] <= quiet:
            b += 1
        is_pause = (b - a) * hop >= _PAUSE_SECONDS - 1e-9 and loud - low >= _PAUSE_DEPTH_DB
        return Valley(a * hop, b * hop, is_pause, (lowest + 0.5) * hop)

    def gap(self, before: Word, after: Word) -> Valley | None:
        """The pause in speech between two consecutive words, if there is one. Far-apart
        words always have one: it runs from the quiet after the first to the quiet before
        the second, and may contain sound that is not speech."""
        if not self._lv:
            return None
        if after.start - before.end <= _FAR_APART:
            valley = self.edge(before, after)
            return valley if valley and valley.is_pause else None
        out, back = self.edge(before, after, +1), self.edge(before, after, -1)
        start = out.start if out.is_pause else before.end
        end = back.end if back.is_pause else after.start
        return Valley(start, max(start, end), True, start)


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
    levels: list[float] | None = None,
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
    sound = Sound(levels)
    drop = drop or set()

    def edge(ref: Ref, side: int, pad: float) -> tuple[float, bool]:
        """Cut point before (side=-1) or after (side=+1) a kept word."""
        j = ref.pos + side
        other = flat[j].word if 0 <= j < len(flat) else None
        before, after = (other, ref.word) if side < 0 else (ref.word, other)
        valley = sound.edge(before, after, side)
        if valley and valley.is_pause:
            room = min(pad, valley.seconds / 2)
            return (valley.end - room, True) if side < 0 else (valley.start + room, True)
        if other is None:
            # The very start / end of the speech, with no quiet around it: there is no
            # neighbour to cut into, so this is not a tight cut.
            return (max(0.0, ref.word.start - pad) if side < 0 else ref.word.end + pad), True
        if valley:
            # Continuous speech: the quietest point between the two words. Both sides
            # of the gap get the same point, so nothing is played twice.
            return valley.lowest, False
        if side < 0:
            return max(ref.word.start - pad, (other.end + ref.word.start) / 2), False
        return min(ref.word.end + pad, (ref.word.end + other.start) / 2), False

    spans: list[Span] = []
    for seg in picked:
        kept = [r for r in by_seg.get(seg, []) if (r.seg, r.idx) not in drop]
        runs: list[tuple[list[Ref], float, float]] = []  # words, pad before, pad after
        for r in kept:
            pad_before = pad_seconds
            if runs and runs[-1][0][-1].idx == r.idx - 1:
                gap = sound.gap(runs[-1][0][-1].word, r.word) if max_pause_seconds is not None else None
                if not (gap and gap.seconds > max_pause_seconds + 1e-6):
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


def cut_report(spans: list[Span], measured: bool, limit: int = 20) -> dict:
    """How many cuts sit in a real pause, and which do not (they may sound abrupt).

    Where one clip ends exactly where the next begins, nothing is cut out: the join is
    seamless and not counted.
    """
    def joined(a: Span, b: Span) -> bool:
        return abs(b.start - a.end) < 1e-6

    tight, total = [], 0
    for i, s in enumerate(spans):
        sides = []
        if not (i > 0 and joined(spans[i - 1], s)):
            total += 1
            if not s.start_in_pause:
                sides.append("start")
        if not (i + 1 < len(spans) and joined(s, spans[i + 1])):
            total += 1
            if not s.end_in_pause:
                sides.append("end")
        if sides:
            tight.append({"clip": i, "sides": sides, "text": s.text[:40]})
    in_pause = total - sum(len(t["sides"]) for t in tight)
    out: dict = {"cuts": total, "in_a_pause": in_pause, "tight": total - in_pause}
    if tight:
        out["tight_clips"] = tight[:limit]
        if len(tight) > limit:
            out["more_tight_clips"] = len(tight) - limit
    if not measured:
        out["note"] = ("The audio's pauses were not measured, so every cut uses Whisper's "
                       "approximate word times. Listen to the cuts.")
    elif tight:
        out["note"] = ("Tight cuts have no pause to sit in (continuous speech): they are at the "
                       "quietest point between the two words and may still clip a syllable. "
                       "Check them, or cut from pause to pause instead.")
    return out


# ── Reading words ───────────────────────────────────────────────────────────

def format_words(segments: list[Segment], levels: list[float] | None,
                 offset: int = 0, limit: int = 200, min_pause_seconds: float = 0.3) -> list[str]:
    """One line per segment: ``[i] start-end 0:word 1:word (0.8s) 2:word``.

    ``(0.8s)`` marks a measured pause of that length before the next word, also at the
    start of a line (the pause since the previous segment).
    """
    sound = Sound(levels)
    rows = list(enumerate(segments))[offset:offset + limit if limit > 0 else None]
    lines = []
    for i, seg in rows:
        if not seg.words:
            lines.append(f"[{i}] {seg.start:.2f}-{seg.end:.2f} {seg.text.strip()} (no word timings)")
            continue
        parts: list[str] = []
        prev: Word | None = segments[i - 1].words[-1] if i > 0 and segments[i - 1].words else None
        for j, w in enumerate(seg.words):
            gap = sound.gap(prev, w) if prev else None
            if gap and gap.seconds >= min_pause_seconds:
                parts.append(f"({gap.seconds:.1f}s)")
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


def scan(segments: list[Segment], levels: list[float] | None,
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

    sound = Sound(levels)
    long_pauses = []
    for before, after in zip(flat, flat[1:]):
        gap = sound.gap(before.word, after.word)
        if gap and gap.seconds >= min_pause_seconds:
            long_pauses.append({"start": round(gap.start, 2), "end": round(gap.end, 2),
                                "seconds": round(gap.seconds, 2),
                                "after_word": before.address, "before_word": after.address,
                                "inside_segment": before.seg == after.seg})

    def capped(name: str, items: list) -> dict:
        out = {name: items[:limit]}
        if len(items) > limit:
            out[f"more_{name}"] = len(items) - limit
        return out

    return {
        "word_timings": has_words,
        "pauses_measured": levels is not None,
        **capped("long_pauses", long_pauses),
        "long_pause_seconds_total": round(sum(p["seconds"] for p in long_pauses), 1),
        **capped("fillers", fillers),
        **capped("repeats", repeats),
        **capped("retakes", retakes),
    }
