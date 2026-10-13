"""Frames and contact sheets: the tools that let an agent look at footage."""
from __future__ import annotations

import base64
import json
import subprocess

import pytest

from resolve_pilot import mcp_server
from resolve_pilot.analyzers import frames
from tests.conftest import FFMPEG, needs_ffmpeg


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_frame_cmd_seeks_on_input_and_writes_one_frame(monkeypatch):
    monkeypatch.setattr(frames.shutil, "which", lambda _: "/ffmpeg")
    cmd = frames._frame_cmd("in.mov", 1.5, "out.jpg", 480)
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert "scale=480:-2" in cmd


def test_sheet_cmd_pins_one_output_frame(monkeypatch):
    monkeypatch.setattr(frames.shutil, "which", lambda _: "/ffmpeg")
    cmd = frames._sheet_cmd("frame_%03d.jpg", "sheet.jpg", 3, 2)
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert any(a.startswith("tile=3x2") for a in cmd)


def test_sample_points_stay_inside_each_scene():
    assert frames.sample_points([0.0, 1.0, 1.2, 9.8], 0.4, 10.0) == pytest.approx([0.4, 1.1, 1.6, 9.9])
    assert frames.sample_points([], 0.4, 10.0) == []


def test_pick_evenly_and_grid_shape():
    assert frames.pick_evenly(3, 10) == [0, 1, 2]
    assert frames.pick_evenly(100, 5) == [0, 25, 50, 74, 99]
    assert frames.pick_evenly(5, 1) == [0]
    assert frames.grid_shape(3) == (3, 1)
    assert frames.grid_shape(12) == (4, 3)
    assert frames.grid_shape(7) == (3, 3)  # 7x1 would be a strip; leave two cells empty
    with pytest.raises(ValueError):
        frames.grid_shape(0)


def test_missing_ffmpeg_is_explained(monkeypatch, tmp_path):
    monkeypatch.setattr(frames.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        frames.extract_frames(tmp_path / "x.mov", [1.0])


# ── Real ffmpeg on generated media ────────────────────────────────────────────

@pytest.fixture(scope="module")
def color_video(tmp_path_factory):
    """9 s: 3 s red, 3 s green, 3 s blue."""
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    target = tmp_path_factory.mktemp("frames") / "colors.mkv"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:s=160x90:d=3:r=25",
        "-f", "lavfi", "-i", "color=c=lime:s=160x90:d=3:r=25",
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=3:r=25",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
        "-map", "[out]", "-c:v", "ffv1", "-y", str(target),
    ], check=True)
    return target


def _blocks(result):
    header = json.loads(result.content[0].text)
    return header, result.content[1:]


def _dominant_channel(block, tmp_path) -> int:
    assert block.type == "image"
    img = tmp_path / "probe.jpg"
    img.write_bytes(base64.b64decode(block.data))
    raw = subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(img),
        "-vf", "scale=1:1", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], capture_output=True, check=True).stdout
    return max(range(3), key=lambda i: raw[i])


def _mime(block) -> str:
    return getattr(block, "mimeType", None) or getattr(block, "mime_type")


@needs_ffmpeg
def test_frames_come_back_labelled_and_frame_accurate(color_video, tmp_path):
    header, blocks = _blocks(mcp_server.grab_frames(str(color_video), [1.5, 4.5, 7.5]))
    assert header["ok"] is True and header["returned"] == 3 and header["dropped"] == []
    labels, images = blocks[0::2], blocks[1::2]
    assert [b.text for b in labels] == ["t=1.50s", "t=4.50s", "t=7.50s"]
    assert all(_mime(b) == "image/jpeg" for b in images)
    assert [_dominant_channel(b, tmp_path) for b in images] == [0, 1, 2]


