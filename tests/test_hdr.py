"""Phone HDR (HLG) footage: recognised, converted with the built-in LUT in Resolve (only on
the clips that are HLG) and in the preview."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from resolve_pilot import control, luts
from resolve_pilot import lua_snippets as lua
from resolve_pilot.analyzers import media as media_probe
from resolve_pilot.bridge import client as bridge
from resolve_pilot.editorial.roughcut import RoughCutClip, RoughCutPlan
from resolve_pilot.exporters import preview
from resolve_pilot.studio_api import actions
from tests.conftest import FFMPEG, needs_ffmpeg, needs_luajit

GREY = (128, 128, 128)
SKY = (120, 150, 190)


@pytest.fixture(scope="module")
def footage(tmp_path_factory):
    """One second of a flat colour, three times: tagged HLG / BT.2020 as an iPhone tags its
    HDR clips, tagged PQ, and untagged. The pixels are the same; only the tags differ."""
    if not (FFMPEG and shutil.which("ffprobe")):
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("hdr")

    def clip(name, colour, transfer=None):
        path = d / name
        tags = f",setparams=color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc" if transfer else ""
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=0x%02x%02x%02x:s=160x90:r=25:d=1" % colour, "-vf", "format=yuv420p" + tags,
                        "-c:v", "libx264", "-qp", "0", str(path)], check=True)
        return str(path)

    return {"hlg": clip("phone hdr.mp4", GREY, "arib-std-b67"), "hlg_sky": clip("sky.mp4", SKY, "arib-std-b67"),
            "pq": clip("hdr10.mp4", GREY, "smpte2084"),
            "plain": clip("plain.mp4", GREY), "plain_sky": clip("plain sky.mp4", SKY)}


def _rgb(path) -> tuple[float, float, float]:
    """The middle frame's colour, 0-1."""
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", "0.4", "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return tuple(v / 255 for v in raw[:3])


