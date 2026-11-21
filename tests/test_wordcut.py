"""Word-level cuts and the speech scan.

SPEECH is a real measurement, not invented numbers: a Turkish recording with known
pauses, transcribed by mlx-whisper large-v3-turbo with word timestamps, next to the
pauses ffmpeg's silencedetect found in the same audio. Whisper's word edges are off by
up to ~0.5 s around pauses, which is what these tests pin down. LEVELS is that
recording's level curve reduced to its essentials: voice outside the pauses, digital
silence inside them, in the 10 ms blocks the real curve comes in.
"""
import pytest

from resolve_pilot.editorial import roughcut, wordcut
from resolve_pilot.transcribe.whisper import Segment, Word
from tests.conftest import FFMPEG


def _seg(text, *words):
    ws = [Word(a, b, t, p) for a, b, t, p in words]
    return Segment(ws[0].start, ws[-1].end, text, ws)


SPEECH = [
    _seg("Merhaba arkadaşlar, bugün size yeni projemi anlatacağım.",
         (0.00, 0.50, "Merhaba", 1.0), (0.50, 1.30, "arkadaşlar,", 0.98), (1.70, 2.06, "bugün", 0.99),
         (2.06, 2.46, "size", 1.0), (2.46, 2.74, "yeni", 1.0), (2.74, 3.30, "projemi", 0.99),
         (3.30, 4.12, "anlatacağım.", 1.0)),
    _seg("Şey, ıh, önce kurulumu gösterelim.",
         (5.16, 5.86, "Şey,", 0.40), (6.16, 6.38, "ıh,", 0.47), (6.62, 6.96, "önce", 1.0),
         (6.96, 7.58, "kurulumu", 1.0), (7.58, 8.32, "gösterelim.", 1.0)),
    _seg("Önce kurulumu, önce kurulumu gösterelim ve sonra sonra ilk videoyu keselim.",
         (8.64, 9.32, "Önce", 1.0), (9.32, 9.88, "kurulumu,", 1.0), (10.00, 10.36, "önce", 1.0),
         (10.36, 11.00, "kurulumu", 1.0), (11.00, 11.74, "gösterelim", 1.0), (11.74, 11.90, "ve", 1.0),
         (11.90, 12.28, "sonra", 1.0), (12.28, 12.68, "sonra", 1.0), (12.68, 13.06, "ilk", 1.0),
         (13.06, 13.48, "videoyu", 1.0), (13.48, 14.04, "keselim.", 1.0)),
    _seg("Hepsi bu kadar, görüşmek üzere.",
         (14.80, 15.60, "Hepsi", 1.0), (15.60, 15.74, "bu", 1.0), (15.74, 16.08, "kadar,", 1.0),
         (16.50, 17.12, "görüşmek", 1.0), (17.12, 17.50, "üzere.", 1.0)),
]
# Where the sound really stops and starts (silencedetect on the same file).
PAUSES = [(1.62, 1.839), (4.211, 5.607), (5.984, 6.175), (6.511, 6.709), (8.377, 8.973),
          (9.937, 10.136), (14.138, 15.229), (16.342, 16.56)]
DURATION = 17.62
NEAR = 0.011  # one 10 ms block


def _levels(pauses, duration, voice=-20.0, silence=-90.0):
    def quiet(i):
        return any(a <= i * 0.01 + 1e-9 and (i + 1) * 0.01 <= b + 1e-9 for a, b in pauses)
    return [silence if quiet(i) else voice for i in range(round(duration * 100))]


LEVELS = _levels(PAUSES, DURATION)


def _in_a_pause(t):
    return any(a - 1e-6 <= t <= b + 1e-6 for a, b in PAUSES)