@needs_ffmpeg
def test_out_of_range_and_over_cap_are_dropped_not_raised(color_video):
    header, blocks = _blocks(mcp_server.grab_frames(str(color_video), [1.5, 999, -1, 2.0], max_frames=3))
    assert header["returned"] == 1 and len(blocks) == 2
    reasons = {d["index"]: d["reason"] for d in header["dropped"]}
    assert "past the end" in reasons[1]
    assert "not a valid time" in reasons[2]
    assert "max_frames" in reasons[3]


@needs_ffmpeg
def test_nothing_extractable_is_a_readable_result(color_video):
    header, blocks = _blocks(mcp_server.grab_frames(str(color_video), [500.0]))
    assert header["ok"] is False and header["returned"] == 0 and blocks == []
    assert header["dropped"][0]["pts_seconds"] == 500.0


@needs_ffmpeg
def test_grid_is_one_contact_sheet_with_reading_order(color_video, tmp_path):
    header, blocks = _blocks(mcp_server.grab_frames(str(color_video), [1.5, 4.5, 7.5], layout="grid",
                                                    width=96))
    assert header["grid"] == {"cols": 3, "rows": 1}
    label, sheet = blocks
    assert "cell 1: t=1.50s" in label.text and "cell 3: t=7.50s" in label.text
    img = tmp_path / "sheet.jpg"
    img.write_bytes(base64.b64decode(sheet.data))
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width",
                            "-of", "csv=p=0", str(img)], capture_output=True, text=True, check=True)
    assert int(probe.stdout) >= 3 * 96


@needs_ffmpeg
def test_temp_frames_are_cleaned_up(color_video, monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server.tempfile, "tempdir", str(tmp_path))
    mcp_server.grab_frames(str(color_video), [1.0, 2.0], layout="grid")
    assert list(tmp_path.iterdir()) == []


@needs_ffmpeg
def test_scene_frames_shows_one_frame_inside_each_shot(color_video, tmp_path):
    header, blocks = _blocks(mcp_server.scene_frames(str(color_video), layout="separate"))
    assert header["scene_count"] == 3
    assert [s["start"] for s in header["scenes"]] == pytest.approx([0, 3, 6], abs=0.05)
    assert [b.text.split(":")[0] for b in blocks[0::2]] == ["scene 0", "scene 1", "scene 2"]
    assert [_dominant_channel(b, tmp_path) for b in blocks[1::2]] == [0, 1, 2]


@needs_ffmpeg
def test_scene_labels_stay_paired_when_a_frame_is_dropped(color_video, monkeypatch, tmp_path):
    real = frames.subprocess.run

    def flaky(cmd, **kw):  # the middle scene's frame fails to decode
        if "-ss" in cmd and cmd[cmd.index("-ss") + 1].startswith("3.4"):
            return subprocess.CompletedProcess(cmd, 1, "", "decode error")
        return real(cmd, **kw)

    monkeypatch.setattr(frames.subprocess, "run", flaky)
    header, blocks = _blocks(mcp_server.scene_frames(str(color_video), layout="separate"))
    assert header["shown_scenes"] == [0, 2]
    assert header["dropped"] == [{"scene": 1, "pts_seconds": pytest.approx(3.4, abs=0.05),
                                  "reason": "ffmpeg produced no frame: decode error"}]
    assert [b.text.split(":")[0] for b in blocks[0::2]] == ["scene 0", "scene 2"]
    assert [_dominant_channel(b, tmp_path) for b in blocks[1::2]] == [0, 2]


@needs_ffmpeg
def test_scene_frames_caps_by_spreading_over_the_video(color_video):
    header, blocks = _blocks(mcp_server.scene_frames(str(color_video), max_frames=2, layout="grid"))
    assert header["scene_count"] == 3 and header["shown_scenes"] == [0, 2]
    assert len(header["scenes"]) == 3


@needs_ffmpeg
def test_non_finite_times_keep_the_reply_valid_json(color_video):
    result = mcp_server.grab_frames(str(color_video), [1.0, float("nan"), float("inf")])
    header = json.loads(result.content[0].text, parse_constant=lambda c: pytest.fail(c))
    assert [d["pts_seconds"] for d in header["dropped"]] == ["nan", "inf"]
