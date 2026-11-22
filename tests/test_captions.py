"""Captions that follow a rough cut: each line lands where its clip plays on the timeline."""
import re

import pytest

from resolve_pilot.editorial import captions
from resolve_pilot.editorial import ops as plan_ops
from resolve_pilot.editorial.roughcut import RoughCutClip, RoughCutPlan, plan_to_timeline_spec
from resolve_pilot.exporters.fcpxml import build_fcpxml
from resolve_pilot.transcribe.whisper import Segment, Word

SOURCE = "/media/talk.mov"


def _seg(text, *words):
    ws = [Word(a, b, t, 1.0) for a, b, t in words]
    return Segment(ws[0].start, ws[-1].end, text, ws)


TALK = [
    _seg("Bir iki üç.", (1.0, 1.4, "Bir"), (1.4, 1.8, "iki"), (1.8, 2.4, "üç.")),
    _seg("Dört beş altı yedi.", (5.0, 5.5, "Dört"), (5.5, 6.0, "beş"), (6.0, 6.5, "altı"), (6.5, 7.0, "yedi.")),
    _seg("Sekiz dokuz.", (10.0, 10.5, "Sekiz"), (10.5, 11.0, "dokuz.")),
]


def _plan(*ranges, source=SOURCE, fps=25.0, **kw):
    clips = [RoughCutClip(source_path=r[2] if len(r) > 2 else source, in_seconds=r[0], duration_seconds=r[1])
             for r in ranges]
    return RoughCutPlan(title="Cut", fps=fps, clips=clips, **kw)


def _cues(plan, transcripts=None, **kw):
    cues, report = captions.plan_captions(plan, {SOURCE: TALK} if transcripts is None else transcripts, **kw)
    return [(round(c.start, 3), round(c.end, 3), c.text) for c in cues], report


def test_captions_move_with_reordered_clips():
    # The end of the talk first, then its beginning.
    cues, report = _cues(_plan((9.8, 1.4), (0.8, 1.8)))
    assert cues == [(0.2, 1.2, "Sekiz dokuz."),       # said at 10.0 s in the file
                    (1.6, 3.2, "Bir iki üç.")]        # 1.4 s of clip one + 0.2 s into clip two
    assert report == {"clips": 2, "clips_without_transcript": [], "word_level": True}


def test_a_word_cut_out_of_the_clip_is_not_in_the_caption():
    # "beş" (5.5-6.0) is cut out: the clips end before and start after it.
    cues, _ = _cues(_plan((4.9, 0.6), (6.0, 1.12)))
    assert cues == [(0.1, 1.72, "Dört altı yedi.")]
    # A clip boundary inside a word: the word belongs to the clip that plays its middle.
    cues, _ = _cues(_plan((4.9, 0.9), (6.2, 0.9)))    # "beş" until 5.8 of 6.0; "altı" from 6.2 of 6.0-6.5
    assert [c[2] for c in cues] == ["Dört beş altı yedi."]
    cues, _ = _cues(_plan((4.9, 0.8), (6.3, 0.8)))    # less than half of either
    assert [c[2] for c in cues] == ["Dört yedi."]


def test_captions_sit_on_the_frames_the_timeline_is_exported_with():
    # Three clips of 1.016 s at 25 fps after a 1 s fade-in. The exporter rounds every clip
    # boundary to a frame (25, 50, 76, 101), so clip three starts at 3.04 s, not 3.032 s.
    plan = _plan((0.9, 1.016), (4.9, 1.016), (9.9, 1.016), fade_in_seconds=1.0)
    pieces, _ = captions.timeline_pieces(plan, {SOURCE: TALK})
    starts = {p.text: round(p.start, 3) for p in pieces}
    assert starts == {"Bir": 1.1, "iki": 1.5, "Dört": 2.1, "beş": 2.6, "Sekiz": 3.14, "dokuz.": 3.64}
    cues, _ = _cues(plan)                             # one run of speech across the cuts
    assert cues == [(1.1, 4.04, "Bir iki Dört beş Sekiz dokuz.")]   # held to the last frame (101)
    xml = build_fcpxml(plan_to_timeline_spec(plan))
    assert [int(n) for n in re.findall(r'<asset-clip [^>]*offset="(\d+)/25s"', xml)] == [25, 50, 76]


