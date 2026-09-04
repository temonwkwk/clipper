#!/usr/bin/env bash
# Acceptance check: cut a job's clips, then probe each output for both streams,
# exact 9:16 geometry, and clean end-to-end decodability.
#
#   bash run_accept.sh [job_dir] [clips.json] [height]
#
# Defaults to the newest job in work/ and clips.json at repo root.
set -eu
cd "$(dirname "$0")"

JOB="${1:-$(ls -dt work/*/ 2>/dev/null | head -1)}"
CLIPS="${2:-clips.json}"
HEIGHT="${3:-1280}"
[ -n "$JOB" ] || { echo "no job in work/ — run 'clipper.py fetch <url>' first"; exit 1; }
JOB="${JOB%/}"
WIDTH=$(( (HEIGHT * 9 / 16) / 2 * 2 ))

echo "job    : $JOB"
echo "clips  : $CLIPS"
echo "expect : ${WIDTH}x${HEIGHT} h264 + aac"
echo

rm -rf "$JOB/clips"
python clipper.py cut "$JOB" "$CLIPS" --layout blur --height "$HEIGHT"

echo
echo "=== stream + decode check ==="
fail=0
for f in "$JOB"/clips/*.mp4; do
  v=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=codec_name,width,height -of csv=p=0 "$f")
  a=$(ffprobe -v error -select_streams a:0 \
        -show_entries stream=codec_name -of csv=p=0 "$f")
  d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
  err=$(ffmpeg -v error -i "$f" -f null - 2>&1 | head -3)
  printf '%-34s %s  audio=%s  %.1fs\n' "$(basename "$f")" "$v" "${a:-NONE}" "$d"
  [ -z "$a" ]   && { echo "   FAIL: no audio stream"; fail=1; }
  [ -n "$err" ] && { echo "   FAIL decode: $err"; fail=1; }
  [ "$v" = "h264,$WIDTH,$HEIGHT" ] || { echo "   FAIL: expected h264,$WIDTH,$HEIGHT"; fail=1; }
done

echo
[ "$fail" -eq 0 ] && echo "ACCEPTANCE PASSED" || { echo "ACCEPTANCE FAILED"; exit 1; }