def test_cuts_between_segments_sit_in_the_real_pauses():
    spans = wordcut.cut_spans(SPEECH, [0, 1, 2, 3], LEVELS, duration=DURATION)
    assert [s.seg for s in spans] == [0, 1, 2, 3] and all(s.whole_segment for s in spans)
    inner = [spans[0].end] + [t for s in spans[1:3] for t in (s.start, s.end)] + [spans[3].start]
    assert all(_in_a_pause(t) for t in inner), inner
    # The file starts and ends in speech: nothing to cut into there, so not "tight".
    assert spans[0].start == 0.0 and spans[3].end == DURATION
    assert wordcut.cut_report(spans, True) == {"cuts": 8, "in_a_pause": 8, "tight": 0}


def test_a_word_whisper_ends_early_is_not_clipped():
    # Whisper ends "arkadaşlar," at 1.30 s; the sound goes on until 1.62 s. Cutting on
    # Whisper's times (1.30 + pad, capped halfway to the next word = 1.50) clips it.
    keep_two = wordcut.parse_drop_words(["0.2-6"], SPEECH)
    (span,) = wordcut.cut_spans(SPEECH, [0], LEVELS, keep_two)
    assert span.text == "Merhaba arkadaşlar," and (span.first, span.last) == (0, 1)
    assert span.end >= 1.62 and _in_a_pause(span.end) and span.end_in_pause
    (blind,) = wordcut.cut_spans(SPEECH, [0], None, keep_two)
    assert blind.end == pytest.approx(1.50) and not blind.end_in_pause


def test_a_word_whisper_starts_early_does_not_drag_the_pause_in():
    # Whisper starts "Şey," at 5.16 s, inside a pause that really lasts until 5.607 s.
    (span,) = wordcut.cut_spans(SPEECH, [1], LEVELS, pad_seconds=0.2)
    assert span.start == pytest.approx(5.607 - 0.2, abs=NEAR)
    (tight,) = wordcut.cut_spans(SPEECH, [1], LEVELS, pad_seconds=0.05)
    assert tight.start == pytest.approx(5.607 - 0.05, abs=NEAR)
    # A short pause is shared: neither side takes more than half of it.
    spans = wordcut.cut_spans(SPEECH, [2], LEVELS, wordcut.parse_drop_words(["2.2-10"], SPEECH), pad_seconds=0.5)
    assert spans[0].end == pytest.approx((9.937 + 10.136) / 2, abs=NEAR)


def test_dropped_words_split_the_clip_and_tight_cuts_are_reported():
    drop = wordcut.parse_drop_words(["1.0-1", "2.0-1", "2.6"], SPEECH)
    spans = wordcut.cut_spans(SPEECH, [1, 2], LEVELS, drop)
    assert [(s.seg, s.first, s.last, s.text) for s in spans] == [
        (1, 2, 4, "önce kurulumu gösterelim."),
        (2, 2, 5, "önce kurulumu gösterelim ve"),
        (2, 7, 10, "sonra ilk videoyu keselim."),
    ]
    # "ıh," is gone: the clip starts in the pause after it, not inside it.
    assert 6.511 <= spans[0].start <= 6.709
    # "ve | sonra sonra" is continuous speech: no pause for that cut to sit in.
    assert not spans[1].end_in_pause and not spans[2].start_in_pause
    # A flat stretch gives no quieter point, so the cut stays where Whisper puts the
    # boundary: never into the dropped "sonra".
    assert spans[1].end == pytest.approx(11.90, abs=NEAR)
    assert spans[2].start == pytest.approx(12.28, abs=NEAR)
    report = wordcut.cut_report(spans, True)
    assert report["tight"] == 2 and report["in_a_pause"] == 4
    assert [(t["clip"], t["sides"]) for t in report["tight_clips"]] == [(1, ["end"]), (2, ["start"])]
    assert "continuous speech" in report["note"]


def test_max_pause_shortens_long_pauses_inside_kept_speech():
    talk = [_seg("so today we build", (0.0, 0.3, "so", 1.0), (0.3, 0.8, "today", 1.0),
                 (3.0, 3.3, "we", 1.0), (3.3, 3.9, "build", 1.0))]
    levels = _levels([(0.9, 2.9)], 4.0)
    (whole,) = wordcut.cut_spans(talk, [0], levels)
    assert whole.end - whole.start > 3.5                                # the 2 s pause stays
    a, b = wordcut.cut_spans(talk, [0], levels, max_pause_seconds=0.4)
    assert (a.text, b.text) == ("so today", "we build")
    assert a.end == pytest.approx(0.9 + 0.2, abs=NEAR) and b.start == pytest.approx(2.9 - 0.2, abs=NEAR)
    assert len(wordcut.cut_spans(talk, [0], levels, max_pause_seconds=2.0)) == 1


