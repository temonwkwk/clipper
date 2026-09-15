#!/usr/bin/env python3
"""
Clipper — turn one long video URL into several short vertical clips with
burned-in captions. Built for Termux/Android (CPU-only ffmpeg).

Pipeline
    1. fetch      yt-dlp pulls the source video + captions -> transcript.json
    2. transcript print the timestamped transcript so clip windows can be chosen
    3. cut        ffmpeg cuts each window, reframes to 9:16, burns captions

Usage
    python clipper.py fetch "<URL>" [--max-height 720] [--lang en] [--workdir DIR]
    python clipper.py transcript <job_dir> [--window 30]
    python clipper.py cut <job_dir> <clips.json> [--layout crop|blur|fit]
                                                [--height 1280] [--captions]

Captions are OFF by default; pass --captions to burn word-timed subtitles in.

clips.json shape
    [
      {"title": "hook-slug", "start": "3:12", "end": "3:58"},
      {"title": "second-clip", "start": 412.5, "end": 455}
    ]
Times accept seconds (int/float) or "M:SS" / "H:MM:SS".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

# Work lands beside the script, so a clone in any directory keeps its own jobs.
# (Hardcoding ~/clipper/work made every clone write into one shared tree.)
DEFAULT_WORKDIR = Path(__file__).resolve().parent / "work"


# ----------------------------------------------------------------------------
# shell helpers
# ----------------------------------------------------------------------------
def run(cmd: list[str], *, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    """Run a command, streaming nothing, capturing everything."""
    if not quiet:
        print(f"  $ {' '.join(cmd[:6])}{' ...' if len(cmd) > 6 else ''}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd[:4])}\n" + "\n".join(tail))
    return proc


def require(*binaries: str) -> None:
    missing = [b for b in binaries if not shutil.which(b)]
    if missing:
        raise SystemExit(f"missing required binaries: {', '.join(missing)}")


def parse_time(value) -> float:
    """Accept 12.5, '12.5', '1:23', '1:02:03' -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"bad timestamp: {value!r}") from exc
    seconds = 0.0
    for num in nums:
        seconds = seconds * 60 + num
    return seconds


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug[:limit].rstrip("-") or "clip")


# ----------------------------------------------------------------------------
# caption parsing
# ----------------------------------------------------------------------------
@dataclass
class Segment:
    start: float
    end: float
    text: str


_TAG_RE = re.compile(r"<[^>]+>")
_VTT_CUE_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{3})"
)
# [Music], [Applause], (laughs) — annotations, not spoken words. Burning these
# in makes a clip look auto-generated.
_NON_SPEECH_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]")


def _clean(text: str) -> str:
    text = _TAG_RE.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    return re.sub(r"\s+", " ", text).strip()


def parse_json3(path: Path) -> tuple[list[Segment], list[tuple[float, str]]]:
    """YouTube json3 captions -> (display segments, word-level timeline).

    json3 carries per-word `tOffsetMs` inside each event, which is far tighter
    than the event windows themselves (those roll and overlap by ~4s, so cues
    built from them drift up to 2s behind the audio).
    """
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    segments: list[Segment] = []
    words: list[tuple[float, str]] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        base = event.get("tStartMs", 0)
        text = _clean("".join(s.get("utf8", "") for s in segs))
        if not text:
            continue
        start = base / 1000.0
        dur = event.get("dDurationMs")
        segments.append(Segment(start, start + (dur / 1000.0 if dur else 2.0), text))
        for piece in segs:
            token = (piece.get("utf8") or "").strip()
            if token:
                words.append(((base + piece.get("tOffsetMs", 0)) / 1000.0, token))
    words.sort(key=lambda w: w[0])
    return segments, words


