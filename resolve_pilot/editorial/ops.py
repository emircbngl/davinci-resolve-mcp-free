"""Small, typed edit operations on a RoughCutPlan.

Agents refine a cut many times. Re-sending a 200-clip plan as JSON for every
tweak burns tokens and invites copy mistakes, so the server keeps the plan and
the agent sends operations like "keep clips 3, 1, 7" or "trim 0.3 s off clip 2".

Operations apply in order; indices always refer to the plan as it is at that
step. A batch is all-or-nothing: if one operation fails, nothing changes.
"""
from __future__ import annotations

import copy
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

from resolve_pilot.editorial.roughcut import RoughCutAudioBed, RoughCutClip, RoughCutPlan


class ClipSpec(BaseModel):
    """A clip to add: a range of one source file."""
    source_path: str = Field(description="Absolute path of the source media file.")
    in_seconds: float = Field(0.0, ge=0, description="Source time where the clip starts.")
    duration_seconds: float | None = Field(None, gt=0, description="Clip length. Give this or out_seconds.")
    out_seconds: float | None = Field(None, gt=0, description="Source time where the clip ends.")
    name: str | None = Field(None, description="Clip name shown in Resolve.")
    rationale: str = Field("", description="Why this clip is in the cut (for the user's review).")
    transition_out_seconds: float = Field(0.0, ge=0, description="Cross dissolve into the next clip.")

    @model_validator(mode="after")
    def _one_length(self) -> ClipSpec:
        if (self.duration_seconds is None) == (self.out_seconds is None):
            raise ValueError("give exactly one of duration_seconds or out_seconds")
        if self.out_seconds is not None and self.out_seconds <= self.in_seconds:
            raise ValueError("out_seconds must be greater than in_seconds")
        return self

    def to_clip(self) -> RoughCutClip:
        dur = self.duration_seconds if self.duration_seconds is not None else self.out_seconds - self.in_seconds
        return RoughCutClip(
            source_path=self.source_path,
            in_seconds=self.in_seconds,
            duration_seconds=dur,
            rationale=self.rationale,
            name=self.name,
            transition_out_seconds=self.transition_out_seconds,
        )


class AudioBedSpec(BaseModel):
    """Music or other audio laid under the whole cut, on its own track."""
    source_path: str
    in_seconds: float = Field(0.0, ge=0, description="Where to start inside the audio file.")
    duration_seconds: float | None = Field(None, gt=0, description="Default: to the end of the timeline.")
    offset_seconds: float = Field(0.0, ge=0, description="Timeline position where the audio starts.")
    name: str | None = None

    def to_bed(self) -> RoughCutAudioBed:
        return RoughCutAudioBed(**self.model_dump())


class KeepOp(BaseModel):
    """Keep only these clips, in exactly this order (select + reorder in one step)."""
    op: Literal["keep"]
    indices: list[int] = Field(min_length=1)


class RemoveOp(BaseModel):
    op: Literal["remove"]
    indices: list[int] = Field(min_length=1)


class MoveOp(BaseModel):
    op: Literal["move"]
    index: int
    to: int = Field(description="Position the clip ends up at.")


class TrimOp(BaseModel):
    """Positive values shorten the clip; negative values extend it."""
    op: Literal["trim"]
    index: int
    head_seconds: float = 0.0
    tail_seconds: float = 0.0


class UpdateOp(BaseModel):
    """Change fields of one clip; omitted fields stay as they are.

    Changing only in_seconds keeps the out point (like trimming the head in an NLE).
    """
    op: Literal["update"]
    index: int
    source_path: str | None = None
    in_seconds: float | None = Field(None, ge=0)
    duration_seconds: float | None = Field(None, gt=0)
    out_seconds: float | None = Field(None, gt=0)
    name: str | None = None
    rationale: str | None = None
    transition_out_seconds: float | None = Field(None, ge=0)


class InsertOp(BaseModel):
    op: Literal["insert"]
    clip: ClipSpec
    index: int | None = Field(None, description="Insert before this position; omit to append.")


class SetOp(BaseModel):
    """Change timeline-level settings. Pass audio_bed=null to remove the music bed."""
    op: Literal["set"]
    title: str | None = None
    fps: float | None = Field(None, gt=0)
    width: int | None = Field(None, gt=0)
    height: int | None = Field(None, gt=0)
    notes: str | None = None
    default_transition_seconds: float | None = Field(None, ge=0)
    fade_in_seconds: float | None = Field(None, ge=0)
    fade_out_seconds: float | None = Field(None, ge=0)
    audio_bed: AudioBedSpec | None = None


