"""LUT staging into Resolve's LUT folder, and transition filtering in the Studio path."""
from __future__ import annotations

import pytest

from resolve_pilot import luts
from resolve_pilot.studio_api import actions


@pytest.fixture
def lut_dir(tmp_path, monkeypatch):
    folder = tmp_path / "LUT"
    folder.mkdir()
    monkeypatch.setenv("RESOLVE_PILOT_LUT_DIR", str(folder))
    return folder


def test_outside_lut_is_copied_into_the_lut_folder(lut_dir, tmp_path):
    src = tmp_path / "grades" / "warm.cube"
    src.parent.mkdir()
    src.write_text("LUT_3D_SIZE 2\n")
    path, copied = luts.stage_lut(src)
    assert copied and path == str(lut_dir / "resolve-pilot" / "warm.cube")
    assert (lut_dir / "resolve-pilot" / "warm.cube").read_text() == "LUT_3D_SIZE 2\n"
    assert luts.stage_lut(src) == (path, True)  # same content: reused, not duplicated


def test_lut_already_in_the_folder_is_used_in_place(lut_dir):
    inside = lut_dir / "Blackmagic Design" / "film.cube"
    inside.parent.mkdir()
    inside.write_text("x")
    assert luts.stage_lut(inside) == (str(inside.resolve()), False)


def test_same_name_different_content_gets_a_distinct_copy(lut_dir, tmp_path):
    a, b = tmp_path / "a" / "look.cube", tmp_path / "b" / "look.cube"
    for f, text in ((a, "one"), (b, "two")):
        f.parent.mkdir()
        f.write_text(text)
    first, _ = luts.stage_lut(a)
    second, _ = luts.stage_lut(b)
    assert first != second and second.endswith(".cube")
    assert open(first).read() == "one" and open(second).read() == "two"


def test_missing_lut_raises(lut_dir, tmp_path):
    with pytest.raises(FileNotFoundError):
        luts.stage_lut(tmp_path / "nope.cube")


def test_unwritable_lut_folder_explains_the_manual_fix(tmp_path, monkeypatch):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("")
    monkeypatch.setenv("RESOLVE_PILOT_LUT_DIR", str(blocker))
    src = tmp_path / "warm.cube"
    src.write_text("x")
    with pytest.raises(PermissionError, match="Open LUT Folder"):
        luts.stage_lut(src)


class _Item:
    def __init__(self, name, mpi=True, props=None):
        self.name, self.mpi = name, mpi
        self.props = {"Opacity": 100.0} if props is None else props

    def GetMediaPoolItem(self):
        return object() if self.mpi else None

    def GetProperty(self):
        return self.props


class _Timeline:
    def __init__(self, items):
        self.items = items

    def GetItemListInTrack(self, kind, index):
        return self.items


def test_studio_clip_items_drops_transitions_but_keeps_titles():
    clip, dissolve, title = _Item("a.mp4"), _Item("Cross Dissolve", mpi=False, props={}), \
        _Item("Text+", mpi=False)
    kept = actions.clip_items(_Timeline([clip, dissolve, title]), "video", 1)
    assert [i.name for i in kept] == ["a.mp4", "Text+"]