def test_whisper_words_without_a_length_go_with_their_neighbour():
    # Measured on a real vlog: two words stacked on the previous segment's end, 5 ms before
    # the cut. They are part of the kept line, so they are in its caption.
    retake = [_seg("Bu ne ya?", (94.34, 94.42, "Bu"), (94.42, 94.56, "ne"), (94.56, 94.58, "ya?")),
              _seg("Bu ne ya?", (94.58, 94.58, "Bu"), (94.58, 94.58, "ne"), (94.58, 94.8, "ya?"))]
    cues, _ = _cues(_plan((94.585, 0.47)), {SOURCE: retake})
    assert [c[2] for c in cues] == ["Bu ne ya?"]
    cues, _ = _cues(_plan((94.285, 0.3)), {SOURCE: retake})    # the first take only
    assert [c[2] for c in cues] == ["Bu ne ya?"]


def test_a_transcript_without_words_moves_whole_segments():
    plain = [Segment(s.start, s.end, s.text) for s in TALK]
    # Segment 1 (5.0-7.0) plays for 0.8 s in clip two: less than half, so it is left out.
    cues, report = _cues(_plan((0.8, 1.8), (5.0, 0.8)), {SOURCE: plain})
    assert cues == [(0.2, 1.6, "Bir iki üç.")] and report["word_level"] is False
    cues, _ = _cues(_plan((0.8, 1.8), (5.0, 1.2)), {SOURCE: plain})
    assert cues == [(0.2, 3.0, "Bir iki üç. Dört beş altı yedi.")]   # ends with its clip


def test_clips_without_a_transcript_are_reported():
    plan = _plan((0.8, 1.8), (0.0, 2.0, "/media/broll.mov"), (9.8, 1.4))
    cues, report = _cues(plan)
    assert report["clips_without_transcript"] == [1]
    assert [(c[0], c[2]) for c in cues] == [(0.2, "Bir iki üç."), (4.0, "Sekiz dokuz.")]
    cues, report = _cues(plan, {})
    assert cues == [] and report["clips_without_transcript"] == [0, 1, 2]


def _pieces(*items):
    return [captions.Piece(a, b, t) for a, b, t in items]


def test_cues_break_at_sentences_gaps_and_full_lines():
    said = _pieces((0.0, 0.3, "OK."), (0.3, 0.6, "Şimdi"), (0.6, 1.0, "başlıyoruz."),
                   (1.0, 1.4, "Hazır"), (1.4, 1.8, "mısınız?"),
                   (4.0, 4.3, "Evet"), (4.3, 4.6, "tamam"))
    cues = captions.cues_from_pieces(said, timeline_end=10.0)
    assert [c.text for c in cues] == ["OK. Şimdi başlıyoruz.",   # "OK." alone is not a cue
                                      "Hazır mısınız?", "Evet tamam"]
    assert [(c.start, round(c.end, 3)) for c in cues] == [(0.0, 1.0), (1.0, 1.824), (4.0, 4.7)]   # 14 chars / 17
    # No punctuation at all: lines fill up, and a silence of more than a second splits.
    run = _pieces(*[(i * 0.4, i * 0.4 + 0.4, f"kelime{i}") for i in range(12)],
                  (6.5, 6.9, "sonra"))
    cues = captions.cues_from_pieces(run, max_chars_per_line=20, max_lines=2, timeline_end=7.0)
    assert all(len(line) <= 20 for c in cues for line in c.text.split("\n"))
    assert all(len(c.text.split("\n")) <= 2 for c in cues)
    assert " ".join(c.text.replace("\n", " ") for c in cues) == " ".join(p.text for p in run)
    assert cues[-1].text == "sonra" and cues[-2].end <= 6.5
    assert all(a.end <= b.start for a, b in zip(cues, cues[1:]))


def test_short_cues_stay_readable_but_never_overlap():
    one = captions.cues_from_pieces(_pieces((2.0, 2.2, "Evet.")), timeline_end=10.0)
    assert (one[0].start, one[0].end) == (2.0, 2.7)             # 0.2 s of speech, shown 0.7 s
    close = captions.cues_from_pieces(_pieces((2.0, 2.2, "Peki, olur tamam."), (2.8, 3.4, "Devam.")),
                                      timeline_end=10.0)
    assert (close[0].start, close[0].end) == (2.0, 2.8)         # wants 1 s; the next line starts at 2.8
    held = captions.cues_from_pieces(_pieces((2.0, 3.0, "Peki, olur tamam."), (3.2, 4.0, "Devam.")),
                                     timeline_end=4.1)
    assert held[0].end == 3.2 and held[1].end == 4.1            # gaps of <= 0.3 s are closed
    # Whisper's times can overlap (segments without word timings do): the earlier cue gives way.
    over = captions.cues_from_pieces(_pieces((0.0, 2.0, "Uzun bir cümle söyledi."), (1.5, 3.0, "Sonra ikincisi.")),
                                     timeline_end=3.0)
    assert [(c.start, c.end) for c in over] == [(0.0, 1.5), (1.5, 3.0)]