def _tags(path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=color_space,color_transfer,color_primaries", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return out.strip().strip(",").split(",")


def _plan(source):
    return RoughCutPlan(title="Cut", fps=25.0, width=160, height=90,
                        clips=[RoughCutClip(source_path=source, in_seconds=0.0, duration_seconds=0.8)])


# ── Recognising it ───────────────────────────────────────────────────────────

def test_probe_says_which_kind_of_hdr_a_file_is(footage, tmp_path, monkeypatch):
    assert media_probe.probe_media(footage["hlg"]).hdr == "hlg"
    assert media_probe.probe_media(footage["pq"]).hdr == "pq"
    assert media_probe.probe_media(footage["plain"]).hdr is None
    assert media_probe.probe_media(footage["plain"]).to_dict()["hdr"] is None
    sound = tmp_path / "s.wav"   # no picture: nothing to ask
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=d=0.2", str(sound)], check=True)
    assert media_probe.probe_media(sound).hdr is None


def test_scan_folder_and_export_say_so(footage, tmp_path):
    from resolve_pilot import mcp_server
    from resolve_pilot.editorial import ops
    folder = tmp_path / "shoot"
    folder.mkdir()
    for key in ("hlg", "pq", "plain"):
        shutil.copy(footage[key], folder)
    rows = {r["path"].rsplit("/", 1)[-1]: r for r in mcp_server.scan_folder(str(folder))["files"]}
    assert rows["phone hdr.mp4"]["hdr"] == "hlg" and rows["hdr10.mp4"]["hdr"] == "pq"
    assert "hdr" not in rows["plain.mp4"]

    def export(*keys):
        pid = mcp_server.create_plan(title="t", fps=25, clips=[
            ops.ClipSpec(source_path=footage[k], in_seconds=0, duration_seconds=0.4) for k in keys])["plan_id"]
        return mcp_server.export_fcpxml(pid, out_path=str(tmp_path / f"{'-'.join(keys)}.fcpxml"))

    both = export("hlg", "hlg_sky", "plain")
    assert both["hdr_sources"] == {"hlg": 2}
    assert "2 source(s) are phone HDR (HLG)" in both["next_step"]
    assert "apply_lut(lut_path='hlg-to-rec709')" in both["next_step"]
    plain = export("plain")
    assert "hdr_sources" not in plain and "apply_lut" not in plain["next_step"]
    pq = export("pq")
    assert pq["hdr_sources"] == {"pq": 1} and "apply_lut" not in pq["next_step"]
    assert any("HDR10 (PQ)" in w and "no built-in conversion" in w for w in pq["warnings"])


# ── The conversion ───────────────────────────────────────────────────────────

def test_the_conversion_undoes_hlg_and_keeps_black_white_and_grey_neutral():
    assert luts.hlg_to_rec709(0, 0, 0) == (0.0, 0.0, 0.0)
    assert luts.hlg_to_rec709(1, 1, 1) == pytest.approx((1.0, 1.0, 1.0), abs=2e-3)
    # HLG 0.5 is 1/12 of the light of HLG 1; with the fitted power 0.40 that is 0.370.
    assert luts.hlg_to_rec709(0.5, 0.5, 0.5) == pytest.approx(((1 / 12) ** 0.40,) * 3, abs=1e-4)
    greys = [luts.hlg_to_rec709(v / 20, v / 20, v / 20) for v in range(21)]
    assert all(max(g) - min(g) < 2e-3 for g in greys)                        # no colour cast
    assert all(a[1] < b[1] for a, b in zip(greys, greys[1:]))               # brighter stays brighter
    # BT.2020 is the wider gamut: the same numbers mean a stronger colour in BT.709.
    r, g, b = luts.hlg_to_rec709(0.47, 0.59, 0.75)
    assert b - r > (0.75 - 0.47) * 1.2 and r < g < b
    # The fitted numbers themselves (power 0.40, saturation 1.14), on a sky blue and an orange.
    assert (r, g, b) == pytest.approx((0.2270, 0.4423, 0.6304), abs=1e-4)
    assert luts.hlg_to_rec709(0.8, 0.4, 0.2) == pytest.approx((0.8448, 0.1824, 0.0720), abs=1e-4)


def test_the_cube_file_is_what_a_lut_reader_expects():
    lines = luts.hlg_to_rec709_cube(size=5).splitlines()
    assert lines[0].startswith("TITLE ") and lines[1] == "LUT_3D_SIZE 5" and len(lines) == 2 + 5 ** 3
    rows = [tuple(float(x) for x in line.split()) for line in lines[2:]]
    assert rows[0] == (0.0, 0.0, 0.0) and rows[-1] == pytest.approx((1.0, 1.0, 1.0), abs=2e-3)
    assert rows[1] == pytest.approx(luts.hlg_to_rec709(0.25, 0, 0), abs=1e-6)    # red changes fastest
    assert rows[5] == pytest.approx(luts.hlg_to_rec709(0, 0.25, 0), abs=1e-6)
    assert rows[25] == pytest.approx(luts.hlg_to_rec709(0, 0, 0.25), abs=1e-6)
    assert all(0.0 <= v <= 1.0 for row in rows for v in row)


def test_the_built_in_lut_is_written_once_and_repaired(isolated_home):
    path = luts.builtin_lut("hlg-to-rec709")
    assert path == isolated_home / "luts" / "hlg-to-rec709.cube"
    assert path.read_text() == luts.hlg_to_rec709_cube() and "LUT_3D_SIZE 33" in path.read_text()
    written = path.stat().st_mtime_ns
    assert luts.builtin_lut("hlg-to-rec709") == path and path.stat().st_mtime_ns == written
    path.write_text("LUT_3D_SIZE 2\n")          # damaged or from an older version
    assert luts.builtin_lut("hlg-to-rec709").read_text() == luts.hlg_to_rec709_cube()
    with pytest.raises(ValueError, match="Unknown built-in LUT 'nope'; there is: hlg-to-rec709"):
        luts.builtin_lut("nope")


# ── In Resolve: only the clips that are HLG ──────────────────────────────────

@pytest.fixture
def no_studio(monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))


def _mark_hlg(index: int) -> None:
    bridge.exec_lua(f'mock.project:GetCurrentTimeline().tracks.video[1][{index + 1}].color_space = "Rec.2100 HLG"')