def parse_vtt_or_srt(path: Path) -> tuple[list[Segment], list[tuple[float, str]]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", raw)
    out: list[Segment] = []
    for block in blocks:
        match = _VTT_CUE_RE.search(block)
        if not match:
            continue
        start = parse_time(match.group(1).replace(",", "."))
        end = parse_time(match.group(2).replace(",", "."))
        lines = block[match.end():].strip().splitlines()
        text = _clean(" ".join(lines))
        if text:
            out.append(Segment(start, end, text))
    return dedupe_rolling(out), []


def dedupe_rolling(segments: list[Segment]) -> list[Segment]:
    """YouTube auto-caption VTT repeats the previous line in each cue.

    Collapse cue N into cue N+1 when N's text is a prefix/suffix duplicate.
    """
    out: list[Segment] = []
    for seg in segments:
        if out:
            prev = out[-1]
            if seg.text == prev.text:
                prev.end = max(prev.end, seg.end)
                continue
            if seg.text.startswith(prev.text) and len(seg.text) > len(prev.text):
                prev.text = seg.text
                prev.end = max(prev.end, seg.end)
                continue
            if prev.text.endswith(seg.text):
                continue
        out.append(Segment(seg.start, seg.end, seg.text))
    return out


def load_captions(job_dir: Path) -> tuple[list[Segment], list[tuple[float, str]], str | None]:
    """Find whatever caption file yt-dlp produced and parse it."""
    candidates: list[Path] = []
    for pattern in ("*.json3", "*.vtt", "*.srt", "*.srv3"):
        candidates.extend(sorted(job_dir.glob(f"subs*{pattern[1:]}")))
        candidates.extend(sorted(job_dir.glob(pattern)))
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            segs, words = (
                parse_json3(path) if path.suffix == ".json3" else parse_vtt_or_srt(path)
            )
        except Exception:
            continue
        if segs:
            return segs, words, path.name
    return [], [], None


# ----------------------------------------------------------------------------
# fetch
# ----------------------------------------------------------------------------
def cmd_fetch(args: argparse.Namespace) -> int:
    require("yt-dlp", "ffmpeg", "ffprobe")
    workdir = Path(args.workdir).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)

    # Keep authentication/challenge solving on every yt-dlp call, including
    # metadata probe. Previously cookies were added only after this probe,
    # making a valid cookie file appear ineffective.
    common = ["yt-dlp", "--no-warnings", "--no-playlist",
              "--remote-components", "ejs:github"]
    if args.cookies:
        common += ["--cookies", args.cookies]
    elif args.cookies_from_browser:
        common += ["--cookies-from-browser", args.cookies_from_browser]

    probe = run(common + [
        "--skip-download",
        "--print", "%(id)s\t%(title)s\t%(duration)s\t%(uploader)s",
        args.url,
    ])
    line = [l for l in probe.stdout.strip().splitlines() if "\t" in l]
    if not line:
        raise SystemExit(f"could not read video metadata:\n{probe.stderr[-500:]}")
    video_id, title, duration, uploader = (line[-1].split("\t") + ["", "", "", ""])[:4]

    job_dir = workdir / slugify(f"{video_id}-{title}", 60)
    job_dir.mkdir(parents=True, exist_ok=True)
    print(f"job dir : {job_dir}")
    print(f"title   : {title}")
    print(f"length  : {fmt_time(float(duration or 0))}")

    height = args.max_height
    fmt = (
        f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={height}]+ba/b[height<={height}]/b"
    )
    common = ["yt-dlp", "--no-warnings", "--no-playlist",
              "--remote-components", "ejs:github"]
    if args.cookies:
        common += ["--cookies", args.cookies]
    elif args.cookies_from_browser:
        common += ["--cookies-from-browser", args.cookies_from_browser]

    # Captions first, in their own call. A caption failure (429, none offered)
    # must not abort the video download — and an exact language list is
    # mandatory: "en.*" matches every auto-TRANSLATED track (en-ar, en-bn, ...)
    # and YouTube rate-limits after a handful.
    # Try the requested language first, then the video's own language. Each
    # attempt is a SEPARATE call: yt-dlp aborts the whole run on the first
    # caption error (a 429 on track 2 loses track 1), and asking for several
    # languages at once is exactly what triggers the 429.
    lang = args.lang
    wanted = [f"{lang}-orig", lang] if lang else []
    for extra in ("id-orig", "id", "en-orig", "en"):
        if extra not in wanted:
            wanted.append(extra)

    got_subs = False
    for candidate in wanted:
        attempt = run(common + [
            "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", candidate,
            "--sub-format", "json3/vtt/srt/best",
            # yt-dlp ignores `-o subtitle:` when the subtitle template has no
            # video extension of its own, and drops the file in the CWD under
            # the video's title. Pin the output dir too so the file always
            # lands where load_captions() looks for it.
            "-o", f"subtitle:{job_dir / 'subs.%(ext)s'}",
            "-P", str(job_dir),
            args.url,
        ], check=False, quiet=True)
        landed = (list(job_dir.glob("*.json3")) + list(job_dir.glob("*.vtt"))
                  + list(job_dir.glob("*.srt")))
        if landed:
            # yt-dlp may still name it after the video title; normalise so
            # load_captions() finds it by its documented prefix.
            for path in landed:
                if not path.name.startswith("subs"):
                    path.rename(job_dir / f"subs.{candidate}{path.suffix}")
            got_subs = True
            print(f"  captions: {candidate}")
            break
        if attempt.returncode != 0 and "429" in (attempt.stderr or ""):
            # Rate-limited: further language attempts will fail the same way.
            break
    if not got_subs:
        print("  (captions unavailable — clips will render without them)",
              file=sys.stderr)

    run(common + [
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--no-write-subs", "--no-write-auto-subs",
        "-o", str(job_dir / "source.%(ext)s"),
        args.url,
    ])

    sources = sorted(job_dir.glob("source.*"))
    video = next((p for p in sources if p.suffix in {".mp4", ".mkv", ".webm"}), None)
    if not video:
        raise SystemExit(f"no source video landed in {job_dir}")

    meta = probe_video(video)
    segments, words, sub_file = load_captions(job_dir)

    transcript = {
        "video_id": video_id,
        "title": title,
        "uploader": uploader,
        "url": args.url,
        "duration": float(duration or meta["duration"]),
        "source": str(video),
        "source_width": meta["width"],
        "source_height": meta["height"],
        "caption_file": sub_file,
        "segments": [asdict(s) for s in segments],
        "words": [[round(t, 3), w] for t, w in words],
    }
    (job_dir / "transcript.json").write_text(json.dumps(transcript, indent=1, ensure_ascii=False))

    print(f"video   : {video.name}  {meta['width']}x{meta['height']}  "
          f"{video.stat().st_size / 1e6:.1f} MB")
    print(f"captions: {sub_file or 'NONE'}  ({len(segments)} segments, "
          f"{len(words)} word timings)")
    print(f"\nnext: python clipper.py transcript {job_dir}")
    return 0


