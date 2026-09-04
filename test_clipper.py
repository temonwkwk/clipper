"""Unit tests for clipper's pure helpers. Run: python -m pytest test_clipper.py -q"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from clipper import (  # noqa: E402
    Segment,
    _merge_small_groups,
    ass_time,
    build_parser,
    caption_geometry,
    cues_from_words,
    dedupe_rolling,
    fits_two_lines,
    fmt_time,
    parse_time,
    quantize,
    regroup_cues,
    slugify,
    snap_window,
    wrap_lines,
    wrap_two_lines,
    write_clip_ass,
)


# --- time helpers -----------------------------------------------------------
def test_parse_time_formats():
    assert parse_time(12.5) == 12.5
    assert parse_time("45") == 45
    assert parse_time("1:23") == 83
    assert parse_time("1:02:03") == 3723
    assert parse_time("0:00") == 0


def test_fmt_time_roundtrip():
    for seconds in (0, 59, 60, 3599, 3600, 3723):
        assert parse_time(fmt_time(seconds)) == seconds


def test_ass_time():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(83.45) == "0:01:23.45"
    assert ass_time(3723.5) == "1:02:03.50"


def test_quantize_matches_ass_time_resolution():
    """A quantized value must survive the ASS 10ms round-trip unchanged."""
    for raw in (0.0, 8.559, 1.234, 6.879, 59.999):
        q = quantize(raw)
        assert ass_time(q) == ass_time(quantize(q))
        assert abs(q - raw) <= 0.005 + 1e-9


def test_slugify():
    assert slugify("Hook: why brains WIN!") == "hook-why-brains-win"
    assert slugify("") == "clip"
    assert len(slugify("x" * 100, 40)) <= 40


# --- caption parsing --------------------------------------------------------
def test_dedupe_rolling_collapses_youtube_repeats():
    segs = [
        Segment(0.0, 2.0, "this is a"),
        Segment(1.5, 3.5, "this is a three"),
        Segment(3.5, 5.0, "it's sloppily written"),
        Segment(5.0, 6.0, "it's sloppily written"),
    ]
    out = dedupe_rolling(segs)
    assert [s.text for s in out] == ["this is a three", "it's sloppily written"]
    assert out[0].end == 3.5
    assert out[1].end == 6.0


# --- word wrapping ----------------------------------------------------------
def test_wrap_lines_never_drops_words():
    text = "one two three four five six seven eight nine ten"
    for width in (6, 10, 18, 40):
        assert " ".join(wrap_lines(text, width)).split() == text.split()


def test_wrap_two_lines_folds_overflow_instead_of_truncating():
    text = "one two three four five six seven eight"
    out = wrap_two_lines(text, 10)
    assert out.count(r"\N") == 1
    assert out.replace(r"\N", " ").split() == text.split(), "no word may vanish"


def test_wrap_two_lines_short_text_untouched():
    assert wrap_two_lines("short", 20) == "short"


def test_fits_two_lines():
    assert fits_two_lines("a b c", 10)
    assert fits_two_lines("aaaa bbbb cccc dddd", 10)
    assert not fits_two_lines("aaaa bbbb cccc dddd eeee ffff", 10)


def test_caption_geometry_scales_with_frame():
    small_line, small_cue = caption_geometry(360, 640)
    big_line, big_cue = caption_geometry(1080, 1920)
    assert small_cue == small_line * 2
    assert abs(small_line - big_line) <= 1  # same aspect -> same budget


# --- word-timed cues (the accurate path) ------------------------------------
WORDS = [
    (0.0, "[Music]"),
    (4.4, "This"), (4.799, "is"), (4.96, "a"), (5.2, "three."),
    (5.92, "It's"), (6.08, "sloppily"), (6.64, "written"), (6.879, "and"),
    (7.12, "rendered"), (7.44, "at"), (7.68, "an"), (7.839, "extremely"),
    (8.32, "low"), (8.559, "resolution"), (9.04, "of"), (9.36, "28x"),
    (10.0, "28"), (10.4, "pixels."),
]


def _cues(start=0.0, end=12.0, per_line=18):
    return cues_from_words(WORDS, start, end,
                           per_line=per_line, max_chars=per_line * 2)


def test_cues_from_words_drops_non_speech_annotations():
    text = " ".join(c.text for c in _cues())
    assert "[Music]" not in text
    assert "Music" not in text


def test_cues_from_words_every_shown_word_is_spoken_in_its_own_window():
    """The core sync contract: text on screen == words spoken during that cue."""
    cues = _cues()
    speech = [(quantize(t), w) for t, w in WORDS if not w.startswith("[")]
    for cue in cues:
        shown = cue.text.replace(r"\N", " ").split()
        spoken = [w for t, w in speech if cue.start <= t < cue.end]
        assert shown == spoken, f"{cue.start}-{cue.end}: {shown} != {spoken}"


def test_cues_from_words_loses_no_words_overall():
    shown = " ".join(c.text.replace(r"\N", " ") for c in _cues()).split()
    spoken = [w for _, w in WORDS if not w.startswith("[")]
    assert shown == spoken


def test_cues_from_words_are_ordered_and_non_overlapping():
    cues = _cues()
    assert cues
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start
        assert a.end > a.start


def test_cues_from_words_are_on_the_ass_grid():
    for cue in _cues():
        assert cue.start == quantize(cue.start)
        assert cue.end == quantize(cue.end)


def test_cues_from_words_respect_two_line_budget():
    per_line = 18
    for cue in _cues(per_line=per_line):
        lines = cue.text.split(r"\N")
        assert len(lines) <= 2
        for line in lines:
            assert len(line) <= per_line + 6


def test_cues_from_words_are_clip_relative_and_bounded():
    cues = cues_from_words(WORDS, 5.0, 9.0, per_line=18, max_chars=36)
    assert cues[0].start >= 0.0
    assert all(c.end <= 4.0 for c in cues)
    assert "This" not in " ".join(c.text for c in cues)   # spoken before window
    assert "28x" not in " ".join(c.text for c in cues)    # spoken after window


def test_cues_from_words_empty_window():
    assert cues_from_words(WORDS, 100.0, 110.0, per_line=18, max_chars=36) == []


def test_cues_from_words_breaks_after_sentence_end():
    cues = _cues()
    assert any("three." in c.text for c in cues)


# --- orphan-cue merging -----------------------------------------------------
SHORT_SENTENCES = [
    (0.0, "Respect"), (0.4, "nob."),
    (1.0, "Nonton"), (1.3, "apa"), (1.6, "ini"), (1.9, "ya"),
    (2.4, "lah."),
    (3.0, "Gila"),
    (3.6, "berat"), (3.9, "ya."),
]


def test_cues_from_words_has_no_one_word_orphans():
    """Short sentences must not each become their own flash-frame cue."""
    cues = cues_from_words(SHORT_SENTENCES, 0.0, 6.0,
                           per_line=18, max_chars=36)
    assert cues
    singles = [c.text for c in cues if len(c.text.replace(r"\N", " ").split()) == 1]
    assert not singles, f"orphan single-word cues: {singles}"


def test_merging_preserves_word_order_and_loses_nothing():
    cues = cues_from_words(SHORT_SENTENCES, 0.0, 6.0,
                           per_line=18, max_chars=36)
    shown = " ".join(c.text.replace(r"\N", " ") for c in cues).split()
    assert shown == [w for _, w in SHORT_SENTENCES]


def test_merging_keeps_cues_ordered_and_non_overlapping():
    cues = cues_from_words(SHORT_SENTENCES, 0.0, 6.0,
                           per_line=18, max_chars=36)
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start
        assert a.end > a.start


def test_merging_never_exceeds_the_two_line_budget():
    per_line = 18
    cues = cues_from_words(SHORT_SENTENCES, 0.0, 6.0,
                           per_line=per_line, max_chars=per_line * 2)
    for cue in cues:
        lines = cue.text.split(r"\N")
        assert len(lines) <= 2
        for line in lines:
            assert len(line) <= per_line + 6


def test_merge_small_groups_leaves_healthy_groups_alone():
    groups = [
        [(0.0, "satu"), (0.2, "dua"), (0.4, "tiga")],
        [(1.0, "empat"), (1.2, "lima"), (1.4, "enam")],
    ]
    out = _merge_small_groups([list(g) for g in groups],
                              per_line=18, max_chars=36, max_dur=2.6)
    assert len(out) == 2


def test_merge_small_groups_refuses_when_it_would_overflow():
    """An orphan next to an already-full group stays separate."""
    long_group = [(float(i) / 10, "xxxxx") for i in range(8)]
    groups = [long_group, [(2.0, "hi.")]]
    out = _merge_small_groups([list(g) for g in groups],
                              per_line=10, max_chars=20, max_dur=2.6)
    assert len(out) == 2, "must not merge past the line budget"


# --- clip window snapping ---------------------------------------------------
def test_snap_window_starts_on_a_sentence():
    start, end = snap_window(WORDS, 5.0, 10.6)
    assert start == 5.92, "should snap to 'It's', the start of a sentence"
    words_in = [w for t, w in WORDS if start <= t < end]
    assert words_in[0] == "It's"


def test_snap_window_ends_on_a_sentence_and_not_mid_next():
    start, end = snap_window(WORDS, 4.4, 10.5)
    words_in = [w for t, w in WORDS if start <= t < end]
    assert words_in[-1].endswith("."), f"clip ended on {words_in[-1]!r}"


def test_snap_window_does_not_swallow_the_next_word():
    """A tail pad must never reach the next spoken word."""
    words = [(0.0, "one."), (0.3, "two"), (0.6, "three.")]
    start, end = snap_window(words, 0.0, 0.1)
    assert end <= 0.3, "pad must stop before the next word starts"


def test_snap_window_respects_tolerance():
    start, end = snap_window(WORDS, 30.0, 40.0, tolerance=2.5)
    assert (start, end) == (30.0, 40.0), "no speech nearby -> unchanged"


def test_snap_window_no_words_is_identity():
    assert snap_window([], 3.0, 9.0) == (3.0, 9.0)


def test_snap_window_never_returns_a_degenerate_window():
    start, end = snap_window(WORDS, 5.0, 5.4)
    assert end > start


# --- segment fallback (no word timings) -------------------------------------
def test_regroup_cues_windows_and_zero_shifts():
    segs = [
        Segment(100.0, 102.0, "alpha"),
        Segment(102.0, 104.0, "beta"),
        Segment(200.0, 202.0, "outside window"),
    ]
    cues = regroup_cues(segs, 100.0, 105.0, max_chars=40, per_line=20)
    assert cues[0].start == 0.0
    assert all(c.end <= 5.0 for c in cues)
    assert "outside window" not in " ".join(c.text for c in cues)


def test_regroup_cues_never_overlaps():
    segs = [Segment(float(i), float(i) + 1.4, f"word{i}") for i in range(20)]
    cues = regroup_cues(segs, 0.0, 20.0, max_chars=24, per_line=12)
    assert cues
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start
        assert a.end > a.start


def test_regroup_cues_partial_overlap_is_clamped():
    segs = [Segment(5.0, 15.0, "spans the boundary")]
    cues = regroup_cues(segs, 10.0, 12.0, max_chars=40, per_line=20)
    assert cues[0].start == 0.0
    assert cues[0].end <= 2.0


def test_cut_defaults_to_no_captions():
    """Captions are opt-in: a bare `cut` must not burn text into the video."""
    args = build_parser().parse_args(["cut", "job", "clips.json"])
    assert args.captions is False


def test_cut_captions_flag_enables_them():
    args = build_parser().parse_args(["cut", "job", "clips.json", "--captions"])
    assert args.captions is True


def test_cut_no_captions_flag_still_accepted():
    """--no-captions kept for compatibility with existing scripts."""
    args = build_parser().parse_args(["cut", "job", "clips.json", "--no-captions"])
    assert args.captions is False


def test_cut_last_flag_wins():
    parser = build_parser()
    assert parser.parse_args(
        ["cut", "j", "c", "--captions", "--no-captions"]).captions is False
    assert parser.parse_args(
        ["cut", "j", "c", "--no-captions", "--captions"]).captions is True


# --- ASS output -------------------------------------------------------------
def test_write_clip_ass_uses_output_resolution(tmp_path):
    path = tmp_path / "c.ass"
    cues = [Segment(0.0, 1.5, r"hello\Nworld")]
    assert write_clip_ass(cues, path, width=540, height=960, font="DejaVu Sans")
    text = path.read_text()
    assert "PlayResX: 540" in text
    assert "PlayResY: 960" in text
    assert text.count("Dialogue:") == 1
    assert r"hello\Nworld" in text


def test_write_clip_ass_margin_clears_platform_ui(tmp_path):
    path = tmp_path / "c.ass"
    write_clip_ass([Segment(0.0, 1.0, "x")], path,
                   width=540, height=960, font="DejaVu Sans")
    style = next(l for l in path.read_text().splitlines()
                 if l.startswith("Style: Caption"))
    margin_v = int(style.split(",")[-2])
    assert 0.15 * 960 <= margin_v <= 0.25 * 960


def test_write_clip_ass_timestamps_match_cues(tmp_path):
    path = tmp_path / "c.ass"
    cues = [Segment(1.23, 2.5, "a"), Segment(2.5, 4.0, "b")]
    write_clip_ass(cues, path, width=540, height=960, font="X")
    rows = [l for l in path.read_text().splitlines() if l.startswith("Dialogue:")]
    assert rows[0].split(",")[1] == "0:00:01.23"
    assert rows[0].split(",")[2] == "0:00:02.50"
    assert rows[1].split(",")[1] == "0:00:02.50"


def test_write_clip_ass_empty_returns_false(tmp_path):
    assert write_clip_ass([], tmp_path / "e.ass", width=540, height=960, font="X") is False
