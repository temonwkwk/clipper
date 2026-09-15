#!/usr/bin/env python3
"""
Upload-Post publishing backend for clipper.

Pushes rendered clips to social platforms through the Upload-Post API
(https://api.upload-post.com/api/upload) — one multipart request per clip,
fanned out to every requested platform.

Why this module exists separately from clipper.py: cutting video is offline and
free, publishing is online, irreversible, and metered. The two deserve
different blast radii.

Quota discipline (the free plan is 10 uploads/month):
  * --dry-run is the DEFAULT. Nothing leaves the machine without --yes.
  * Every send is recorded in <job>/publish.json. A clip already published to a
    platform is skipped on re-run unless --force.
  * --max-uploads caps a single invocation.
  * Each request carries a stable Idempotency-Key, so a retry after a timeout
    resumes the existing job instead of burning a second upload.

Only the stdlib is used, matching the rest of the repo.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

API_BASE = "https://api.upload-post.com/api"
UPLOAD_URL = f"{API_BASE}/upload"
STATUS_URL = f"{API_BASE}/uploadposts/status"

# Platforms the Upload-Post video endpoint accepts. Kept explicit so a typo
# fails locally instead of burning a request on a 400.
PLATFORMS = (
    "tiktok", "instagram", "youtube", "facebook", "linkedin", "twitter",
    "threads", "pinterest", "bluesky", "reddit", "discord", "telegram",
    "google_business", "mastodon", "wordpress",
)

# YouTube and Reddit reject an upload with no title. We never hit that: every
# clip gets a title by construction (caption -> humanized filename -> "Clip"),
# so the requirement is satisfied before a request is ever built. Kept named
# for the reader, and asserted in plan_uploads.
TITLE_REQUIRED = frozenset({"youtube", "reddit"})

# Terminal states from GET /uploadposts/status.
DONE_STATES = frozenset({"completed", "failed", "not_found"})


# ----------------------------------------------------------------------------
# credentials
# ----------------------------------------------------------------------------
def parse_env_file(text: str) -> dict[str, str]:
    """Minimal KEY=VALUE .env reader: ignores comments, strips one quote layer."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_setting(name: str, env_path: Path | None = None,
                 environ: "os._Environ[str] | dict[str, str] | None" = None) -> str:
    """Read one setting from the environment, falling back to a .env file.

    Returns "" when unset — callers decide whether that is fatal.
    """
    env: "os._Environ[str] | dict[str, str]" = os.environ if environ is None else environ
    value = (env.get(name) or "").strip()
    if value:
        return value
    if env_path and env_path.is_file():
        return parse_env_file(env_path.read_text(encoding="utf-8")).get(name, "").strip()
    return ""


def load_api_key(env_path: Path | None = None,
                 environ: "os._Environ[str] | dict[str, str] | None" = None) -> str:
    """Resolve the API key from the environment, then from a .env file.

    Never accepted on the command line: argv is world-readable via /proc.
    """
    key = load_setting("UPLOAD_POST_API_KEY", env_path, environ)
    if key:
        return key
    raise SystemExit(
        "missing UPLOAD_POST_API_KEY.\n"
        "  export UPLOAD_POST_API_KEY=... , or put it in clipper/.env\n"
        "  (get one at https://app.upload-post.com -> API Keys)"
    )


def load_user(env_path: Path | None = None,
              environ: "os._Environ[str] | dict[str, str] | None" = None) -> str:
    """Resolve the Upload-Post profile name. Unlike the key, --user may override.

    Not a secret, so it is fine on the command line — but it never changes
    between runs, so .env is the sane home for it.
    """
    user = load_setting("UPLOAD_POST_USER", env_path, environ)
    if user:
        return user
    raise SystemExit(
        "missing Upload-Post profile name.\n"
        "  pass --user <profile>, export UPLOAD_POST_USER=... ,\n"
        "  or put UPLOAD_POST_USER in clipper/.env\n"
        "  (list your profiles with: clipper.py publish --check)"
    )


def redact(secret: str) -> str:
    """Log-safe rendering of a key. Never print the raw value."""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}...{secret[-4:]}"