def _luts_on_track() -> list:
    return bridge.exec_lua("local out = {} for i, it in ipairs(mock.project:GetCurrentTimeline().tracks.video[1]) do "
                           "out[i] = it.lut and it.lut.path or false end return out").result


@needs_luajit
def test_the_built_in_lut_goes_only_on_hlg_clips(live_bridge, no_studio, tmp_path):
    from resolve_pilot import mcp_server
    _mark_hlg(1)
    out = mcp_server.apply_lut("hlg-to-rec709")
    staged = tmp_path / "resolve-luts" / "resolve-pilot" / "hlg-to-rec709.cube"
    assert out == {"ok": True, "applied": 1, "skipped": 1, "total": 2, "node_index": 1,
                   "channel": "bridge", "staged_lut": str(staged)}
    assert staged.read_text() == luts.hlg_to_rec709_cube()
    assert _luts_on_track() == [False, str(staged)]


@needs_luajit
def test_no_hlg_clip_is_said_not_passed_over(live_bridge, no_studio):
    from resolve_pilot import mcp_server
    out = mcp_server.apply_lut("hlg-to-rec709")
    assert (out["ok"], out["applied"], out["skipped"]) == (True, 0, 2)
    assert out["note"] == "No clip on video track 1 is tagged Rec.2100 HLG in Resolve, so nothing was changed."
    assert _luts_on_track() == [False, False]


@needs_luajit
def test_an_ordinary_lut_still_goes_on_every_clip(live_bridge, no_studio, tmp_path):
    from resolve_pilot import mcp_server
    _mark_hlg(0)
    lut = tmp_path / "warm.cube"
    lut.write_text("LUT_3D_SIZE 2\n")
    out = mcp_server.apply_lut(str(lut))
    assert (out["applied"], out["total"]) == (2, 2) and "skipped" not in out and "note" not in out


@needs_luajit
def test_a_rejected_lut_on_the_hlg_clips_is_an_error(live_bridge):
    _mark_hlg(0)
    bridge.exec_lua("for _, it in ipairs(mock.project:GetCurrentTimeline().tracks.video[1]) do "
                    "it.SetLUT = function() return false end end")
    res = bridge.exec_lua(lua.snippet_apply_lut_to_clips("/elsewhere/a.cube", only_color_space="Rec.2100 HLG")).result
    assert res["ok"] is False and (res["applied"], res["skipped"]) == (0, 1) and "LUT folder" in res["error"]


class _Pool:
    def __init__(self, space):
        self.space = space

    def GetClipProperty(self, key):
        return self.space if key == "Input Color Space" else None


class _Item:
    def __init__(self, space, accepts=True):
        self.pool, self.accepts, self.lut = _Pool(space), accepts, None

    def GetMediaPoolItem(self):
        return self.pool

    def GetProperty(self):
        return {"Opacity": 100.0}

    def SetLUT(self, node, path):
        if self.accepts:
            self.lut = (node, path)
        return self.accepts


class _Timeline:
    def __init__(self, items):
        self.items = items

    def GetItemListInTrack(self, kind, index):
        return self.items


def test_the_studio_path_filters_the_same_way(monkeypatch):
    items = [_Item("Project"), _Item("Rec.2100 HLG"), _Item("Rec.2100 HLG")]
    monkeypatch.setattr(actions, "_current_timeline", lambda: _Timeline(items))
    monkeypatch.setattr(actions, "_current_project", lambda: object())
    out = actions.apply_lut_report("/l/x.cube", 1, 2, "Rec.2100 HLG")
    assert out == {"ok": True, "applied": 2, "skipped": 1, "total": 3, "node_index": 2}
    assert [i.lut for i in items] == [None, (2, "/l/x.cube"), (2, "/l/x.cube")]
    assert actions.apply_lut_report("/l/y.cube") == {"ok": True, "applied": 3, "total": 3, "node_index": 1}
    none = [_Item("Project")]
    monkeypatch.setattr(actions, "_current_timeline", lambda: _Timeline(none))
    assert actions.apply_lut_report("/l/x.cube", only_color_space="Rec.2100 HLG")["ok"] is True
    refused = [_Item("Rec.2100 HLG", accepts=False)]
    monkeypatch.setattr(actions, "_current_timeline", lambda: _Timeline(refused))
    out = actions.apply_lut_report("/l/x.cube", only_color_space="Rec.2100 HLG")
    assert out["ok"] is False and "LUT folder" in out["error"]


