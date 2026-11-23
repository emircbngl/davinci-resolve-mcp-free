"""import_timeline(plan_id=...): the timeline Resolve built is compared with the plan."""
from __future__ import annotations

import re
import unicodedata

import pytest

from resolve_pilot import control
from resolve_pilot.bridge import client as bridge
from resolve_pilot.editorial import timeline_check
from resolve_pilot.editorial.roughcut import RoughCutClip, RoughCutPlan
from tests.conftest import needs_ffmpeg, needs_luajit


def _plan(**kw) -> RoughCutPlan:
    # Three clips of two files at 24 fps: frames 0-48, 48-54, 54-90.
    return RoughCutPlan(title="Cut", fps=24.0, width=1920, height=1080, clips=[
        RoughCutClip("/footage/a.mov", in_seconds=1.0, duration_seconds=2.0),
        RoughCutClip("/footage/b.mov", in_seconds=10.0, duration_seconds=0.25),
        RoughCutClip("/footage/a.mov", in_seconds=5.0, duration_seconds=1.5)], **kw)


def _rows():
    return [[0, 48, 24, "/footage/a.mov", 0, 1, 1], [48, 54, 240, "/footage/b.mov", 0, 1, 1],
            [54, 90, 120, "/footage/a.mov", 0, 1, 1]]


def test_a_timeline_that_is_the_plan_passes():
    assert timeline_check.check(_plan(), _rows(), 24) == {"ok": True, "clips_in_plan": 3, "clips_on_timeline": 3}


def test_a_clip_that_is_a_frame_short_is_named():
    rows = _rows()
    rows[1][1] = 53                      # the one-frame hole Resolve left on the real import
    out = timeline_check.check(_plan(), rows, 24)
    assert out["ok"] is False and out["positions_off_count"] == 1
    assert out["positions_off"] == [{"clip": 1, "timeline": [48, 53], "plan": [48, 54]}]
    assert "sources_off" not in out and "turns_off" not in out


def test_another_file_or_another_in_point_is_named():
    rows = _rows()
    rows[0][3] = "/footage/b.mov"        # the wrong file
    rows[2][2] = 122                     # two frames late into the right file
    out = timeline_check.check(_plan(), rows, 24)
    assert out["ok"] is False and out["sources_off_count"] == 2
    assert out["sources_off"] == [{"clip": 0, "timeline": ["b.mov", 1.0], "plan": ["a.mov", 1.0]},
                                  {"clip": 2, "timeline": ["a.mov", 5.083], "plan": ["a.mov", 5.0]}]
    rows = _rows()
    rows[2][2] = 121                     # one frame is what a conformed source may differ by
    assert timeline_check.check(_plan(), rows, 24)["ok"] is True
    rows[2][3] = ""                      # a clip without a media file is not the plan's clip
    assert timeline_check.check(_plan(), rows, 24)["sources_off"][0]["clip"] == 2


def test_the_same_file_in_another_unicode_form_is_the_same_file():
    plan = _plan()
    plan.clips[1].source_path = unicodedata.normalize("NFC", "/footage/gün.mov")
    rows = _rows()
    rows[1][3] = unicodedata.normalize("NFD", "/footage/gün.mov")
    assert rows[1][3] != plan.clips[1].source_path
    assert timeline_check.check(plan, rows, 24)["ok"] is True


def test_turns_must_have_arrived():
    plan = _plan(source_turns={"/footage/a.mov": 270, "/footage/b.mov": 180})
    rows = _rows()
    out = timeline_check.check(plan, rows, 24)
    assert out["ok"] is False and out["turns_off_count"] == 3
    assert out["turns_off"][0] == {"clip": 0, "timeline": [0, 1, 1], "plan": [90, 1.7778]}
    assert out["turns_off"][1] == {"clip": 1, "timeline": [0, 1, 1], "plan": [180, 1.0]}
    for row, (rotation, zoom) in zip(rows, [(90, 1.7778), (180, 1), (90, 1.7778)]):
        row[4:] = [rotation, zoom, zoom]
    assert timeline_check.check(plan, rows, 24)["ok"] is True
    rows[2][5] = 1.0                     # turned but not refitted
    assert timeline_check.check(plan, rows, 24)["turns_off"] == [
        {"clip": 2, "timeline": [90, 1.0, 1.7778], "plan": [90, 1.7778]}]
    rows[2][5] = 1.7778                  # and clips turned on a plan that turns nothing
    assert timeline_check.check(_plan(), rows, 24)["turns_off_count"] == 3


def test_another_frame_rate_or_clip_count_fails_without_guessing():
    out = timeline_check.check(_plan(), _rows(), 25)
    assert out["ok"] is False and out["frame_rate"] == {"plan": 24.0, "timeline": 25.0}
    assert timeline_check.check(_plan(), _rows(), None)["ok"] is True     # not reported: the plan's is assumed
    # Even when every clip happens to sit on the plan's frame numbers, another rate is another timeline.
    one = RoughCutPlan(title="One", fps=24.0, clips=[RoughCutClip("/footage/a.mov", in_seconds=0.0, duration_seconds=2.0)])
    same_numbers = timeline_check.check(one, [[0, 48, 0, "/footage/a.mov", 0, 1, 1]], 25)
    assert same_numbers["ok"] is False and list(same_numbers) == ["ok", "clips_in_plan", "clips_on_timeline", "frame_rate"]
    short = timeline_check.check(_plan(), _rows()[:2], 24)
    assert short == {"ok": False, "clips_in_plan": 3, "clips_on_timeline": 2,
                     "note": "The clip counts differ, so the clips were not compared one by one."}
    assert timeline_check.check(_plan(), None, 24)["clips_on_timeline"] == 0


