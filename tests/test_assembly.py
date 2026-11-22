"""One request, a folder of clips: intake of a folder, and one plan cut from several files."""
import os
import shutil
import subprocess
import threading

import pytest

from tests.conftest import FFMPEG
from tests.test_wordcut import SPEECH


def _stamped(src, dst, when):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c", "copy"]
    subprocess.run(cmd + (["-metadata", f"creation_time={when}"] if when else []) + [str(dst)], check=True)


@pytest.fixture
def shoot(media, tmp_path):
    """A folder as it comes off a phone: three clips whose names do not sort by time, a
    song, and things that are not footage."""
    d = tmp_path / "shoot"
    d.mkdir()
    _stamped(media["clip"], d / "IMG_0003.mp4", "2025-07-02T09:00:00.000000Z")
    _stamped(media["clip"], d / "IMG_0001.mp4", "2025-07-02T11:30:00.000000Z")
    _stamped(media["clip"], d / "IMG_0002.mp4", "2025-07-02T10:15:00.000000Z")
    shutil.copy(media["song"], d / "song.wav")
    (d / "notes.txt").write_text("not media")
    (d / ".hidden.mp4").write_bytes(b"x")
    (d / "broken.mov").write_bytes(b"this is not a movie")
    (d / "sub").mkdir()
    return d


def test_scan_folder_lists_the_footage_in_recording_order(shoot):
    from resolve_pilot import mcp_server
    # The song has no camera time, so the file times decide for all of them (mixing the two
    # orders would put the song first whenever it was copied).
    by_file_time = ["IMG_0001.mp4", "song.wav", "IMG_0002.mp4", "IMG_0003.mp4"]
    for n, name in enumerate(by_file_time):
        os.utime(shoot / name, (1_700_000_000 + n, 1_700_000_000 + n))
    out = mcp_server.scan_folder(str(shoot))
    assert [os.path.basename(f["path"]) for f in out["files"]] == by_file_time
    assert out["unreadable"] == ["broken.mov"] and out["file_count"] == 4 and out["total_seconds"] == 24.0
    clip = out["files"][3]
    assert clip == {"path": str(shoot / "IMG_0003.mp4"), "seconds": 6.0, "has_sound": True, "frame": "320x240",
                    "fps": 24.0, "recorded": "2025-07-02T09:00:00.000000Z"}
    assert out["files"][1] == {"path": str(shoot / "song.wav"), "seconds": 6.0, "has_sound": True}
    assert "transcripts" not in out

    # Every file says when it was shot: that order wins over names and file times.
    (shoot / "song.wav").rename(shoot.parent / "song.wav")
    out = mcp_server.scan_folder(str(shoot))
    assert [os.path.basename(f["path"]) for f in out["files"]] == ["IMG_0003.mp4", "IMG_0002.mp4", "IMG_0001.mp4"]
    for n, name in enumerate(["IMG_0001.mp4", "IMG_0002.mp4", "IMG_0003.mp4"]):
        os.utime(shoot / name, (1_700_000_000 + n, 1_700_000_000 + n))
    assert [os.path.basename(f["path"]) for f in mcp_server.scan_folder(str(shoot))["files"]] == [
        "IMG_0003.mp4", "IMG_0002.mp4", "IMG_0001.mp4"]
    with pytest.raises(FileNotFoundError, match="Not a folder"):
        mcp_server.scan_folder(str(shoot / "notes.txt"))


def test_scan_folder_transcribes_in_the_background_and_one_bad_file_does_not_hide_the_rest(shoot, monkeypatch):
    from resolve_pilot import mcp_server
    from resolve_pilot.transcribe import store
    from resolve_pilot.transcribe.whisper import TranscriptionResult
    go = threading.Event()

    def fake(path, model=None, language=None, word_timestamps=False, engine="auto"):
        assert go.wait(30)
        if path.name == "IMG_0002.mp4":
            raise RuntimeError("the audio could not be decoded")
        return TranscriptionResult(language=language or "tr", text="", segments=list(SPEECH[:2]))

    monkeypatch.setattr(store, "transcribe", fake)
    monkeypatch.setattr(store, "resolve_engine", lambda engine="auto": "mlx")
    monkeypatch.setattr(store, "voice_levels", lambda path: [-20.0] * 600)
    (shoot / "song.wav").rename(shoot.parent / "song.wav")

    first = mcp_server.scan_folder(str(shoot), transcribe=True, language="tr", wait_seconds=0)
    assert first["transcripts"] == {"done": 0, "running": 3, "failed": 0}
    assert "again" in first["next_step"] and first["files"][0]["transcript"] == {"status": "running"}
    go.set()
    out = mcp_server.scan_folder(str(shoot), transcribe=True, language="tr", wait_seconds=30)
    assert out["transcripts"] == {"done": 2, "running": 0, "failed": 1} and "plan_from_transcript" in out["next_step"]
    done, failed, _ = (f["transcript"] for f in out["files"])          # IMG_0003, IMG_0002, IMG_0001
    assert failed["status"] == "failed" and "could not be decoded" in failed["error"]
    assert done["status"] == "done" and done["language"] == "tr" and (done["segments"], done["words"]) == (2, 12)
    assert done["starts"].startswith("Merhaba arkadaşlar, bugün size yeni projemi anlatacağım. Şey,")
    # The transcript is the one transcribe_audio serves.
    again = mcp_server.transcribe_audio(str(shoot / "IMG_0003.mp4"), language="tr")
    assert again["cached"] is True and again["transcript_id"] == done["transcript_id"]


def test_a_plan_is_cut_from_several_files_and_captions_follow_all_of_them(talk, tmp_path):
    from resolve_pilot import mcp_server
    first, _, _ = talk
    second = tmp_path / "evening.mp4"
    second.write_bytes(b"another clip")
    tid1 = mcp_server.transcribe_audio(str(first), language="tr")["transcript_id"]
    tid2 = mcp_server.transcribe_audio(str(second), language="tr")["transcript_id"]
    assert tid1 != tid2

    made = mcp_server.plan_from_transcript(transcript_id=tid1, segment_indices=[0, 1], title="Gün")
    pid = made["plan_id"]
    # From the second file: the repeated line without its repeats, then the goodbye.
    more = mcp_server.plan_from_transcript(transcript_id=tid2, segment_indices=[2, 3], drop_words=["2.0-1", "2.6"],
                                           append_to=pid, title="ignored")
    assert more["plan_id"] == pid and more["title"] == "Gün" and more["clip_count"] == 5
    assert more["sources"] == [str(first), str(second)]
    assert more["appended"]["first_clip"] == 2 and more["appended"]["clips"] == 3
    assert [c["source"] for c in more["clips"]] == [0, 0, 1, 1, 1]
    # The cut report names the plan's clips, not the appended file's own 0, 1, 2.
    assert [t["clip"] for t in more["cut_report"]["tight_clips"]] == [2, 3]

    out = mcp_server.export_srt(plan_id=pid)
    assert out["warnings"] == [] and "clips_without_transcript" not in out
    text = " ".join(open(out["path"], encoding="utf-8").read().split())
    assert text.index("Merhaba arkadaşlar") < text.index("ilk videoyu") < text.index("Hepsi bu kadar")
    assert "Önce kurulumu," not in text
    with pytest.raises(ValueError, match="nope"):
        mcp_server.plan_from_transcript(transcript_id=tid2, append_to="nope")
