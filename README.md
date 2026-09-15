# clipper

Turn one long video URL into several short vertical (9:16) clips, ready for
Reels / Shorts / TikTok. Optional word-accurate burned-in captions.

Runs entirely on-device with `yt-dlp` + CPU `ffmpeg` — no GPU, no cloud, no API
keys. Developed and tested on Android/Termux, works the same on Linux/macOS.

## Install

```bash
# Termux
pkg install ffmpeg yt-dlp

# Debian/Ubuntu
sudo apt install ffmpeg && pipx install yt-dlp

# macOS
brew install ffmpeg yt-dlp
```

Python 3.10+. No third-party Python packages required (pytest only for tests).

## Use

```bash
# 1. download the source video + its captions
python clipper.py fetch "https://youtu.be/VIDEO_ID"

# 2. read the transcript and pick your windows
python clipper.py transcript work/<job-dir> --window 60

# 3. cut
python clipper.py cut work/<job-dir> clips.json

# 4. publish (optional — dry run by default, see below)
python clipper.py publish work/<job-dir> --clips clips.json \
  --user <profile> --platform tiktok --platform youtube
```

`clips.json` — times accept seconds, `M:SS`, or `H:MM:SS`:

```json
[
  {"title": "hook",   "start": "0:00",  "end": "0:38"},
  {"title": "payoff", "start": 412.5,   "end": 455}
]
```

An optional `"caption"` field per clip is carried through, and `publish` uses it
as the post text. The `clips.json` in this repo is a working example cut against
3Blue1Brown's "But what is a neural network?" — replace the windows with your
own.

Clips land in `work/<job-dir>/clips/NN-title.mp4`.

## Publishing

`publish` pushes rendered clips to social platforms through
[Upload-Post](https://app.upload-post.com) — one request per clip, fanned out to
every `--platform` you list. Connect the social accounts in their dashboard
first; the API key alone is not enough.

The key is read from `UPLOAD_POST_API_KEY` in the environment, or from a `.env`
beside the script. It is never accepted as a CLI argument, because `argv` is
world-readable.

```bash
cp .env.example .env    # then paste your key in
python clipper.py publish work/<job-dir> --clips clips.json \
  --user mybrand --platform tiktok        # dry run: prints, sends nothing
python clipper.py publish work/<job-dir> --clips clips.json \
  --user mybrand --platform tiktok --yes  # actually publishes
```

**Quota discipline.** Upload-Post's free plan allows 10 uploads/month, and a
publish is irreversible, so the defaults are deliberately timid:

| Flag | Effect |
|---|---|
| *(none)* | **Dry run.** Prints the plan, spends nothing. `--yes` is the only way to send |
| `--max-uploads N` | Hard cap on uploads spent in one run |
| `--only 02` | Publish just these clips (filename, stem, or index) |
| `--force` | Resend even if `publish.json` says it already landed |
| `--schedule` / `--timezone` | ISO-8601 publish time, e.g. `--timezone Asia/Jakarta` |
| `--sync` / `--no-wait` | Synchronous upload / skip status polling |

Every send is recorded in `work/<job-dir>/publish.json`, per clip **and per
platform**. Re-running skips what already landed, so an interrupted batch
resumes instead of double-posting — and a clip that reached TikTok but failed on
YouTube retries only YouTube. Each request also carries a stable
`Idempotency-Key`, so a retry after a network timeout resumes the existing job
rather than burning a second upload.

Platform reality check: YouTube and TikTok both gate API publishing behind an app
audit. Upload-Post has passed those audits — that, not the HTTP call, is what
you are paying for. Facebook Pages, Threads, Bluesky, Telegram and Discord have
no such gate and can be self-hosted against their own APIs if you'd rather not
spend quota on them.

## Options

| Flag | Effect |
|---|---|
| `--layout blur` | Blurred zoomed backdrop, source fitted centre (default) |
| `--layout crop` | Centre-crop to 9:16 — fills the frame, cuts the sides |
| `--layout fit` | Black bars, nothing cropped |
| `--height 1280` | Output height; width auto-derives 9:16 |
| `--captions` | Burn word-timed subtitles in (**off** by default) |
| `--exact-times` | Don't snap windows to sentence boundaries |
| `--keep-subs` | Keep the generated `.ass` files for inspection |
| `--crf 23` / `--preset veryfast` | Encode quality / speed |

`fetch --max-height 720` caps download resolution (use 360 while iterating).
`fetch --lang id` picks a caption language. `fetch --cookies-from-browser
chrome` for age- or login-gated videos.

## How captions stay in sync

YouTube's `json3` caption format carries per-word timings (`tOffsetMs`). Clipper
builds cues from those words, not from the caption *events* — events roll and
overlap by ~4 s, which is why naive clippers run up to 2 s behind the audio.

Three details that are easy to get wrong and are handled here:

- **Cue boundaries are snapped to the ASS 10 ms grid**, so the cues computed in
  Python are byte-identical to what libass renders.
- **Captions are written as `.ass` with explicit `PlayResX`/`PlayResY`**, never
  as `.srt` + `force_style`. ffmpeg's SRT decoder assumes a 384×288 canvas, so
  libass silently scales font size *and* margins by `height/288` — at 960p that
  throws captions off the top of the frame.
- **Short sentences are merged into neighbouring cues.** Breaking after
  sentence-ending punctuation otherwise produces one-word cues that flash past
  unreadably.

Clip windows are also nudged onto sentence boundaries so a clip never opens or
closes mid-word.

## Verification

```bash
python -m pytest test_clipper.py -q   # 43 unit tests, no network
bash run_accept.sh                    # fresh cut + stream/decode/aspect checks
bash run_verify.sh                    # parses the real .ass files and asserts
                                      # every cue's text equals the words
                                      # spoken in that cue's own window
bash run_fallback.sh                  # proves the no-word-timings path works
```

`run_verify.sh` is the important one: it checks caption correctness against the
word-level timeline rather than trusting the renderer. A non-zero desync count
is a bug, not a rounding artifact.

## Limits

- No speech recognition. A video with captions disabled produces clips without
  captions (`fetch` reports `captions: NONE`); everything else still works.
- Vertical reframing is centre-based — there is no face or subject tracking.
  For talking-head footage where the speaker is off-centre, prefer
  `--layout blur`.
- Reels/Shorts want 3–90 s, 9:16, H.264 + AAC. Output already complies.

## Respect the source

Clipping someone else's video and reposting it is a copyright question, not a
technical one. Get permission, or clip material you own or that is licensed for
reuse, and credit the original.

## License

MIT