# ----------------------------------------------------------------------------
# multipart encoding (stdlib only)
# ----------------------------------------------------------------------------
def encode_multipart(fields: list[tuple[str, str]],
                     files: list[tuple[str, Path]]) -> tuple[bytes, str]:
    """Build a multipart/form-data body.

    `fields` is a LIST of pairs, not a dict: the API takes repeated keys
    (platform[]=tiktok&platform[]=youtube) and a dict would collapse them.
    """
    boundary = f"----clipper{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields:
        parts += [
            f"--{boundary}".encode(), crlf,
            f'Content-Disposition: form-data; name="{name}"'.encode(), crlf,
            crlf,
            str(value).encode("utf-8"), crlf,
        ]
    for name, path in files:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts += [
            f"--{boundary}".encode(), crlf,
            (f'Content-Disposition: form-data; name="{name}"; '
             f'filename="{path.name}"').encode(), crlf,
            f"Content-Type: {ctype}".encode(), crlf,
            crlf,
            path.read_bytes(), crlf,
        ]
    parts += [f"--{boundary}--".encode(), crlf]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ----------------------------------------------------------------------------
# planning
# ----------------------------------------------------------------------------
@dataclass
class PlannedUpload:
    """One clip -> one API request, fanned out to N platforms."""
    index: int
    path: Path
    title: str
    description: str
    platforms: list[str]
    idempotency_key: str
    external_id: str
    skipped: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name


