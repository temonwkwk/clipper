#!/usr/bin/env bash
# Caption correctness check.
#
# Cuts with --captions --keep-subs, then parses the REAL .ass files ffmpeg
# burned in and asserts, per cue, that the words shown equal the words spoken
# inside that cue's own [start, end) window on the quantized word timeline.
# Also asserts no overlapping cues and no over-long lines.
#
#   bash run_verify.sh [job_dir] [clips.json] [height]
set -eu
cd "$(dirname "$0")"

JOB="${1:-$(ls -dt work/*/ 2>/dev/null | head -1)}"
CLIPS="${2:-clips.json}"
HEIGHT="${3:-1280}"
[ -n "$JOB" ] || { echo "no job in work/ — run 'clipper.py fetch <url>' first"; exit 1; }
JOB="${JOB%/}"

python clipper.py cut "$JOB" "$CLIPS" --captions --keep-subs \
  --layout blur --height "$HEIGHT" 2>&1 | tail -12

python - "$JOB" "$CLIPS" <<'PY'
import json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))   # run_verify.sh cd's to the repo root
from clipper import caption_geometry, fmt_time, parse_time, quantize, snap_window

job = Path(sys.argv[1])
clips = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
if isinstance(clips, dict):
    clips = clips.get("clips", [])

data = json.loads((job / "transcript.json").read_text(encoding="utf-8"))
words = [(float(t), w) for t, w in data.get("words", [])]
if not words:
    print("\nno word timings in this job — nothing to verify against")
    sys.exit(0)

NON_SPEECH = re.compile(r"[\[\(][^\]\)]*[\]\)]")

def ass_seconds(stamp):
    h, m, rest = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)

def norm(text):
    return re.sub(r"[^a-z0-9' ]", "", text.lower()).split()

failures = []
print()
for index, clip in enumerate(clips, 1):
    ass = job / ".tmp" / f"cues-{index:02d}.ass"
    if not ass.is_file():
        failures.append(f"clip {index}: no .ass produced")
        continue

    text = ass.read_text(encoding="utf-8")
    playres = dict(re.findall(r"PlayRes([XY]): (\d+)", text))
    width, height = int(playres.get("X", 0)), int(playres.get("Y", 0))
    per_line, _ = caption_geometry(width, height)

    cues = []
    for line in text.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            cues.append((ass_seconds(parts[1]), ass_seconds(parts[2]), parts[9]))

    start, end = parse_time(clip["start"]), parse_time(clip["end"])
    start, end = snap_window(words, start, end)   # cut snaps by default
    clip_words = [
        (quantize(t - start), w)
        for t, w in words
        if start <= t < end and not NON_SPEECH.fullmatch(w)
    ]

    desync = badline = overlap = 0
    for i, (cue_start, cue_end, body) in enumerate(cues):
        spoken = norm(" ".join(w for t, w in clip_words if cue_start <= t < cue_end))
        shown = norm(body.replace(r"\N", " "))
        if shown != spoken:
            desync += 1
            if desync == 1:
                print(f"  clip {index} cue {i} MISMATCH\n"
                      f"    shown : {shown}\n    spoken: {spoken}")
        lines = body.split(r"\N")
        if len(lines) > 2 or max((len(l) for l in lines), default=0) > per_line + 6:
            badline += 1
        if i and cues[i - 1][1] > cue_start + 1e-9:
            overlap += 1
        if cue_end <= cue_start:
            overlap += 1

    coverage = sum(e - s for s, e, _ in cues)
    ok = bool(cues) and not (desync or badline or overlap)
    print(f"clip {index} {clip.get('title', ''):<24} {len(cues):>3} cues  "
          f"{width}x{height}  cov={coverage:.1f}/{end - start:.0f}s  "
          f"desync={desync} badline={badline} overlap={overlap}  "
          f"{'OK' if ok else 'FAIL'}")
    if not ok:
        failures.append(f"clip {index} ({fmt_time(start)}-{fmt_time(end)})")

print()
if failures:
    print("FAILED:", ", ".join(failures))
    sys.exit(1)
print("ALL CAPTION CUES VERIFIED AGAINST WORD TIMELINE")
PY
