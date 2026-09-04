#!/usr/bin/env bash
# Fallback path: a caption source with NO word timings (VTT-only video).
# Proves captioned clips still render via regroup_cues, with sane cue geometry.
#
#   bash run_fallback.sh [source_job_dir] [clips.json]
set -eu
cd "$(dirname "$0")"

SRC="${1:-$(ls -dt work/*/ 2>/dev/null | head -1)}"
CLIPS="${2:-clips.json}"
[ -n "$SRC" ] || { echo "no job in work/ — run 'clipper.py fetch <url>' first"; exit 1; }
SRC="${SRC%/}"
FB="work/fallback-test"

rm -rf "$FB"; mkdir -p "$FB"
python - "$SRC" "$FB" <<'PY'
import json, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
data = json.loads((src / "transcript.json").read_text(encoding="utf-8"))
data["words"] = []                       # simulate a VTT-only caption source
data["caption_file"] = "subs.vtt"
(dst / "transcript.json").write_text(json.dumps(data), encoding="utf-8")
print(f"fallback transcript: {len(data['segments'])} segments, 0 word timings")
PY

python clipper.py cut "$FB" "$CLIPS" --captions --keep-subs --layout crop --height 640

echo
echo "=== fallback cue sanity ==="
python - "$FB" <<'PY'
import re, sys
from pathlib import Path

job = Path(sys.argv[1])
def secs(stamp):
    h, m, rest = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)

bad = 0
files = sorted((job / ".tmp").glob("*.ass"))
if not files:
    print("FAIL: no .ass files produced"); sys.exit(1)
for ass in files:
    rows = [l for l in ass.read_text(encoding="utf-8").splitlines()
            if l.startswith("Dialogue:")]
    times = [(secs(r.split(",")[1]), secs(r.split(",")[2])) for r in rows]
    overlap = sum(1 for a, b in zip(times, times[1:]) if a[1] > b[0] + 1e-9)
    empty = sum(1 for a, b in times if b <= a)
    print(f"{ass.name}: {len(rows)} cues  overlap={overlap} empty={empty}")
    if not rows or overlap or empty:
        bad = 1
sys.exit(bad)
PY
echo
echo "FALLBACK PATH OK"
