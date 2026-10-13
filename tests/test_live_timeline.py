"""Live timeline building: import_media, append_clips, remove_clips.

The real snippets run inside the real bridge (LuaJIT) against tests/lua/mock_resolve.lua,
whose media pool models the measured quirks: ImportMedia takes plain path strings,
AppendToTimeline's endFrame is exclusive and counts source frames at the source's fps.
The Studio twins run against a small Python fake and must answer in the same shape.
"""
from __future__ import annotations

import pytest

from resolve_pilot import bridge, control, mcp_server
from resolve_pilot.lua_snippets import library as lua
from resolve_pilot.studio_api import actions
from tests.conftest import needs_luajit


@pytest.fixture
def no_studio(monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))


@pytest.fixture
def files(tmp_path):
    out = {}
    for name in ("a.mov", "b.mov", "music.wav", "c d [x].mov"):
        p = tmp_path / name
        p.write_bytes(b"x")
        out[name] = str(p.resolve())
    return out


def _mock_media(info: dict[str, dict]) -> None:
    rows = ", ".join(f"[{lua.lua_string(k)}] = {lua.to_lua(v)}" for k, v in info.items())
    bridge.exec_lua(f"mock.media_info = {{{rows}}}")


def test_clip_specs_are_validated():
    with pytest.raises(ValueError, match="exactly one"):
        lua.normalize_clips([{"in_seconds": 1}])
    with pytest.raises(ValueError, match="not both"):
        lua.normalize_clips([{"path": "/a", "out_seconds": 2, "duration_seconds": 1}])
    with pytest.raises(ValueError, match="after in_seconds"):
        lua.normalize_clips([{"path": "/a", "in_seconds": 3, "duration_seconds": 0}])
    row = lua.normalize_clips([{"path": "/a", "in_seconds": 1, "duration_seconds": 2, "media": "Video"}])[0]
    assert row == {"index": 0, "media": "video", "path": "/a", "in_seconds": 1.0, "out_seconds": 3.0}
    assert lua.normalize_bin(" B-roll / Day 1/ ") == "B-roll/Day 1"
    with pytest.raises(ValueError):
        lua.normalize_bin("//")
    with pytest.raises(ValueError, match="not both"):
        lua.snippet_append_clips([], timeline="A", new_timeline="B")
    with pytest.raises(ValueError, match="empty"):
        lua.snippet_append_clips([])


def test_tools_reject_missing_files_before_touching_resolve(tmp_path, no_studio):
    with pytest.raises(FileNotFoundError):
        mcp_server.import_media([str(tmp_path / "nope.mov")])
    with pytest.raises(FileNotFoundError):
        mcp_server.append_clips([mcp_server.ClipIn(source=str(tmp_path / "nope.mov"))])


def test_manual_channel_hands_back_lua(files, no_studio):
    out = mcp_server.append_clips([mcp_server.ClipIn(source=files["a.mov"], duration_seconds=2)],
                                  new_timeline="Cut 2")
    assert out["channel"] == "manual" and "AppendToTimeline" in out["lua_code"]


@needs_luajit
def test_import_media_into_a_new_bin_reuses_files_already_there(live_bridge, files, no_studio):
    out = mcp_server.import_media([files["a.mov"], files["c d [x].mov"]], bin="B-roll/Day 1")
    assert out["ok"] and out["channel"] == "bridge"
    assert out["bin"] == "Day 1" and out["bins_created"] == ["B-roll", "Day 1"]
    assert out["imported"] == 2 and out["reused"] == 0
    a = out["items"][0]
    assert a["name"] == "a.mov" and a["media_pool_id"] and a["already_in_pool"] is False
    assert a["in_bin"] is True and a["fps"] == 25 and a["frames"] == 250

    again = mcp_server.import_media([files["a.mov"], files["b.mov"]], bin="B-roll/Day 1")
    assert again["bins_created"] == [] and again["imported"] == 1 and again["reused"] == 1
    assert again["items"][0]["already_in_pool"] is True
    assert again["items"][0]["media_pool_id"] == a["media_pool_id"]
    seen = bridge.exec_lua("local mp = mock.project.pool return {imports = mock.project.imports, "
                           "current = mp:GetCurrentFolder():GetName(), bins = #mp:GetRootFolder().subs}").result
    assert seen == {"imports": 3, "current": "Master", "bins": 1}  # current bin restored, no duplicate bin