def test_long_lists_are_cut_but_counted():
    plan = RoughCutPlan(title="Cut", fps=24.0, clips=[
        RoughCutClip("/footage/a.mov", in_seconds=float(i), duration_seconds=1.0) for i in range(30)])
    rows = [[24 * i + 1, 24 * i + 25, 24 * i, "/footage/a.mov", 0, 1, 1] for i in range(30)]
    out = timeline_check.check(plan, rows, 24)
    assert out["positions_off_count"] == 30 and len(out["positions_off"]) == 10


# ── Through the tool, against the mock that reads the exporter's file ────────

@pytest.fixture
def no_studio(monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))


def _exported(media, tmp_path, turn=True):
    from resolve_pilot import mcp_server
    from resolve_pilot.editorial import ops
    clip = str(media["clip"])     # 6 s at 24 fps, 320x240
    pid = mcp_server.create_plan("Demo", [
        ops.ClipSpec(source_path=clip, in_seconds=0.0, duration_seconds=1.0),
        ops.ClipSpec(source_path=clip, in_seconds=4.0, duration_seconds=0.25),
        ops.ClipSpec(source_path=clip, in_seconds=2.0, duration_seconds=1.5)], fade_in_seconds=0.5)["plan_id"]
    if turn:
        mcp_server.edit_plan(pid, [ops.TurnOp(op="turn", source=0, degrees=270)])
    path = tmp_path / "cut.fcpxml"
    mcp_server.export_fcpxml(pid, out_path=str(path))
    return pid, path


@needs_ffmpeg
@needs_luajit
def test_import_with_a_plan_reads_the_timeline_back_and_passes(media, tmp_path, live_bridge, no_studio):
    from resolve_pilot import mcp_server
    pid, path = _exported(media, tmp_path)
    out = mcp_server.import_timeline(str(path), timeline_name="A", plan_id=pid)
    assert out["ok"] is True and out["video_clip_count"] == 3
    assert out["plan_check"] == {"ok": True, "clips_in_plan": 3, "clips_on_timeline": 3}
    assert "clips" not in out and "fps" not in out          # the raw rows stay out of the reply
    plain = mcp_server.import_timeline(str(path), timeline_name="B")
    assert "plan_check" not in plain and "clips" not in plain


@needs_ffmpeg
@needs_luajit
def test_a_timeline_that_differs_from_its_plan_is_not_passed(media, tmp_path, live_bridge, no_studio):
    from resolve_pilot import mcp_server
    pid, path = _exported(media, tmp_path)
    text = path.read_text()
    # The second clip one frame late and one short, and the last clip's turn lost: what an
    # import that does not follow the file would look like.
    assert text.count('offset="36/24s" start="96/24s" duration="6/24s"') == 1
    broken = text.replace('offset="36/24s" start="96/24s" duration="6/24s"', 'offset="37/24s" start="99/24s" duration="5/24s"')
    last = [m for m in re.finditer(r"<adjust-transform [^>]*/>", broken)][-1]
    broken = broken[:last.start()] + broken[last.end():]
    path.write_text(broken)
    check = mcp_server.import_timeline(str(path), timeline_name="C", plan_id=pid)["plan_check"]
    assert check["ok"] is False
    assert check["positions_off"] == [{"clip": 1, "timeline": [37, 42], "plan": [36, 42]}]
    assert check["sources_off"] == [{"clip": 1, "timeline": ["two scenes.mp4", 4.125], "plan": ["two scenes.mp4", 4.0]}]
    assert check["turns_off"] == [{"clip": 2, "timeline": [0, 1, 1], "plan": [90, 0.75]}]


@needs_ffmpeg
@needs_luajit
def test_a_wrong_plan_id_stops_before_anything_is_imported(media, tmp_path, live_bridge, no_studio):
    from resolve_pilot import mcp_server
    _, path = _exported(media, tmp_path, turn=False)
    before = bridge.exec_lua("return #mock.project.timelines").result
    with pytest.raises(Exception, match="nope"):
        mcp_server.import_timeline(str(path), plan_id="nope")
    assert bridge.exec_lua("return #mock.project.timelines").result == before


@needs_ffmpeg
def test_without_a_live_channel_the_check_is_said_to_be_missing(media, tmp_path):
    from resolve_pilot import mcp_server
    pid, path = _exported(media, tmp_path, turn=False)
    out = mcp_server.import_timeline(str(path), plan_id=pid, channel="manual")
    assert out["plan_check"] is None and "cannot be compared with the plan" in out["note"]
    assert "lua_code" in out