PlanOp = Annotated[
    Union[KeepOp, RemoveOp, MoveOp, TrimOp, UpdateOp, InsertOp, SetOp],
    Field(discriminator="op"),
]


def _check_index(plan: RoughCutPlan, i: int, *, allow_end: bool = False) -> None:
    n = len(plan.clips)
    hi = n if allow_end else n - 1
    if not 0 <= i <= hi:
        rng = f"0-{hi}" if hi >= 0 else "none (the plan is empty)"
        raise ValueError(f"index {i} is out of range; the plan has {n} clips (valid: {rng})")


def _apply(plan: RoughCutPlan, op: BaseModel) -> str:
    if isinstance(op, KeepOp):
        for i in op.indices:
            _check_index(plan, i)
        if len(set(op.indices)) != len(op.indices):
            raise ValueError("keep: indices contain duplicates; use insert to repeat a clip")
        plan.clips = [plan.clips[i] for i in op.indices]
        return f"kept {len(op.indices)} clips"
    if isinstance(op, RemoveOp):
        for i in op.indices:
            _check_index(plan, i)
        drop = set(op.indices)
        plan.clips = [c for i, c in enumerate(plan.clips) if i not in drop]
        return f"removed {len(drop)} clips"
    if isinstance(op, MoveOp):
        _check_index(plan, op.index)
        _check_index(plan, op.to)
        plan.clips.insert(op.to, plan.clips.pop(op.index))
        return f"moved clip {op.index} to {op.to}"
    if isinstance(op, TrimOp):
        _check_index(plan, op.index)
        c = plan.clips[op.index]
        new_in = c.in_seconds + op.head_seconds
        new_out = c.out_seconds - op.tail_seconds
        if new_in < 0:
            raise ValueError(f"trim: clip {op.index} would start before 0s ({new_in:.3f}s)")
        if new_out - new_in <= 0:
            raise ValueError(f"trim: clip {op.index} would have no duration left")
        c.in_seconds, c.duration_seconds = new_in, new_out - new_in
        return f"trimmed clip {op.index} to {c.in_seconds:.3f}-{c.out_seconds:.3f}s"
    if isinstance(op, UpdateOp):
        _check_index(plan, op.index)
        c = plan.clips[op.index]
        if op.duration_seconds is not None and op.out_seconds is not None:
            raise ValueError("update: give duration_seconds or out_seconds, not both")
        out = c.out_seconds
        for name in ("source_path", "in_seconds", "name", "rationale", "transition_out_seconds"):
            value = getattr(op, name)
            if value is not None:
                setattr(c, name, value)
        if op.duration_seconds is not None:
            c.duration_seconds = op.duration_seconds
        elif op.out_seconds is not None or op.in_seconds is not None:
            # Moving the in point keeps the out point unless a new one is given.
            c.duration_seconds = (op.out_seconds if op.out_seconds is not None else out) - c.in_seconds
        if c.duration_seconds <= 0:
            raise ValueError(f"update: clip {op.index} would have no duration")
        return f"updated clip {op.index}"
    if isinstance(op, InsertOp):
        at = len(plan.clips) if op.index is None else op.index
        _check_index(plan, at, allow_end=True)
        plan.clips.insert(at, op.clip.to_clip())
        return f"inserted a clip at {at}"
    if isinstance(op, SetOp):
        changed = []
        for name in ("title", "fps", "width", "height", "notes", "default_transition_seconds",
                     "fade_in_seconds", "fade_out_seconds"):
            value = getattr(op, name)
            if value is not None:
                setattr(plan, name, value)
                changed.append(name)
        if "audio_bed" in op.model_fields_set:
            plan.audio_bed = op.audio_bed.to_bed() if op.audio_bed else None
            changed.append("audio_bed")
        return "set " + (", ".join(changed) if changed else "nothing")
    raise TypeError(f"unknown operation {op!r}")


def apply_ops(plan: RoughCutPlan, ops: list[BaseModel]) -> tuple[RoughCutPlan, list[str]]:
    """Apply ``ops`` to a copy of ``plan``; returns (new_plan, change_log)."""
    work = copy.deepcopy(plan)
    log = []
    for n, op in enumerate(ops):
        try:
            log.append(_apply(work, op))
        except ValueError as e:
            raise ValueError(f"operation #{n} ({getattr(op, 'op', '?')}): {e}. No changes were saved.") from e
    return work, log