def test_sound_between_far_apart_words_is_left_out():
    # Measured on vlog footage: 6 s of loud waves between two sentences is no pause by
    # level, and one shared cut point in it would keep all of it in the edit.
    talk = [_seg("hello there", (0.0, 0.4, "hello", 1.0), (0.4, 1.0, "there", 1.0)),
            _seg("moving on", (7.0, 7.4, "moving", 1.0), (7.4, 8.0, "on", 1.0))]
    waves = [-20.0] * 100 + [-18.0] * 600 + [-20.0] * 100
    a, b = wordcut.cut_spans(talk, [0, 1], waves, pad_seconds=0.2)
    assert a.end <= 1.0 + 0.5 and b.start >= 7.0 - 0.5
    assert not a.end_in_pause and not b.start_in_pause       # said: no quiet to cut in
    assert (b.end - b.start) + (a.end - a.start) < 3.0
    # With a breath of quiet next to each word, the cuts sit in it.
    quiet = [-20.0] * 100 + [-50.0] * 30 + [-18.0] * 540 + [-50.0] * 30 + [-20.0] * 100
    a, b = wordcut.cut_spans(talk, [0, 1], quiet, pad_seconds=0.2)
    assert a.end == pytest.approx(1.0 + 0.15, abs=NEAR) and b.start == pytest.approx(7.0 - 0.15, abs=NEAR)
    assert a.end_in_pause and b.start_in_pause
    assert wordcut.format_words(talk, quiet)[1] == "[1] 7.00-8.00 (6.0s) 0:moving 1:on"


def test_shallow_dips_of_noisy_speech_still_count_as_pauses():
    # Outdoors the gap between two words is only ~10 dB below them; a fixed silence
    # threshold finds nothing there (measured: silencedetect, 0 pauses).
    talk = [_seg("one two", (0.0, 0.5, "one", 1.0), (0.8, 1.3, "two", 1.0))]
    noisy = [-28.0] * 50 + [-38.0] * 30 + [-28.0] * 50
    keep_one = wordcut.parse_drop_words(["0.1"], talk)
    (span,) = wordcut.cut_spans(talk, [0], noisy, keep_one, pad_seconds=0.05)
    assert span.end == pytest.approx(0.55, abs=NEAR) and span.end_in_pause
    # A 60 ms dip is a consonant, not a pause: cut at its quietest point, reported tight.
    blip = [-28.0] * 50 + [-38.0] * 6 + [-28.0] * 74
    (span,) = wordcut.cut_spans(talk, [0], blip, keep_one, pad_seconds=0.05)
    assert 0.50 <= span.end <= 0.56 and not span.end_in_pause


def test_unmeasured_pauses_are_said_out_loud():
    spans = wordcut.cut_spans(SPEECH, [1, 2], None)
    report = wordcut.cut_report(spans, False)
    assert report["in_a_pause"] == 0 and "not measured" in report["note"]
    # Segment 1 runs straight into segment 2 (one shared point, nothing cut out):
    # that join is not a cut, so only the two outer ones are counted.
    assert spans[0].end == spans[1].start and report["cuts"] == 2 and report["tight"] == 2


def test_drop_words_are_validated():
    for spec, msg in (("x", "segment.word"), ("9.0", "segments 0-3"), ("1.7", "words 0-4"),
                      ("1.3-2", "words 0-4")):
        with pytest.raises(ValueError, match=msg):
            wordcut.parse_drop_words([spec], SPEECH)
    with pytest.raises(ValueError, match="no word timings"):
        wordcut.parse_drop_words(["0.0"], [Segment(0, 1, "plain")])
    assert wordcut.parse_drop_words(["1.0", " 2.6 - 7 "], SPEECH) == {(1, 0), (2, 6), (2, 7)}