# ── In the preview ───────────────────────────────────────────────────────────

@needs_ffmpeg
@pytest.mark.parametrize("key", ["hlg", "hlg_sky"])
def test_the_preview_shows_hlg_sources_converted_and_says_rec709(footage, tmp_path, key):
    before = _rgb(footage[key])                      # the code values, as an unmanaged project shows them
    out = preview.render(_plan(footage[key]), tmp_path / "p.mp4", height=90)
    assert out["hlg_sources_converted"] == 1 and out["not_shown"] == []
    assert _tags(out["path"]) == ["bt709", "bt709", "bt709"]
    assert _rgb(out["path"]) == pytest.approx(luts.hlg_to_rec709(*before), abs=0.02)
    assert max(abs(a - b) for a, b in zip(_rgb(out["path"]), before)) > 0.08   # it did change


@needs_ffmpeg
def test_other_sources_are_left_as_they_are(footage, tmp_path):
    for key in ("plain", "plain_sky", "pq"):
        out = preview.render(_plan(footage[key]), tmp_path / f"{key}.mp4", height=90)
        assert "hlg_sources_converted" not in out
        assert _rgb(out["path"]) == pytest.approx(_rgb(footage[key]), abs=0.02)
    assert out["not_shown"] == ["HDR10 (PQ) sources are not converted to Rec.709"]


@needs_ffmpeg
def test_an_ffmpeg_without_lut3d_is_said_not_hidden(footage, tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "converts_hlg", lambda ffmpeg: False)
    out = preview.render(_plan(footage["hlg"]), tmp_path / "p.mp4", height=90)
    assert out["not_shown"] == ["phone HDR (HLG) sources are not converted to Rec.709 (this ffmpeg has no lut3d filter)"]
    assert "hlg_sources_converted" not in out
    assert _rgb(out["path"]) == pytest.approx(_rgb(footage["hlg"]), abs=0.02)


@needs_ffmpeg
def test_the_lut_is_found_in_a_home_with_awkward_characters(footage, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVE_PILOT_HOME", str(tmp_path / "Emir's home, 50% [new]: x"))
    out = preview.render(_plan(footage["hlg"]), tmp_path / "p.mp4", height=90)
    assert out["hlg_sources_converted"] == 1
    assert _rgb(out["path"]) == pytest.approx(luts.hlg_to_rec709(*_rgb(footage["hlg"])), abs=0.02)


def test_ffmpeg_filter_lookup_reads_the_filter_list(monkeypatch):
    listing = " TS lut3d             V->V       Adjust colors using a 3D LUT.\n .. lut3dx   V->V  x\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, listing, ""))
    preview._has_filter.cache_clear()
    try:
        assert preview._has_filter("ffmpeg-a", "lut3d") is True
        assert preview._has_filter("ffmpeg-a", "lut3") is False
        assert preview._has_filter("ffmpeg-a", "V->V") is False
    finally:
        preview._has_filter.cache_clear()


@needs_ffmpeg
def test_a_change_in_how_the_preview_is_rendered_makes_it_render_again(footage, monkeypatch):
    from resolve_pilot import mcp_server
    from resolve_pilot.editorial import ops
    pid = mcp_server.create_plan(title="t", fps=25, clips=[
        ops.ClipSpec(source_path=footage["hlg"], in_seconds=0, duration_seconds=0.4)])["plan_id"]
    first = mcp_server.render_preview(pid, wait_seconds=120)
    assert first["status"] == "done" and first["cached"] is False and first["hlg_sources_converted"] == 1
    assert mcp_server.render_preview(pid, wait_seconds=120)["cached"] is True
    monkeypatch.setattr(preview, "RENDER_VERSION", preview.RENDER_VERSION + 1)
    assert mcp_server.render_preview(pid, wait_seconds=120)["cached"] is False
    assert json.loads(json.dumps(first))  # the result is plain data