def test_fast_slow_and_unpunctuated_speech_still_gives_readable_cues():
    # All four are shapes measured on real vlog footage.
    # A sentence said in 0.4 s with the next one right behind it shares its cue.
    fast = _pieces((9.36, 9.5, "Daha"), (9.5, 9.6, "ne"), (9.6, 9.76, "kadar."),
                   (9.76, 10.0, "10"), (10.0, 10.4, "dakika"), (10.4, 10.9, "çektim."), (10.9, 11.3, "Tamam."))
    cues = captions.cues_from_pieces(fast, timeline_end=20.0)
    assert [(c.start, c.text) for c in cues] == [(9.36, "Daha ne kadar. 10 dakika çektim."), (10.9, "Tamam.")]
    # Slow recitation, 1.5 s a word and no punctuation: no caption sits there for 20 s.
    slow = _pieces(*[(i * 1.5, i * 1.5 + 1.5, f"söz{i}") for i in range(12)])
    cues = captions.cues_from_pieces(slow, timeline_end=18.0)
    assert [c.text for c in cues] == ["söz0 söz1 söz2 söz3", "söz4 söz5 söz6 söz7", "söz8 söz9 söz10 söz11"]
    assert max(c.end - c.start for c in cues) <= captions.MAX_CUE_SECONDS
    # No punctuation: a pause (or a cut) ends the cue once it has a line's worth of text.
    run = _pieces((0.0, 0.5, "deniz"), (0.5, 1.0, "güzeldi"), (1.0, 1.5, "ben"), (1.5, 2.0, "zevk"),
                  (2.4, 2.9, "aldım"), (2.9, 3.4, "duş"), (3.8, 4.3, "almış"), (4.3, 4.8, "yaklaşık"))
    cues = captions.cues_from_pieces(run, max_chars_per_line=20, timeline_end=5.0)
    # Break at the pause after "zevk" (22 characters by then), not at the one after "duş" (9).
    assert [c.text for c in cues] == ["deniz güzeldi ben\nzevk", "aldım duş almış\nyaklaşık"]
    # Forty characters said in one second stay up long enough to read (17 characters a second).
    quick = captions.cues_from_pieces(_pieces((1.0, 2.0, "Kanka çorba değil, kahvaltı yapacaktık.")),
                                      timeline_end=30.0)
    assert quick[0].end == pytest.approx(1.0 + 39 / 17)


# ── through the tools ───────────────────────────────────────────────────────

def _read_srt(path):
    from resolve_pilot import mcp_server
    text = open(path, encoding="utf-8").read()
    lines = [" ".join(block.split("\n")[2:]) for block in text.strip().split("\n\n")]
    return [(a, b, line) for (a, b), line in zip(mcp_server._srt_times(path), lines)]


def test_srt_for_a_plan_follows_the_cut_and_later_edits(talk):
    from resolve_pilot import mcp_server
    path, _, _ = talk
    tid = mcp_server.transcribe_audio(str(path), language="tr")["transcript_id"]
    # The goodbye first, then the greeting.
    plan = mcp_server.plan_from_transcript(transcript_id=tid, segment_indices=[3, 0])
    pid = plan["plan_id"]
    bye, hello = mcp_server.get_plan(pid)["clips"]
    out = mcp_server.export_srt(plan_id=pid)
    assert out["path"] == str(path.parent / "Transcript_Roughcut.srt")
    assert out["timed_to_plan"] == pid and out["warnings"] == []
    cues = _read_srt(out["path"])
    assert [c[2] for c in cues] == ["Hepsi bu kadar, görüşmek üzere.",
                                    "Merhaba arkadaşlar, bugün size yeni projemi anlatacağım."]
    # "Hepsi" is said at 14.80 s in the file; on the timeline it is that far into clip one.
    # Whisper hears "Hepsi" from 14.80 s, the sound starts at 15.23 s and the cut is just
    # before it: the caption starts with its clip, never before it.
    assert bye["in_seconds"] > 14.80 and cues[0][0] == 0.0
    second = round(bye["duration_seconds"] * 25) / 25           # clip two starts on a frame
    assert hello["in_seconds"] == 0.0 and cues[1][0] == pytest.approx(second, abs=0.002)
    assert all(b <= out["timeline_seconds"] + 0.001 for _, b, _ in cues)
    # The transcript's own times are what export_srt writes without the plan.
    plain = _read_srt(mcp_server.export_srt(transcript_id=tid)["path"])
    assert plain[0][2].startswith("Merhaba") and plain[-1][0] == pytest.approx(14.80)

    mcp_server.edit_plan(pid, [plan_ops.MoveOp(op="move", index=1, to=0)])
    moved = _read_srt(mcp_server.export_srt(plan_id=pid)["path"])
    assert [c[2][:7] for c in moved] == ["Merhaba", "Hepsi b"] and moved[0][0] == pytest.approx(0.0, abs=0.002)
    mcp_server.edit_plan(pid, [plan_ops.RemoveOp(op="remove", indices=[0])])
    assert [c[2][:7] for c in _read_srt(mcp_server.export_srt(plan_id=pid)["path"])] == ["Hepsi b"]