def test_words_view_shows_addresses_and_pauses():
    lines = wordcut.format_words(SPEECH, LEVELS)
    assert lines[0].startswith("[0] 0.00-4.12 0:Merhaba 1:arkadaşlar, 2:bugün")
    assert lines[1] == "[1] 5.16-8.32 (1.4s) 0:Şey, 1:ıh, 2:önce 3:kurulumu 4:gösterelim."
    assert lines[3].startswith("[3] 14.80-17.50 (1.1s) 0:Hepsi")
    assert wordcut.format_words(SPEECH, LEVELS, offset=2, limit=1)[0].startswith("[2] ")
    assert "(no word timings)" in wordcut.format_words([Segment(0, 1, "plain")], None)[0]


def test_scan_finds_pauses_fillers_repeats_and_retakes():
    out = wordcut.scan(SPEECH, LEVELS, language="tr")
    assert [(p["after_word"], p["before_word"], p["seconds"]) for p in out["long_pauses"]] == [
        ("0.6", "1.0", 1.38), ("2.10", "3.0", 1.08)]
    assert out["long_pause_seconds_total"] == 2.5
    assert [(f["drop"], f["kind"], f.get("unsure")) for f in out["fillers"]] == [
        ("1.0", "discourse", True), ("1.1", "hesitation", True)]
    assert [(r["drop"], r["text"]) for r in out["repeats"]] == [
        (["2.0-1"], "Önce kurulumu,"), (["2.6"], "sonra")]
    assert [(r["earlier"], r["later"]) for r in out["retakes"]] == [(1, 2)]
    # Every suggestion can be applied as it is.
    drops = [f["drop"] for f in out["fillers"]] + [d for r in out["repeats"] for d in r["drop"]]
    spans = wordcut.cut_spans(SPEECH, [1, 2], LEVELS, wordcut.parse_drop_words(drops, SPEECH))
    assert " ".join(s.text for s in spans) == (
        "önce kurulumu gösterelim. önce kurulumu gösterelim ve sonra ilk videoyu keselim.")


def test_scan_in_english_and_with_the_speakers_own_fillers():
    talk = [_seg("Um so I I think, like, you know it works",
                 (0.0, 0.2, "Um", 0.9), (0.2, 0.4, "so", 1.0), (0.4, 0.5, "I", 1.0), (0.5, 0.6, "I", 1.0),
                 (0.6, 0.9, "think,", 1.0), (0.9, 1.1, "like,", 1.0), (1.1, 1.3, "y'know", 1.0),
                 (1.3, 1.5, "it", 1.0), (1.5, 1.9, "works", 1.0))]
    out = wordcut.scan(talk, [], language="en", extra_fillers=["y'know"])
    assert [(f["drop"], f["kind"]) for f in out["fillers"]] == [
        ("0.0", "hesitation"), ("0.5", "discourse"), ("0.6", "hesitation")]
    assert [r["drop"] for r in out["repeats"]] == [["0.2"]]
    assert out["long_pauses"] == [] and out["retakes"] == []
    plain = wordcut.scan([Segment(0, 1, "no words here")], None)
    assert plain["word_timings"] is False and plain["pauses_measured"] is False


def test_transcript_cut_builds_the_plan_and_the_report(monkeypatch):
    from resolve_pilot.analyzers import MediaInfo
    monkeypatch.setattr(roughcut, "probe_media", lambda p: MediaInfo(
        path=str(p), duration_seconds=DURATION, width=1920, height=1080, fps=25.0,
        video_codec="h264", audio_codec="aac", sample_rate=48000, channels=2, bit_rate=None,
        container="mov"))
    plan, report = roughcut.transcript_cut("/talk.mp4", SPEECH, segment_indices=[0, 2],
                                           levels=LEVELS, drop_words=["2.0-1", "2.6"])
    assert [c.rationale[:22] for c in plan.clips] == [
        "segment 0: Merhaba ark", "segment 2 words 2-5: ö", "segment 2 words 7-10: "]
    assert plan.notes == "Built from 2 of 4 transcript segments."
    assert plan.clips[0].in_seconds == 0.0 and report["tight"] == 2
    for a, b in zip(plan.clips, plan.clips[1:]):
        assert b.in_seconds >= a.out_seconds - 1e-9  # nothing is played twice