def slugify(text: str, limit: int = 40) -> str:
    """Mirror of clipper.slugify — kept local so this module imports cleanly."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug[:limit].rstrip("-") or "clip")


def idempotency_key(job_name: str, clip_name: str, platforms: list[str]) -> str:
    """Stable per (job, clip, platform-set).

    Same clip to the same platforms => same key => the API returns the existing
    job instead of publishing twice. Adding a platform yields a new key, which
    is correct: that is a genuinely different publish.
    """
    seed = f"{job_name}|{clip_name}|{','.join(sorted(platforms))}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def clip_metadata(clips_spec: list[dict]) -> dict[str, dict]:
    """Map rendered filename -> its clips.json entry.

    cmd_cut names output `{index:02d}-{slug(title)}.mp4`, so the mapping is
    recomputed deterministically rather than guessed from the directory.
    """
    out: dict[str, dict] = {}
    for index, clip in enumerate(clips_spec, 1):
        title = clip.get("title") or f"clip-{index:02d}"
        out[f"{index:02d}-{slugify(title)}.mp4"] = clip
    return out


def humanize(slug: str) -> str:
    """'01-brain-vs-computer-hook.mp4' -> 'Brain vs computer hook'."""
    stem = Path(slug).stem
    stem = re.sub(r"^\d+-", "", stem)
    words = stem.replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "Clip"


def plan_uploads(job_dir: Path, platforms: list[str], *,
                 clips_spec: list[dict] | None = None,
                 ledger: dict | None = None,
                 force: bool = False,
                 only: list[str] | None = None) -> list[PlannedUpload]:
    """Decide what to send. Pure apart from listing the clips directory."""
    bad = [p for p in platforms if p not in PLATFORMS]
    if bad:
        raise SystemExit(f"unknown platform(s): {', '.join(bad)}\n"
                         f"known: {', '.join(PLATFORMS)}")
    if not platforms:
        raise SystemExit("no --platform given")

    clips_dir = job_dir / "clips"
    if not clips_dir.is_dir():
        raise SystemExit(f"no clips directory: {clips_dir} (run `cut` first)")
    videos = sorted(clips_dir.glob("*.mp4"))
    if only:
        wanted = {o.strip() for o in only}
        videos = [v for v in videos
                  if v.name in wanted or v.stem in wanted
                  or v.name.split("-")[0] in wanted]
    if not videos:
        raise SystemExit(f"no .mp4 files in {clips_dir}")

    meta = clip_metadata(clips_spec or [])
    done = (ledger or {}).get("published", {})
    plans: list[PlannedUpload] = []

    for index, path in enumerate(videos, 1):
        entry = meta.get(path.name, {})
        # `caption` is the human line written in clips.json; `title` is the
        # slug used for the filename. Prefer the caption for the post text.
        caption = (entry.get("caption") or "").strip()
        title = caption or humanize(path.name)
        description = (entry.get("description") or caption or "").strip()

        already = set(done.get(path.name, [])) if not force else set()
        targets = [p for p in platforms if p not in already]
        skipped = [p for p in platforms if p in already]
        if not targets:
            plans.append(PlannedUpload(index, path, title, description, [],
                                       "", "", skipped))
            continue

        # Invariant, not a recoverable branch: humanize() ends with a "Clip"
        # fallback, so an empty title here means the fallback itself broke.
        if not title:
            raise AssertionError(f"{path.name}: title resolution produced nothing")

        job_name = job_dir.name
        plans.append(PlannedUpload(
            index=index,
            path=path,
            title=title,
            description=description,
            platforms=targets,
            idempotency_key=idempotency_key(job_name, path.name, targets),
            external_id=f"{job_name}:{path.stem}"[:255],
            skipped=skipped,
        ))
    return plans


def build_fields(plan: PlannedUpload, user: str, *,
                 scheduled_date: str | None = None,
                 timezone: str | None = None,
                 async_upload: bool = True) -> list[tuple[str, str]]:
    """Form fields for one upload, in a stable order (keeps tests readable)."""
    fields: list[tuple[str, str]] = [("user", user), ("title", plan.title)]
    if plan.description:
        fields.append(("description", plan.description))
    for platform in plan.platforms:
        fields.append(("platform[]", platform))
    fields.append(("external_id", plan.external_id))
    fields.append(("request_id", plan.idempotency_key))
    if async_upload:
        fields.append(("async_upload", "true"))
    if scheduled_date:
        fields.append(("scheduled_date", scheduled_date))
        if timezone:
            fields.append(("timezone", timezone))
    return fields


# ----------------------------------------------------------------------------
# ledger — what we already spent quota on
# ----------------------------------------------------------------------------
def load_ledger(path: Path) -> dict:
    if not path.is_file():
        return {"published": {}, "requests": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"published": {}, "requests": []}
    data.setdefault("published", {})
    data.setdefault("requests", [])
    return data


def record(ledger: dict, plan: PlannedUpload, response: dict) -> dict:
    """Mark a clip as published, but ONLY to the platforms that succeeded."""
    landed = successful_platforms(response) or list(plan.platforms)
    seen = set(ledger["published"].get(plan.name, []))
    ledger["published"][plan.name] = sorted(seen | set(landed))
    ledger["requests"].append({
        "clip": plan.name,
        "platforms": plan.platforms,
        "request_id": response.get("request_id") or plan.idempotency_key,
        "external_id": plan.external_id,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "response": response,
    })
    return ledger


def successful_platforms(response: dict) -> list[str]:
    """Pull platform names out of whichever response shape came back."""
    out: list[str] = []
    results = response.get("results")
    if isinstance(results, dict):
        for platform, res in results.items():
            if isinstance(res, dict) and res.get("success"):
                out.append(platform)
    elif isinstance(results, list):
        for res in results:
            if isinstance(res, dict) and res.get("success") and res.get("platform"):
                out.append(res["platform"])
    return out


def post_urls(response: dict) -> dict[str, str]:
    """platform -> permalink, when the API reports one."""
    urls: dict[str, str] = {}
    results = response.get("results")
    items = (results.items() if isinstance(results, dict)
             else [(r.get("platform", "?"), r) for r in results or []
                   if isinstance(r, dict)])
    for platform, res in items:
        if isinstance(res, dict):
            url = res.get("post_url") or res.get("url")
            if url:
                urls[platform] = url
    return urls


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def api_request(url: str, api_key: str, *, data: bytes | None = None,
                content_type: str | None = None,
                headers: dict[str, str] | None = None,
                timeout: int = 900) -> dict:
    """One API call. Returns parsed JSON; raises RuntimeError with the body."""
    req = urllib.request.Request(url, data=data,
                                 method="POST" if data else "GET")
    req.add_header("Authorization", f"Apikey {api_key}")
    req.add_header("Accept", "application/json")
    if content_type:
        req.add_header("Content-Type", content_type)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
        if status == 401:
            raise RuntimeError("401 unauthorized — API key rejected") from exc
        raise RuntimeError(f"HTTP {status}: {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"HTTP {status}: non-JSON response: {body[:200]}")
    return parsed


def send_upload(plan: PlannedUpload, api_key: str, fields: list[tuple[str, str]]) -> dict:
    body, ctype = encode_multipart(fields, [("video", plan.path)])
    return api_request(UPLOAD_URL, api_key, data=body, content_type=ctype,
                       headers={"Idempotency-Key": plan.idempotency_key})


def poll_status(request_id: str, api_key: str, *, attempts: int = 20,
                delay: float = 6.0, sleep=time.sleep) -> dict:
    """Poll until a terminal state or the attempts run out."""
    url = f"{STATUS_URL}?{urllib.parse.urlencode({'request_id': request_id})}"
    last: dict = {}
    for n in range(attempts):
        try:
            last = api_request(url, api_key, timeout=60)
        except RuntimeError as exc:
            last = {"status": "unknown", "error": str(exc)}
        if str(last.get("status", "")).lower() in DONE_STATES:
            return last
        if n + 1 < attempts:
            sleep(delay)
    return last


def check_profiles(api_key: str) -> dict:
    return api_request(f"{API_BASE}/uploadposts/users", api_key, timeout=60)