@needs_luajit
def test_import_media_reports_files_resolve_rejects(live_bridge, files, no_studio):
    # The file exists for the server but Resolve cannot read it.
    bridge.exec_lua("local orig = mock.project.pool.ImportMedia "
                    "mock.project.pool.ImportMedia = function(self, l) "
                    "  if l[1]:find('b.mov', 1, true) then return {} end return orig(self, l) end")
    out = mcp_server.import_media([files["a.mov"], files["b.mov"]])
    assert out["ok"] is False and out["failed"] == 1 and out["imported"] == 1
    assert out["items"][1]["ok"] is False and "imported nothing" in out["items"][1]["error"]


@needs_luajit
def test_append_converts_source_seconds_with_the_source_fps(live_bridge, files, no_studio):
    _mock_media({files["a.mov"]: {"fps": 50, "frames": 500}})
    clip = mcp_server.ClipIn
    out = mcp_server.append_clips([
        clip(source=files["a.mov"], in_seconds=1, duration_seconds=2),
        clip(source=files["b.mov"], in_seconds=0.5, out_seconds=1.5, media="video"),
    ])
    assert out["ok"] and out["appended"] == 2, out
    assert out["timeline"]["name"] == "Main" and out["timeline"]["created"] is False
    first = out["clips"][0]
    # 50 fps source: 1 s -> frame 50, 3 s -> frame 150 (exclusive).
    assert first["requested"]["source_start_frame"] == 50 and first["requested"]["source_end_frame"] == 150
    assert first["imported"] is True and "warnings" not in first
    tracks = sorted(i["track"] for i in first["items"])
    assert tracks == ["audio 1", "video 1"]
    video = next(i for i in first["items"] if i["track"] == "video 1")
    # Appended after A (2 s) and B (3 s) on V1; 2 s at 24 fps = 48 frames.
    assert video["start_seconds"] == 5 and video["duration_frames"] == 48 and video["source_in_frame"] == 50
    second = out["clips"][1]
    assert [i["track"] for i in second["items"]] == ["video 1"]  # video only
    assert second["requested"]["source_start_frame"] == 13 and second["items"][0]["duration_seconds"] == 1
    # Read back through get_resolve_state as the agent would.
    state = bridge.exec_lua(lua.snippet_get_state(include_clips=True)).result["current_timeline"]
    assert [c["name"] for c in state["clips"]] == ["A", "B", "a.mov", "b.mov"]


@needs_luajit
def test_append_to_a_new_timeline_on_v2_at_a_record_position(live_bridge, files, no_studio):
    clip = mcp_server.ClipIn
    out = mcp_server.append_clips(
        [clip(source=files["a.mov"], duration_seconds=1, track=2, media="video", record_seconds=3)],
        new_timeline="Cut 2", new_timeline_fps=25)
    assert out["ok"], out
    tl = out["timeline"]
    assert tl["name"] == "Cut 2" and tl["created"] is True and tl["fps"] == 25 and tl["fps_applied"] is True
    assert out["tracks_added"] == ["video 2"]
    item = out["clips"][0]["items"][0]
    assert item["track"] == "video 2" and item["start_seconds"] == 3 and item["duration_frames"] == 25
    current = bridge.exec_lua("return mock.project:GetCurrentTimeline():GetName()").result
    assert current == "Cut 2"  # made current even though the mock's CreateEmptyTimeline does not

    clash = mcp_server.append_clips(new_timeline="Cut 2")
    assert clash["ok"] is False and "already exists" in clash["error"]
    empty = mcp_server.append_clips(new_timeline="Empty")
    assert empty["ok"] and empty["appended"] == 0 and empty["timeline"]["created"] is True

    # By name, and by media pool id (no second import).
    pool_id = out["clips"][0]["source"]["media_pool_id"]
    more = mcp_server.append_clips([clip(media_pool_id=pool_id, duration_seconds=0.4, media="video")],
                                   timeline="Cut 2")
    assert more["ok"] and more["clips"][0]["items"][0]["start_seconds"] == 0  # V1 of Cut 2 was empty
    assert "imported" not in more["clips"][0]
    assert bridge.exec_lua("return mock.project.imports").result == 1