def probe_video(path: Path) -> dict:
    proc = run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ], quiet=True)
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": float((data.get("format") or {}).get("duration") or 0.0),
    }


# ----------------------------------------------------------------------------
# transcript view
# ----------------------------------------------------------------------------
def cmd_transcript(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser()
    data = json.loads((job_dir / "transcript.json").read_text(encoding="utf-8"))
    segments = [Segment(**s) for s in data["segments"]]
    if not segments:
        print("no captions available for this video", file=sys.stderr)
        return 1

    window = args.window
    print(f"# {data['title']}  ({fmt_time(data['duration'])})\n")
    bucket_start = 0.0
    buffer: list[str] = []
    for seg in segments:
        if seg.start >= bucket_start + window and buffer:
            print(f"[{fmt_time(bucket_start)}] {' '.join(buffer)}\n")
            buffer = []
            bucket_start = seg.start - (seg.start % window)
        if not buffer:
            bucket_start = seg.start - (seg.start % window)
        buffer.append(seg.text)
    if buffer:
        print(f"[{fmt_time(bucket_start)}] {' '.join(buffer)}")
    return 0


# ----------------------------------------------------------------------------
# caption cue building
# ----------------------------------------------------------------------------
_ASS_TICK = 0.01  # ASS timestamps have 10ms resolution — see ass_time()


def quantize(seconds: float) -> float:
    """Snap to the ASS timestamp grid so computed cues equal rendered cues."""
    return round(round(seconds / _ASS_TICK) * _ASS_TICK, 2)


def snap_window(
    words: list[tuple[float, str]],
    start: float,
    end: float,
    *,
    tolerance: float = 2.5,
) -> tuple[float, float]:
    """Nudge a clip window onto speech boundaries so it never starts or ends
    mid-word.

    start moves to the nearest word-start (preferring one that begins a
    sentence); end moves just past the nearest sentence-ending word. Both stay
    within `tolerance` seconds of the requested time, so a hand-picked window
    is respected — only tidied.
    """
    if not words:
        return start, end

    starts = [t for t, _ in words]
    sentence_starts = [
        t for i, (t, _) in enumerate(words)
        if i == 0 or words[i - 1][1].endswith((".", "!", "?"))
    ]
    sentence_ends = [t for t, w in words if w.endswith((".", "!", "?"))]

    def nearest(candidates: list[float], target: float) -> float | None:
        inside = [c for c in candidates if abs(c - target) <= tolerance]
        return min(inside, key=lambda c: abs(c - target)) if inside else None

    new_start = nearest(sentence_starts, start)
    if new_start is None:
        new_start = nearest(starts, start)
    if new_start is None:
        new_start = start

    new_end = nearest(sentence_ends, end)
    if new_end is not None:
        # Include the final word plus a beat of breathing room, but stop before
        # the next word is spoken — otherwise the clip ends on a dangling
        # fragment of the following sentence ("...see this three. But").
        following = [t for t in starts if t > new_end]
        tail_limit = following[0] if following else new_end + 0.9
        new_end = min(new_end + 0.9, tail_limit, end + tolerance)
    else:
        new_end = end

    if new_end - new_start < 1.0:
        return start, end
    return new_start, new_end


def _merge_small_groups(
    groups: list[list[tuple[float, str]]],
    *,
    per_line: int,
    max_chars: int,
    max_dur: float,
    min_words: int = 3,
) -> list[list[tuple[float, str]]]:
    """Fold orphan cues back into a neighbour.

    Breaking after sentence-ending punctuation leaves 1-word cues whenever a
    sentence is short ("lah.", "nob.", "Gila"). On screen those flash past and
    read as a stutter, so merge any group under `min_words` into the adjacent
    group it fits with. Merging only ever joins ADJACENT groups, so word order
    and timing stay monotonic, and it never runs if the merge would overflow
    two lines or the duration cap.
    """
    def text_of(group):
        return " ".join(w for _, w in group)

    def span(group):
        return group[-1][0] - group[0][0]

    merged_any = True
    while merged_any:
        merged_any = False
        for index, group in enumerate(groups):
            if len(group) >= min_words:
                continue
            best: tuple[int, int, list] | None = None
            for other in (index - 1, index + 1):
                if not 0 <= other < len(groups):
                    continue
                candidate = (groups[other] + group if other < index
                             else group + groups[other])
                text = text_of(candidate)
                if len(text) > max_chars or not fits_two_lines(text, per_line):
                    continue
                if span(candidate) > max_dur * 1.6:
                    continue
                # prefer the shorter neighbour so cues stay evenly sized
                score = len(groups[other])
                if best is None or score < best[0]:
                    best = (score, other, candidate)
            if best:
                _, other, candidate = best
                low, high = min(index, other), max(index, other)
                groups[low:high + 1] = [candidate]
                merged_any = True
                break
    return groups


def cues_from_words(
    words: list[tuple[float, str]],
    start: float,
    end: float,
    *,
    per_line: int,
    max_chars: int,
    max_dur: float = 2.6,
    gap_break: float = 0.7,
) -> list[Segment]:
    """Build cues from word-level timings — the accurate path.

    Each cue starts on the first word it shows and ends when the next cue's
    first word is spoken, so text appears exactly as it is said. Breaks on:
    two-line overflow, duration cap, sentence-ending punctuation, or a pause.
    All boundaries are snapped to the ASS 10ms grid, so what this returns is
    byte-for-byte what libass will render.
    """
    picked = [
        (t - start, w)
        for t, w in words
        if start <= t < end and not _NON_SPEECH_RE.fullmatch(w)
    ]
    if not picked:
        return []

    groups: list[list[tuple[float, str]]] = [[]]
    for i, (offset, word) in enumerate(picked):
        current = groups[-1]
        candidate = " ".join(w for _, w in current) + (" " if current else "") + word
        # Break on RENDERED geometry, not a raw character count: the cue must
        # fit two lines at this font size or words get folded/hidden.
        too_long = len(candidate) > max_chars or not fits_two_lines(candidate, per_line)
        too_slow = bool(current) and (offset - current[0][0]) > max_dur
        paused = bool(current) and (offset - current[-1][0]) > gap_break
        if current and (too_long or too_slow or paused):
            groups.append([(offset, word)])
        else:
            current.append((offset, word))
        # break AFTER sentence-ending punctuation so a new sentence starts fresh
        if word.endswith((".", "!", "?")) and i + 1 < len(picked):
            groups.append([])
    groups = [g for g in groups if g]

    # A short sentence leaves a 1-word cue that flashes past unreadably; fold
    # those back into a neighbour before any timing is computed.
    groups = _merge_small_groups(groups, per_line=per_line, max_chars=max_chars,
                                max_dur=max_dur)

    # Two groups whose first words land in the same 10ms tick would render as a
    # zero-length (invisible) cue — merge rather than drop the words.
    merged: list[list[tuple[float, str]]] = []
    for group in groups:
        if merged and quantize(group[0][0]) <= quantize(merged[-1][0][0]):
            merged[-1].extend(group)
        else:
            merged.append(group)
    groups = merged

    cues: list[Segment] = []
    clip_len = quantize(end - start)
    for index, group in enumerate(groups):
        cue_start = max(0.0, quantize(group[0][0]))
        if index + 1 < len(groups):
            # cue ends exactly when the next cue's first word is spoken
            next_start = quantize(groups[index + 1][0][0])
        else:
            next_start = clip_len
        cue_end = min(next_start, clip_len)
        if index + 1 == len(groups):
            # last cue: hold for a short tail rather than to the clip's end
            cue_end = min(clip_len, quantize(group[-1][0] + 1.4))
        # Nudge sub-frame cues up to a readable floor, but NEVER past the next
        # cue's start — overlapping cues make libass stack them on top of each
        # other and would also steal the next cue's first word.
        if cue_end - cue_start < 0.25:
            cue_end = min(next_start, quantize(cue_start + 0.25), clip_len)
        if cue_end <= cue_start:
            continue
        text = wrap_two_lines(" ".join(w for _, w in group), per_line)
        cues.append(Segment(cue_start, cue_end, text))
    return cues


def regroup_cues(
    segments: list[Segment],
    start: float,
    end: float,
    *,
    max_chars: int = 34,
    per_line: int = 17,
    max_dur: float = 2.8,
) -> list[Segment]:
    """Slice segments to the clip window, shift to zero, regroup into punchy cues."""
    window = [s for s in segments if s.end > start and s.start < end]
    cues: list[Segment] = []
    for seg in window:
        text = seg.text
        cue_start = max(seg.start, start) - start
        cue_end = min(seg.end, end) - start
        if cue_end - cue_start < 0.12:
            cue_end = cue_start + 0.12
        if cues:
            last = cues[-1]
            merged = f"{last.text} {text}".strip()
            if len(merged) <= max_chars and (cue_end - last.start) <= max_dur:
                last.text = merged
                last.end = cue_end
                continue
        cues.append(Segment(cue_start, cue_end, text))

    # split anything still too long onto two lines, and stop overlap
    out: list[Segment] = []
    for i, cue in enumerate(cues):
        if i + 1 < len(cues):
            cue.end = min(cue.end, cues[i + 1].start)
        if cue.end <= cue.start:
            continue
        out.append(Segment(cue.start, cue.end, wrap_two_lines(cue.text, per_line)))
    return out


def wrap_lines(text: str, width: int) -> list[str]:
    """Greedy word wrap. Never drops a word; a word longer than width gets its
    own line rather than being truncated."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def fits_two_lines(text: str, width: int) -> bool:
    return len(wrap_lines(text, width)) <= 2


def wrap_two_lines(text: str, width: int) -> str:
    r"""Join wrapped lines with the ASS line break.

    Callers must keep cue text inside two lines (see fits_two_lines) — this
    function will NOT silently discard overflow. Anything past line two is
    folded onto line two so no spoken word ever disappears from the caption.
    """
    lines = wrap_lines(text, width)
    if not lines:
        return text
    if len(lines) > 2:
        lines = [lines[0], " ".join(lines[1:])]
    return r"\N".join(lines)


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    centis = int(round(seconds * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6_000)
    secs, centis = divmod(centis, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def write_clip_ass(
    cues: list[Segment], path: Path, *, width: int, height: int, font: str
) -> bool:
    """Write captions as ASS with an explicit PlayRes matching the output frame.

    Do NOT emit SRT and lean on subtitles=force_style: ffmpeg's SRT decoder
    hardcodes a 384x288 ASS canvas, so libass rescales Fontsize AND MarginV by
    height/288 (3.3x at 960p). That threw captions off the top of the frame.
    An explicit PlayResX/PlayResY makes every value literal pixels.
    """
    if not cues:
        return False

    size = max(14, round(height * 0.046))
    # 20% bottom margin keeps captions clear of TikTok/Reels UI, which can
    # reach ~15-20% up the frame. Facebook Reels puts its own caption/CTA
    # block there too, so sit a little higher: 22%.
    margin_v = round(height * 0.22)
    margin_h = round(width * 0.07)
    outline = max(2, round(size * 0.09))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,-1,0,0,0,100,100,0,0,1,{outline},1,2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [
        f"Dialogue: 0,{ass_time(c.start)},{ass_time(c.end)},Caption,,0,0,0,,{c.text}"
        for c in cues
    ]
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return True


# ----------------------------------------------------------------------------
# reframing + cutting
# ----------------------------------------------------------------------------
def pick_font() -> str:
    """Find a font fontconfig can resolve for libass."""
    for name, probe in (
        ("DejaVu Sans", "DejaVuSans"),
        ("Noto Sans", "NotoSans"),
        ("Roboto", "Roboto"),
        ("Liberation Sans", "LiberationSans"),
    ):
        for root in ("/system/fonts", f"{os.environ.get('PREFIX', '/usr')}/share/fonts"):
            base = Path(root)
            if base.is_dir() and any(base.rglob(f"{probe}*.ttf")):
                return name
    return "sans-serif"


def build_video_filter(layout: str, width: int, height: int) -> str:
    if layout == "crop":
        return (
            f"crop='min(iw,ih*{width}/{height})':'min(ih,iw*{height}/{width})',"
            f"scale={width}:{height}:flags=bicubic,setsar=1"
        )
    if layout == "fit":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        )
    # blur: source fitted on a blurred, zoomed copy of itself
    return (
        f"split[bgsrc][fgsrc];"
        f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=28,eq=brightness=-0.08[bg];"
        f"[fgsrc]scale={width}:-2:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


def caption_geometry(width: int, height: int) -> tuple[int, int]:
    """(max chars per line, max chars per cue) for a bold sans at this frame size.

    Bold DejaVu advances ~0.58em; usable width excludes the 7% side margins.
    """
    size = max(14, round(height * 0.046))
    usable = width * 0.86
    per_line = max(10, int(usable / (size * 0.58)))
    return per_line, per_line * 2


def cmd_cut(args: argparse.Namespace) -> int:
    require("ffmpeg")
    job_dir = Path(args.job_dir).expanduser()
    data = json.loads((job_dir / "transcript.json").read_text(encoding="utf-8"))
    segments = [Segment(**s) for s in data["segments"]]
    source = Path(data["source"])
    if not source.is_file():
        raise SystemExit(f"source video missing: {source}")

    clips = json.loads(Path(args.clips).expanduser().read_text(encoding="utf-8"))
    if isinstance(clips, dict):
        clips = clips.get("clips", [])
    if not clips:
        raise SystemExit("clips file contained no clips")

    words = [(float(t), w) for t, w in data.get("words", [])]

    out_dir = job_dir / "clips"
    out_dir.mkdir(exist_ok=True)
    tmp_dir = job_dir / ".tmp"
    tmp_dir.mkdir(exist_ok=True)

    height = args.height
    width = args.width or (round(height * 9 / 16) // 2) * 2
    font = pick_font()
    base_vf = build_video_filter(args.layout, width, height)
    per_line, per_cue = caption_geometry(width, height)

    print(f"source  : {source.name} ({data['source_width']}x{data['source_height']})")
    print(f"output  : {width}x{height}  layout={args.layout}  font={font}")
    if not args.captions:
        print("captions: off (pass --captions to burn them in)\n")
    else:
        mode = "word-timed" if words else "segment-timed (no word timings)"
        print(f"captions: {mode}, {len(words) or len(segments)} units\n")

    made: list[Path] = []
    for index, clip in enumerate(clips, 1):
        start = parse_time(clip["start"])
        end = parse_time(clip["end"])
        if end <= start:
            print(f"[{index}] skipped — end <= start", file=sys.stderr)
            continue
        requested = (start, end)
        if words and not args.exact_times:
            start, end = snap_window(words, start, end)
        duration = end - start
        title = clip.get("title") or f"clip-{index:02d}"
        out_path = out_dir / f"{index:02d}-{slugify(title)}.mp4"

        vf = base_vf
        if args.captions and (words or segments):
            if words:
                cues = cues_from_words(words, start, end,
                                       per_line=per_line, max_chars=per_cue)
            else:
                cues = regroup_cues(segments, start, end,
                                    max_chars=per_cue, per_line=per_line)
            ass_path = tmp_dir / f"cues-{index:02d}.ass"
            if write_clip_ass(cues, ass_path, width=width, height=height, font=font):
                escaped = str(ass_path).replace("\\", "/").replace(":", r"\:")
                vf = f"{vf},subtitles='{escaped}'"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", str(source),
            "-filter_complex" if args.layout == "blur" else "-vf", vf,
            "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", "-r", str(args.fps),
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart",
            str(out_path),
        ]
        print(f"[{index}] {title}  {fmt_time(start)}–{fmt_time(end)} ({duration:.0f}s)")
        if (start, end) != requested:
            print(f"     snapped to speech from "
                  f"{fmt_time(requested[0])}–{fmt_time(requested[1])}")
        run(cmd, quiet=True)
        size_mb = out_path.stat().st_size / 1e6
        print(f"     -> {out_path.name}  {size_mb:.1f} MB")
        made.append(out_path)

    if not args.keep_subs:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"\n{len(made)} clip(s) in {out_dir}")
    if args.keep_subs:
        print(f"caption files kept in {tmp_dir}")
    return 0 if made else 1


# ----------------------------------------------------------------------------
# publish
# ----------------------------------------------------------------------------
def cmd_publish(args: argparse.Namespace) -> int:
    """Push rendered clips to social platforms via the Upload-Post API.

    Dry-run is the default: publishing is irreversible, public, and metered
    (the free plan allows 10 uploads/month). --yes is the only way to spend it.
    """
    import uploadpost as up

    env_path = Path(__file__).resolve().parent / ".env"

    # --check verifies credentials and lists the connected accounts. It hits a
    # read-only endpoint, so it costs no upload quota — always the first call
    # to make against a new key.
    if args.check:
        api_key = up.load_api_key(env_path)
        print(f"api key  : {up.redact(api_key)}")
        data = up.check_profiles(api_key)
        profiles = data.get("profiles", [])
        if not profiles:
            print("no profiles — create one at https://app.upload-post.com")
            return 1
        print(f"plan     : {data.get('plan', '?')}  "
              f"(profile limit {data.get('limit', '?')})\n")
        for profile in profiles:
            name = profile.get("username", "?")
            print(f"profile: {name}")
            accounts = profile.get("social_accounts") or {}
            connected = {k: v for k, v in accounts.items() if v}
            if not connected:
                print("  (no social accounts connected — uploads would be skipped)")
            for platform, info in connected.items():
                label = info.get("handle") or info.get("display_name") \
                    if isinstance(info, dict) else str(info)
                flag = " [REAUTH REQUIRED]" if isinstance(info, dict) \
                    and info.get("reauth_required") else ""
                print(f"  {platform:<16} {label or '(connected)'}{flag}")
            print(f"\n  use it with: --user {name}   "
                  f"(or UPLOAD_POST_USER={name} in .env)\n")
        return 0

    if not args.job_dir:
        raise SystemExit("job_dir is required (omit it only with --check)")
    user = args.user or up.load_user(env_path)

    job_dir = Path(args.job_dir).expanduser()
    if not job_dir.is_dir():
        raise SystemExit(f"no such job dir: {job_dir}")

    clips_spec: list[dict] = []
    spec_path = Path(args.clips).expanduser() if args.clips else None
    if spec_path and spec_path.is_file():
        loaded = json.loads(spec_path.read_text(encoding="utf-8"))
        clips_spec = loaded.get("clips", []) if isinstance(loaded, dict) else loaded

    ledger_path = job_dir / "publish.json"
    ledger = up.load_ledger(ledger_path)

    plans = up.plan_uploads(
        job_dir, args.platform,
        clips_spec=clips_spec,
        ledger=ledger,
        force=args.force,
        only=args.only,
    )

    live = [p for p in plans if p.platforms]
    if args.max_uploads is not None:
        live = live[:args.max_uploads]

    print(f"job      : {job_dir}")
    print(f"profile  : {user}")
    print(f"platforms: {', '.join(args.platform)}")
    if args.schedule:
        print(f"schedule : {args.schedule} ({args.timezone or 'UTC'})")
    print(f"uploads  : {len(live)} of {len(plans)} clip(s)\n")

    for plan in plans:
        if not plan.platforms:
            print(f"[{plan.index}] {plan.name}  — already published to "
                  f"{', '.join(plan.skipped)}; skipping (use --force to resend)")
            continue
        if plan not in live:
            print(f"[{plan.index}] {plan.name}  — held back by --max-uploads")
            continue
        size_mb = plan.path.stat().st_size / 1e6
        print(f"[{plan.index}] {plan.name}  {size_mb:.1f} MB -> "
              f"{', '.join(plan.platforms)}")
        print(f"     title: {plan.title}")
        if plan.skipped:
            print(f"     (skipping {', '.join(plan.skipped)} — already done)")

    if not live:
        print("\nnothing to upload.")
        return 0

    if not args.yes:
        print(f"\nDRY RUN — nothing was sent. {len(live)} upload(s) would be "
              f"spent.\nRe-run with --yes to publish.")
        return 0

    api_key = up.load_api_key(env_path)
    print(f"\napi key  : {up.redact(api_key)}")
    print(f"sending {len(live)} upload(s)...\n")

    failures = 0
    for index, plan in enumerate(live):
        scheduled_date = args.schedule
        if scheduled_date and args.gap_minutes:
            try:
                base = dt.datetime.fromisoformat(scheduled_date.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SystemExit(f"invalid --schedule ISO-8601 value: {scheduled_date}") from exc
            base += dt.timedelta(minutes=args.gap_minutes * index)
            scheduled_date = base.isoformat(timespec="seconds")
        fields = up.build_fields(
            plan, user,
            scheduled_date=scheduled_date,
            timezone=args.timezone,
            async_upload=not args.sync,
        )
        try:
            response = up.send_upload(plan, api_key, fields)
        except RuntimeError as exc:
            failures += 1
            print(f"[{plan.index}] {plan.name}  FAILED: {exc}", file=sys.stderr)
            continue

        request_id = response.get("request_id") or plan.idempotency_key
        print(f"[{plan.index}] {plan.name}  accepted  request_id={request_id}")

        if not args.no_wait and not args.schedule:
            status = up.poll_status(request_id, api_key)
            response = {**response, **status}
            state = status.get("status", "unknown")
            print(f"     status: {state}")
            for platform, url in up.post_urls(status).items():
                print(f"     {platform}: {url}")
            if state == "failed":
                failures += 1

        ledger = up.record(ledger, plan, response)
        ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    print(f"\nledger: {ledger_path}")
    if failures:
        print(f"{failures} upload(s) did not succeed.", file=sys.stderr)
    return 1 if failures else 0


# ----------------------------------------------------------------------------
# cli
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="download source video + captions")
    p_fetch.add_argument("url")
    p_fetch.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    p_fetch.add_argument("--max-height", type=int, default=720)
    p_fetch.add_argument("--lang", default="en")
    p_fetch.add_argument("--cookies", default=None,
                         help="Netscape cookies.txt file for YouTube login")
    p_fetch.add_argument("--cookies-from-browser", default=None)
    p_fetch.set_defaults(func=cmd_fetch)

    p_tr = sub.add_parser("transcript", help="print timestamped transcript")
    p_tr.add_argument("job_dir")
    p_tr.add_argument("--window", type=float, default=30.0)
    p_tr.set_defaults(func=cmd_transcript)

    p_cut = sub.add_parser("cut", help="cut clips from clips.json")
    p_cut.add_argument("job_dir")
    p_cut.add_argument("clips")
    p_cut.add_argument("--layout", choices=["crop", "blur", "fit"], default="blur")
    p_cut.add_argument("--height", type=int, default=1280)
    p_cut.add_argument("--width", type=int, default=None)
    p_cut.add_argument("--fps", type=int, default=30)
    p_cut.add_argument("--crf", type=int, default=23)
    p_cut.add_argument("--preset", default="veryfast")
    # Captions are OPT-IN: burned-in text is a style choice, and the default
    # must not silently bake it into every clip. --captions turns it on.
    p_cut.add_argument("--captions", dest="captions", action="store_true",
                       default=False,
                       help="burn word-timed captions into the clips "
                            "(off by default)")
    p_cut.add_argument("--no-captions", dest="captions", action="store_false",
                       help="explicit opposite of --captions (already the default)")
    p_cut.add_argument("--exact-times", action="store_true",
                       help="use clips.json times verbatim; skip snapping to speech")
    p_cut.add_argument("--keep-subs", action="store_true",
                       help="keep the generated .ass caption files for inspection")
    p_cut.set_defaults(func=cmd_cut)

    p_pub = sub.add_parser("publish",
                           help="upload rendered clips via Upload-Post")
    p_pub.add_argument("job_dir", nargs="?", default=None,
                       help="job directory (omit only with --check)")
    p_pub.add_argument("--check", action="store_true",
                       help="verify the API key and list connected accounts; "
                            "costs no upload quota")
    p_pub.add_argument("--clips", default=None,
                       help="clips.json used for the cut, for titles/captions")
    p_pub.add_argument("--user", default=None,
                       help="Upload-Post profile name "
                            "(default: UPLOAD_POST_USER from env or .env)")
    p_pub.add_argument("--platform", action="append", default=[],
                       help="target platform; repeat for several")
    p_pub.add_argument("--only", action="append", default=None,
                       help="publish just these clips (filename, stem, or index)")
    # Publishing is irreversible and metered. Dry-run is the default, and --yes
    # is the only thing that spends quota.
    p_pub.add_argument("--yes", action="store_true",
                       help="actually send (default is a dry run)")
    p_pub.add_argument("--max-uploads", type=int, default=None,
                       help="cap uploads spent in this run")
    p_pub.add_argument("--force", action="store_true",
                       help="resend even if publish.json says it already landed")
    p_pub.add_argument("--schedule", default=None,
                       help="ISO-8601 publish time, e.g. 2026-09-20T18:00:00")
    p_pub.add_argument("--timezone", default=None,
                       help="IANA zone for --schedule, e.g. Asia/Jakarta")
    p_pub.add_argument("--gap-minutes", type=int, default=0,
                       help="add this many minutes between scheduled clips")
    p_pub.add_argument("--sync", action="store_true",
                       help="synchronous upload (default is async + polling)")
    p_pub.add_argument("--no-wait", action="store_true",
                       help="do not poll for status after sending")
    p_pub.set_defaults(func=cmd_publish)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
