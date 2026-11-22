"""What is wrong with a cut, found without a person watching it.

Everything is located on the timeline (seconds from its start, and the clip playing
there), from two sources: the plan itself, and measurements of its rendered preview,
whose times are timeline times (see exporters.preview). These are things to look at or
listen to, not verdicts: a 0.3 s "Yes." is a clip, and silence can be a held look.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from resolve_pilot.analyzers.media import MediaInfo
from resolve_pilot.editorial.roughcut import RoughCutPlan
from resolve_pilot.exporters.srt import SrtCue

FLASH_SECONDS = 0.4        # a clip shorter than this is gone before it is seen
SILENCE_SECONDS = 1.5      # dead air worth a look
FREEZE_SECONDS = 1.0
BLACK_SECONDS = 0.2
LEVEL_JUMP_DB = 8.0        # the voice level steps by this much across a cut
_SOUND_DB = -50.0          # quieter than this on a side of a cut: nothing to compare
_CUT_DIP_DB = 6.0          # a cut needs the sound to dip this far below the voice around it
FAST_CAPTION_CPS = 25.0    # characters a second nobody reads
_PER_KIND = 12             # issues listed per kind; the rest are counted
_HOP = 0.01                # seconds per block of the level curve


@dataclass
class Issue:
    kind: str
    at_seconds: float
    detail: str
    clip: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["at_seconds"] = round(self.at_seconds, 2)
        return {k: v for k, v in d.items() if v is not None}


def clip_at(starts: list[float], t: float) -> int | None:
    """The clip playing at timeline second `t`; `starts` has every clip's start and the
    last one's end. None in the fade gaps."""
    for i in range(len(starts) - 1):
        if starts[i] <= t < starts[i + 1]:
            return i
    return None


def real_cuts(plan: RoughCutPlan) -> list[int]:
    """Clip indices i where clip i-1 -> i is a cut, not footage running on unbroken."""
    cuts = []
    for i in range(1, len(plan.clips)):
        a, b = plan.clips[i - 1], plan.clips[i]
        if a.source_path != b.source_path or abs(b.in_seconds - a.out_seconds) > 1e-3:
            cuts.append(i)
    return cuts


def _upper_quartile(xs: list[float]) -> float | None:
    return sorted(xs)[(3 * len(xs)) // 4] if xs else None


def plan_issues(plan: RoughCutPlan, starts: list[float], infos: dict[str, MediaInfo | None]) -> list[Issue]:
    out = []
    for i, c in enumerate(plan.clips):
        length = starts[i + 1] - starts[i]
        if length < FLASH_SECONDS:
            out.append(Issue("flash_clip", starts[i], f"plays for {length:.2f} s "
                             f"({round(length * plan.fps)} frames): too short to take in?", i))
    seen = set()
    for i, c in enumerate(plan.clips):
        info = infos.get(c.source_path)
        if info and info.rotation % 360 and c.source_path not in seen:
            seen.add(c.source_path)
            turned = info.rotation % 180 != 0
            shape = (f"{info.height}x{info.width}" if turned else f"{info.width}x{info.height}")
            out.append(Issue("rotated_source", starts[i],
                             f"{Path(c.source_path).name} is tagged as turned {info.rotation} degrees: "
                             f"players show it as {shape} inside the plan's {plan.width}x{plan.height} "
                             "frame. Look at a frame; if it is on its side, the tag is wrong for "
                             "how the camera was held", i))
    return out


def quality_issues(spans: list, starts: list[float], end: float) -> list[Issue]:
    """Silence, black and frozen stretches of the preview (analyzers.quality spans)."""
    out = []
    for s in spans:
        length = s.end - s.start
        clip = clip_at(starts, s.start + min(length / 2, 0.05))
        if s.kind == "silence" and length >= SILENCE_SECONDS:
            out.append(Issue("silence", s.start, f"nothing is heard for {length:.1f} s", clip))
        elif s.kind == "freeze" and length >= FREEZE_SECONDS:
            out.append(Issue("frozen_picture", s.start, f"the picture stands still for {length:.1f} s", clip))
        elif s.kind == "black" and length >= BLACK_SECONDS:
            # The fade gaps before the first and after the last clip are meant to be black.
            a, b = max(s.start, starts[0]), min(s.end, starts[-1])
            if b - a >= BLACK_SECONDS:
                out.append(Issue("black_picture", a, f"the picture is black for {b - a:.1f} s", clip_at(starts, a)))
    return out


def cut_issues(plan: RoughCutPlan, starts: list[float], levels: list[float]) -> list[Issue]:
    """At every real cut, from the preview's voice-band level curve: does the cut land in
    sound (no dip to sit in: a clipped syllable or a click), and does the level jump?"""
    out = []

    def window(a: float, b: float) -> list[float]:
        return levels[max(0, round(a / _HOP)):max(0, round(b / _HOP))]

    for i in real_cuts(plan):
        t = starts[i]
        before, after = _upper_quartile(window(t - 1.0, t - 0.05)), _upper_quartile(window(t + 0.05, t + 1.0))
        at = window(t - 0.03, t + 0.03)
        if before is None or after is None or not at:
            continue
        if before > _SOUND_DB and after > _SOUND_DB:
            if abs(after - before) >= LEVEL_JUMP_DB:
                out.append(Issue("level_jump", t, f"the sound is {abs(after - before):.0f} dB "
                                 f"{'louder' if after > before else 'quieter'} after this cut", i))
            if min(before, after) - min(at) < _CUT_DIP_DB:
                out.append(Issue("cut_in_sound", t, "no dip in the sound to cut in: listen for a "
                                 "clipped syllable or a click", i))
    return out


def caption_issues(cues: list[SrtCue], starts: list[float]) -> list[Issue]:
    out = []
    for c in cues:
        chars, length = len(c.text.replace("\n", " ")), c.end - c.start
        if length > 0 and chars / length > FAST_CAPTION_CPS:
            out.append(Issue("fast_caption", c.start, f"{chars} characters in {length:.1f} s "
                             f"({chars / length:.0f} a second)", clip_at(starts, c.start)))
    return out


def summarize(issues: list[Issue]) -> dict:
    """{"issues": the first of each kind in timeline order, "issue_counts": all of them}."""
    counts: dict[str, int] = {}
    kept = []
    for issue in sorted(issues, key=lambda x: (x.at_seconds, x.kind)):
        counts[issue.kind] = counts.get(issue.kind, 0) + 1
        if counts[issue.kind] <= _PER_KIND:
            kept.append(issue.to_dict())
    return {"issues": kept, "issue_counts": counts}