@needs_luajit
def test_append_reports_what_resolve_did_differently(live_bridge, files, no_studio):
    _mock_media({files["a.mov"]: {"fps": 24, "frames": 48}})
    # A Resolve that treats endFrame as inclusive: one frame longer than asked.
    bridge.exec_lua("local orig = mock.project.pool.AppendToTimeline "
                    "mock.project.pool.AppendToTimeline = function(self, infos) "
                    "  local out = orig(self, infos) for _, it in ipairs(out) do it.dur = it.dur + 1 end "
                    "  return out end")
    out = mcp_server.append_clips([mcp_server.ClipIn(source=files["a.mov"], in_seconds=1, out_seconds=9,
                                                     media="video")])
    row = out["clips"][0]
    assert out["ok"] and row["requested"]["source_end_frame"] == 48
    assert any("past the end of the source" in w for w in row["warnings"])
    assert any("landed with 25 frames, expected 24" in w for w in row["warnings"])


@needs_luajit
def test_append_failures_are_per_clip(live_bridge, files, no_studio):
    out = mcp_server.append_clips([
        mcp_server.ClipIn(media_pool_id="nope"),
        mcp_server.ClipIn(source=files["a.mov"], in_seconds=20, duration_seconds=1),  # past the end
        mcp_server.ClipIn(source=files["b.mov"], duration_seconds=1),
    ])
    assert out["ok"] is False and out["appended"] == 1 and out["failed"] == 2
    assert "No media pool item" in out["clips"][0]["error"]
    assert "empty range" in out["clips"][1]["error"]
    assert out["clips"][2]["ok"] is True
    missing = mcp_server.append_clips([mcp_server.ClipIn(source=files["a.mov"])], timeline="Nope")
    assert missing["ok"] is False and "No timeline named" in missing["error"]


@needs_luajit
def test_remove_clips_checks_names_and_ripples(live_bridge, no_studio):
    bridge.exec_lua("resolve.page = 'color'")
    stale = mcp_server.remove_clips([0], names=["B"])
    assert stale["ok"] is False and "Nothing was deleted" in stale["error"]
    with_gap = mcp_server.remove_clips([5])
    assert with_gap["ok"] is False and "does not exist" in with_gap["error"]

    out = mcp_server.remove_clips([0], names=["A"], ripple=True)
    assert out["ok"] and out["deleted"] == 1 and out["clips_after"] == 1
    assert out["removed"] == [{"index": 0, "name": "A", "start_seconds": 0, "end_seconds": 2}]
    assert out["timeline_duration_seconds"] == 3  # B moved to the start
    assert bridge.exec_lua("return resolve.page").result == "color"  # page restored


@needs_luajit
def test_remove_clips_reports_a_refused_delete(live_bridge, no_studio):
    bridge.exec_lua("mock.project:GetCurrentTimeline().DeleteClips = function() return false end")
    out = mcp_server.remove_clips([1])
    assert out["ok"] is False and out["deleted"] == 0 and "DeleteClips returned false" in out["error"]


@needs_luajit
@pytest.mark.parametrize("live_bridge", ["21.1"], indirect=True)
def test_live_timeline_in_the_21_1_sandbox(live_bridge, files, no_studio):
    imp = mcp_server.import_media([files["music.wav"]], bin="Music")
    assert imp["ok"] and imp["items"][0]["name"] == "music.wav"
    out = mcp_server.append_clips([mcp_server.ClipIn(media_pool_id=imp["items"][0]["media_pool_id"],
                                                     duration_seconds=2, media="audio", track=2)])
    assert out["ok"], out
    assert out["tracks_added"] == ["audio 2"] and out["clips"][0]["items"][0]["track"] == "audio 2"
    assert mcp_server.remove_clips([0], track_type="audio", track_index=2, names=["music.wav"])["ok"]


# ── Studio twins: same shapes against a Python fake ───────────────────────────


class _Mpi:
    def __init__(self, path, n):
        self.path, self.id = path, f"id-{n}"

    def GetName(self):
        return self.path.rsplit("/", 1)[-1]

    def GetUniqueId(self):
        return self.id

    def GetClipProperty(self, key):
        return {"File Path": self.path, "FPS": "25", "Frames": "250"}.get(key)


class _Folder:
    def __init__(self, name):
        self.name, self.clips, self.subs = name, [], []

    def GetName(self):
        return self.name

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return list(self.subs)


class _Item:
    def __init__(self, name, start, dur, kind, index, left=0):
        self.name, self.start, self.dur, self.kind, self.index, self.left = name, start, dur, kind, index, left

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.start + self.dur

    def GetLeftOffset(self):
        return self.left

    def GetTrackTypeAndIndex(self):
        return [self.kind, self.index]

    def GetMediaPoolItem(self):
        return object()