def test_words_from_both_engines():
    from types import SimpleNamespace
    from resolve_pilot.transcribe.whisper import _words
    mlx = [{"word": " Merhaba", "start": 0.0, "end": 0.5, "probability": 0.99},
           {"word": "  ", "start": 0.5, "end": 0.6}, {"word": " dünya", "start": 0.6, "end": 0.55}]
    assert _words(mlx) == [Word(0.0, 0.5, "Merhaba", 0.99), Word(0.6, 0.6, "dünya", None)]
    faster = [SimpleNamespace(word=" hi", start=1.0, end=1.2, probability=0.5)]
    assert _words(faster) == [Word(1.0, 1.2, "hi", 0.5)]
    assert _words(None) == []
    seg = Segment(0.0, 0.6, "Merhaba", [Word(0.0, 0.5, "Merhaba", 0.987)])
    assert seg.to_dict()["words"] == [{"start": 0.0, "end": 0.5, "text": "Merhaba", "p": 0.99}]
    assert "words" not in Segment(0.0, 1.0, "plain").to_dict()


@pytest.fixture
def talk(tmp_path, monkeypatch):
    """A transcribed talk as the tools see it: SPEECH from a stand-in Whisper, LEVELS
    from a stand-in ffmpeg. Returns (path, calls, state); state["levels"] can be set
    to an exception to make the measurement fail."""
    from resolve_pilot.analyzers import MediaInfo
    from resolve_pilot.transcribe import store
    from resolve_pilot.transcribe.whisper import TranscriptionResult
    calls, state = [], {"levels": LEVELS}

    def fake(path, model=None, language=None, word_timestamps=False, engine="auto"):
        calls.append(word_timestamps)
        segs = SPEECH if word_timestamps else [Segment(s.start, s.end, s.text) for s in SPEECH]
        return TranscriptionResult(language="tr", text="", segments=list(segs))

    def levels(path):
        if isinstance(state["levels"], Exception):
            raise state["levels"]
        return state["levels"]

    info = MediaInfo(path="", duration_seconds=DURATION, width=1920, height=1080, fps=25.0,
                     video_codec="h264", audio_codec="aac", sample_rate=48000, channels=2,
                     bit_rate=None, container="mov")
    monkeypatch.setattr(store, "transcribe", fake)
    monkeypatch.setattr(store, "resolve_engine", lambda engine="auto": "mlx")
    monkeypatch.setattr(store, "voice_levels", levels)
    monkeypatch.setattr(roughcut, "probe_media", lambda p: info)
    path = tmp_path / "talk.mp4"
    path.write_bytes(b"not really a video")
    return path, calls, state


def test_tools_from_transcript_to_a_cleaned_cut(talk):
    from resolve_pilot import mcp_server
    path, calls, _ = talk
    out = mcp_server.transcribe_audio(str(path), language="tr")
    assert calls == [True]                      # words are recorded by default
    assert out["word_timings"] is True and out["pauses_measured"] is True
    assert out["segments"][1] == "[1] 5.16-8.32 Şey, ıh, önce kurulumu gösterelim."
    tid = out["transcript_id"]
    words = mcp_server.transcribe_audio(str(path), language="tr", detail="words")
    assert words["cached"] and calls == [True]
    assert words["segments"][1] == "[1] 5.16-8.32 (1.4s) 0:Şey, 1:ıh, 2:önce 3:kurulumu 4:gösterelim."

    scan = mcp_server.scan_speech(tid)
    assert scan["transcript_id"] == tid and "note" not in scan
    assert [r["earlier"] for r in scan["retakes"]] == [1]
    drops = [d for r in scan["repeats"] for d in r["drop"]]
    assert drops == ["2.0-1", "2.6"]

    # Leave the false start (segment 1) out, drop the repeats inside segment 2.
    plan = mcp_server.plan_from_transcript(transcript_id=tid, segment_indices=[0, 2, 3], drop_words=drops)
    assert plan["clip_count"] == 4
    assert plan["cut_report"]["cuts"] == 8 and plan["cut_report"]["tight"] == 2
    stored = mcp_server.get_plan(plan["plan_id"], include_clips=True)["clips"]
    assert [c["rationale"].split(":")[0] for c in stored] == [
        "segment 0", "segment 2 words 2-5", "segment 2 words 7-10", "segment 3"]
    with pytest.raises(ValueError, match="words 0-10"):
        mcp_server.plan_from_transcript(transcript_id=tid, drop_words=["2.40"])