def test_srt_for_a_plan_leaves_out_dropped_words(talk):
    from resolve_pilot import mcp_server
    path, _, _ = talk
    tid = mcp_server.transcribe_audio(str(path), language="tr")["transcript_id"]
    plan = mcp_server.plan_from_transcript(transcript_id=tid, segment_indices=[2], drop_words=["2.0-1", "2.6"])
    cues = _read_srt(mcp_server.export_srt(plan_id=plan["plan_id"], out_path=str(path.parent / "cut.srt"))["path"])
    assert " ".join(c[2] for c in cues) == "önce kurulumu gösterelim ve sonra ilk videoyu keselim."
    assert cues[0][0] < 0.5 and all(a[1] <= b[0] for a, b in zip(cues, cues[1:]))


def test_plans_that_do_not_know_their_transcript_take_transcript_ids(talk, tmp_path):
    from resolve_pilot import mcp_server
    path, _, _ = talk
    broll = tmp_path / "broll.mp4"
    broll.write_bytes(b"x")
    tid = mcp_server.transcribe_audio(str(path), language="tr")["transcript_id"]
    made = mcp_server.create_plan("Mixed", [
        plan_ops.ClipSpec(source_path=str(broll), in_seconds=0, duration_seconds=2),
        plan_ops.ClipSpec(source_path=str(path), in_seconds=14.5, duration_seconds=3.0)], fps=25)
    pid = made["plan_id"]
    with pytest.raises(ValueError, match="No clip of this plan has a transcript"):
        mcp_server.export_srt(plan_id=pid)
    out = mcp_server.export_srt(plan_id=pid, transcript_ids=[tid])
    assert out["clips_without_transcript"] == [0] and "broll.mp4" in out["warnings"][0]
    assert _read_srt(out["path"]) == [(2.3, 5.0, "Hepsi bu kadar, görüşmek üzere.")]
    # transcript_id (one) is accepted as well; cues and inline segments are not mixed in.
    assert mcp_server.export_srt(plan_id=pid, transcript_id=tid)["cue_count"] == 1
    with pytest.raises(ValueError, match="goes with plan_id"):
        mcp_server.export_srt(transcript_ids=[tid])
    with pytest.raises(ValueError, match="not inline segments"):
        mcp_server.export_srt(plan_id=pid, segments=[mcp_server.SegmentIn(start=0, end=1, text="x")])


def test_plan_captions_say_what_they_could_not_do(talk):
    from resolve_pilot import mcp_server
    from resolve_pilot.transcribe import store
    path, _, _ = talk
    plain = mcp_server.transcribe_audio(str(path), language="tr", word_timestamps=False)["transcript_id"]
    pid = mcp_server.plan_from_transcript(transcript_id=plain, segment_indices=[3, 0])["plan_id"]
    out = mcp_server.export_srt(plan_id=pid)
    assert out["cue_count"] == 2 and "no word timings" in out["warnings"][0]
    # The transcript the plan was cut from is gone (cache cleared): say so, write nothing.
    (store.transcripts_dir() / f"{plain}.json").rename(store.transcripts_dir() / "gone.bak")
    with pytest.raises(ValueError, match="no longer stored"):
        mcp_server.export_srt(plan_id=pid, out_path=str(path.parent / "never.srt"))
    assert not (path.parent / "never.srt").exists()


def test_add_subtitles_for_a_plan_writes_the_cut_captions(talk):
    from resolve_pilot import mcp_server
    path, _, _ = talk
    tid = mcp_server.transcribe_audio(str(path), language="tr")["transcript_id"]
    pid = mcp_server.plan_from_transcript(transcript_id=tid, segment_indices=[3, 0])["plan_id"]
    out = mcp_server.add_subtitles(plan_id=pid, channel="manual")
    assert out["channel"] == "manual" and out["srt_path"] in out["lua_code"]
    assert [c[2][:5] for c in _read_srt(out["srt_path"])] == ["Hepsi", "Merha"]
    with pytest.raises(ValueError, match="not both"):
        mcp_server.add_subtitles(srt_path=out["srt_path"], plan_id=pid, channel="manual")