class _Timeline:
    def __init__(self, name):
        self.name, self.tracks = name, {"video": [[]], "audio": [[]]}

    def GetName(self):
        return self.name

    def GetStartFrame(self):
        return 86400

    def GetEndFrame(self):
        return max([86400] + [it.GetEnd() for t in self.tracks.values() for tr in t for it in tr])

    def GetSetting(self, key):
        return ""

    def SetSetting(self, key, value):
        return True

    def GetTrackCount(self, kind):
        return len(self.tracks[kind])

    def AddTrack(self, kind):
        self.tracks[kind].append([])
        return True

    def GetItemListInTrack(self, kind, index):
        return self.tracks[kind][index - 1]

    def DeleteClips(self, items, ripple):
        for tr in self.tracks["video"]:
            tr[:] = [it for it in tr if it not in items]
        return True


class _Pool:
    def __init__(self, project):
        self.project, self.root = project, _Folder("Master")
        self.current, self.n = self.root, 0

    def GetRootFolder(self):
        return self.root

    def GetCurrentFolder(self):
        return self.current

    def SetCurrentFolder(self, f):
        self.current = f
        return True

    def AddSubFolder(self, parent, name):
        parent.subs.append(_Folder(name))
        return parent.subs[-1]

    def ImportMedia(self, paths):
        self.n += 1
        mpi = _Mpi(paths[0], self.n)
        self.current.clips.append(mpi)
        return [mpi]

    def CreateEmptyTimeline(self, name):
        tl = _Timeline(name)
        self.project.timelines.append(tl)
        return tl

    def AppendToTimeline(self, infos):
        info = infos[0]
        tl, idx = self.project.current, info.get("trackIndex", 1)
        kinds = {1: ["video"], 2: ["audio"]}.get(info.get("mediaType"), ["video", "audio"])
        dur = round((info["endFrame"] - info["startFrame"]) / 25 * 24)
        out = []
        for k in kinds:
            track = tl.tracks[k][idx - 1]
            rec = info.get("recordFrame") or max([86400] + [it.GetEnd() for it in track])
            track.append(_Item(info["mediaPoolItem"].GetName(), rec, dur, k, idx, info["startFrame"]))
            out.append(track[-1])
        return out


class _Project:
    def __init__(self):
        self.timelines = [_Timeline("Main")]
        self.current = self.timelines[0]
        self.pool = _Pool(self)

    def GetMediaPool(self):
        return self.pool

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, i):
        return self.timelines[i - 1]

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, tl):
        self.current = tl
        return True

    def GetSetting(self, key):
        return "24"


def _shape(value):
    """Keys (recursively) without values: what both channels must agree on."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items() if k != "channel"}
    if isinstance(value, list):
        return [_shape(v) for v in value[:1]]
    return type(value).__name__ if isinstance(value, (bool, str)) else "num"


@needs_luajit
def test_studio_twins_answer_in_the_same_shape(live_bridge, files, no_studio, monkeypatch):
    clip = {"path": files["a.mov"], "in_seconds": 1, "duration_seconds": 2, "track": 2}
    rows = lua.normalize_clips([clip])
    lua_imp = mcp_server.import_media([files["b.mov"]], bin="Day")
    lua_app = bridge.exec_lua(lua.snippet_append_clips(rows, new_timeline="T")).result
    lua_rm = bridge.exec_lua(lua.snippet_remove_clips([0], "video", 2, ["a.mov"])).result

    project = _Project()

    class _Resolve:
        page = "edit"

        def GetCurrentPage(self):
            return self.page

        def OpenPage(self, p):
            self.page = p
            return True

    monkeypatch.setattr(actions, "_current_project", lambda: project)
    monkeypatch.setattr(actions, "_current_timeline", lambda: project.current)
    monkeypatch.setattr(actions, "get_resolve", lambda: _Resolve())
    st_imp = actions.import_media([files["b.mov"]], "Day")
    st_app = actions.append_clips(rows, new_timeline="T")
    st_rm = actions.remove_clips([0], "video", 2, ["a.mov"])
    assert st_app["ok"] and st_app["clips"][0]["items"][0]["track"] == "video 2"
    assert st_rm["ok"] and st_rm["deleted"] == 1
    assert _shape(st_imp) == _shape(lua_imp)
    assert _shape(st_app) == _shape(lua_app)
    assert _shape(st_rm) == _shape(lua_rm)