def test_tools_say_so_when_words_or_pauses_are_missing(talk):
    from resolve_pilot import mcp_server
    path, calls, state = talk
    state["levels"] = RuntimeError("ffmpeg is not installed")
    out = mcp_server.transcribe_audio(str(path), language="tr")
    assert out["pauses_measured"] is False and out["pauses_error"] == "ffmpeg is not installed"
    plan = mcp_server.plan_from_transcript(transcript_id=out["transcript_id"])
    assert plan["cut_report"]["in_a_pause"] == 2 and "not measured" in plan["cut_report"]["note"]
    assert "ffmpeg is not installed" in mcp_server.scan_speech(out["transcript_id"])["note"]

    plain = mcp_server.transcribe_audio(str(path), language="tr", word_timestamps=False)
    assert calls == [True, False] and plain["word_timings"] is False
    assert "word_timestamps=true" in mcp_server.scan_speech(plain["transcript_id"])["note"]
    with pytest.raises(ValueError, match="no word timings"):
        mcp_server.plan_from_transcript(transcript_id=plain["transcript_id"], drop_words=["1.0"])


def test_levels_survive_storage():
    from resolve_pilot.analyzers.speech import pack_levels, unpack_levels
    assert unpack_levels(pack_levels([-0.4, -20.2, -89.6, -140.0, 3.0])) == [-0.0, -20.0, -90.0, -140.0, -0.0]
    assert len(pack_levels([-30.0] * 360000)) < 500_000  # an hour of audio


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("noise", [0.0, 0.03])
def test_levels_measured_from_audio_show_the_gap_between_words(tmp_path, noise):
    import subprocess
    from resolve_pilot.analyzers.speech import voice_levels
    # A 440 Hz "voice" from 0-1 s and 1.6-3 s with nothing in between; with `noise`,
    # seeded room tone under all of it, about 15 dB below the voice.
    tone = "0.3*sin(2*PI*440*t)*(lt(t,1)+between(t,1.6,3))"
    wav = tmp_path / "talk.wav"
    filters = f"aevalsrc='{tone}':d=3:s=16000[a]"
    if noise:
        filters = (f"aevalsrc='{tone}':d=3:s=16000[t];"
                   f"anoisesrc=color=white:amplitude={noise}:d=3:r=16000:seed=7[n];"
                   "[t][n]amix=inputs=2:normalize=0[a]")
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-filter_complex", filters, "-map", "[a]",
                    "-ac", "1", str(wav)], check=True)
    levels = voice_levels(wav)
    assert 295 <= len(levels) <= 300
    voice, gap = sorted(levels[20:80])[30], sorted(levels[110:150])[20]
    assert -16 < voice < -12 and (gap < -60 if not noise else -40 < gap < voice - 10), (voice, gap)
    valley = wordcut.Sound(levels).gap(Word(0.3, 0.9, "one"), Word(1.5, 2.2, "two"))
    assert valley and abs(valley.start - 1.0) < 0.04 and abs(valley.end - 1.6) < 0.04, valley
    with pytest.raises(RuntimeError, match="could not read the audio"):
        voice_levels(tmp_path / "missing.wav")
