"""Unit tests for the Upload-Post publishing backend.

No network: every HTTP boundary is stubbed. Run:
    python -m pytest test_uploadpost.py -q

The quota is 10 uploads/month on the free plan, so the tests that matter most
are the ones proving we do NOT send: dry-run by default, skip what the ledger
says already landed, and honour --max-uploads.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import uploadpost as up  # noqa: E402
from clipper import build_parser  # noqa: E402

# Bound before the autouse fixture below stubs it out, so the credential tests
# can exercise the real resolver while everything else gets the stub.
REAL_LOAD_USER = up.load_user


# --- fixtures ---------------------------------------------------------------
@pytest.fixture
def job(tmp_path):
    """A job dir with three rendered clips, named exactly as cmd_cut names them."""
    clips_dir = tmp_path / "job-abc" / "clips"
    clips_dir.mkdir(parents=True)
    for name in ("01-hook.mp4", "02-middle-bit.mp4", "03-payoff.mp4"):
        (clips_dir / name).write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    return tmp_path / "job-abc"


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    """Keep tests off the real .env and the real environment.

    Without this, cmd_publish would resolve UPLOAD_POST_USER from the
    developer's own .env and the suite would pass or fail depending on whose
    machine it runs on.
    """
    monkeypatch.delenv("UPLOAD_POST_API_KEY", raising=False)
    monkeypatch.delenv("UPLOAD_POST_USER", raising=False)
    monkeypatch.setattr(up, "load_user", lambda *a, **k: "test-profile")


SPEC = [
    {"title": "hook", "start": 0, "end": 30, "caption": "Otak kamu kenal angka 3."},
    {"title": "middle bit", "start": 60, "end": 90},
    {"title": "payoff", "start": 120, "end": 150, "caption": "Ini lapisan pertama."},
]


# --- env / credentials ------------------------------------------------------
def test_parse_env_file_handles_comments_quotes_and_export():
    text = "# comment\nUPLOAD_POST_API_KEY='abc123'\nexport OTHER=\"x y\"\nBAD_LINE\n"
    parsed = up.parse_env_file(text)
    assert parsed["UPLOAD_POST_API_KEY"] == "abc123"
    assert parsed["OTHER"] == "x y"
    assert "BAD_LINE" not in parsed


def test_load_api_key_prefers_environment(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("UPLOAD_POST_API_KEY=from-file\n")
    assert up.load_api_key(env_file, {"UPLOAD_POST_API_KEY": "from-env"}) == "from-env"


def test_load_api_key_falls_back_to_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("UPLOAD_POST_API_KEY=from-file\n")
    assert up.load_api_key(env_file, {}) == "from-file"


def test_load_api_key_missing_raises(tmp_path):
    with pytest.raises(SystemExit):
        up.load_api_key(tmp_path / "nope.env", {})


def test_load_user_prefers_environment(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("UPLOAD_POST_USER=from-file\n")
    assert REAL_LOAD_USER(env_file, {"UPLOAD_POST_USER": "from-env"}) == "from-env"


def test_load_user_falls_back_to_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("UPLOAD_POST_USER=mybrand\n")
    assert REAL_LOAD_USER(env_file, {}) == "mybrand"


def test_load_user_missing_raises(tmp_path):
    with pytest.raises(SystemExit):
        REAL_LOAD_USER(tmp_path / "nope.env", {})


def test_load_setting_returns_empty_when_unset(tmp_path):
    """Unlike the loaders, load_setting is not fatal — callers decide."""
    assert up.load_setting("NOPE", tmp_path / "absent.env", {}) == ""


def test_an_empty_value_in_env_counts_as_unset(tmp_path):
    """A freshly scaffolded .env has `UPLOAD_POST_API_KEY=` — must not pass."""
    env_file = tmp_path / ".env"
    env_file.write_text("UPLOAD_POST_API_KEY=\nUPLOAD_POST_USER=\n")
    with pytest.raises(SystemExit):
        up.load_api_key(env_file, {})
    with pytest.raises(SystemExit):
        REAL_LOAD_USER(env_file, {})


def test_redact_never_leaks_the_middle():
    key = "sk-abcdefghijklmnop"
    out = up.redact(key)
    assert "cdefghijklm" not in out
    assert out.startswith("sk-a") and out.endswith("mnop")


def test_redact_short_secret_is_fully_masked():
    assert up.redact("tiny") == "***"


# --- multipart --------------------------------------------------------------
def test_encode_multipart_repeats_platform_keys(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"bytes")
    fields = [("user", "me"), ("platform[]", "tiktok"), ("platform[]", "youtube")]
    body, ctype = up.encode_multipart(fields, [("video", video)])
    assert ctype.startswith("multipart/form-data; boundary=")
    # A dict would have collapsed these two into one.
    assert body.count(b'name="platform[]"') == 2
    assert b"tiktok" in body and b"youtube" in body
    assert b'filename="clip.mp4"' in body
    assert b"video/mp4" in body
    assert b"bytes" in body


def test_encode_multipart_body_is_properly_terminated(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    body, ctype = up.encode_multipart([("user", "me")], [("video", video)])
    boundary = ctype.split("boundary=")[1]
    assert body.endswith(f"--{boundary}--\r\n".encode())


# --- planning ---------------------------------------------------------------
def test_plan_uploads_maps_captions_to_titles(job):
    plans = up.plan_uploads(job, ["tiktok"], clips_spec=SPEC)
    assert [p.name for p in plans] == ["01-hook.mp4", "02-middle-bit.mp4",
                                       "03-payoff.mp4"]
    assert plans[0].title == "Otak kamu kenal angka 3."


def test_plan_uploads_falls_back_to_humanized_filename(job):
    plans = up.plan_uploads(job, ["tiktok"], clips_spec=SPEC)
    # Clip 2 has no caption in the spec.
    assert plans[1].title == "Middle bit"


def test_plan_uploads_works_without_any_spec(job):
    plans = up.plan_uploads(job, ["tiktok"], clips_spec=[])
    assert [p.title for p in plans] == ["Hook", "Middle bit", "Payoff"]


def test_plan_uploads_rejects_unknown_platform(job):
    with pytest.raises(SystemExit):
        up.plan_uploads(job, ["myspace"], clips_spec=SPEC)


def test_plan_uploads_rejects_empty_platform_list(job):
    with pytest.raises(SystemExit):
        up.plan_uploads(job, [], clips_spec=SPEC)


def test_plan_uploads_requires_a_clips_dir(tmp_path):
    with pytest.raises(SystemExit):
        up.plan_uploads(tmp_path, ["tiktok"], clips_spec=SPEC)


def test_plan_uploads_only_filter_accepts_name_stem_and_index(job):
    for selector in ("02-middle-bit.mp4", "02-middle-bit", "02"):
        plans = up.plan_uploads(job, ["tiktok"], clips_spec=SPEC, only=[selector])
        assert [p.name for p in plans] == ["02-middle-bit.mp4"], selector


# --- the quota guards -------------------------------------------------------
def test_ledger_skips_already_published_clips(job):
    ledger = {"published": {"01-hook.mp4": ["tiktok"]}, "requests": []}
    plans = up.plan_uploads(job, ["tiktok"], clips_spec=SPEC, ledger=ledger)
    assert plans[0].platforms == []          # nothing to send
    assert plans[0].skipped == ["tiktok"]
    assert plans[1].platforms == ["tiktok"]  # untouched clips still go


def test_ledger_skips_only_the_platform_already_done(job):
    """A clip on TikTok but not YouTube must still go to YouTube — and only there."""
    ledger = {"published": {"01-hook.mp4": ["tiktok"]}, "requests": []}
    plans = up.plan_uploads(job, ["tiktok", "youtube"], clips_spec=SPEC,
                            ledger=ledger)
    assert plans[0].platforms == ["youtube"]
    assert plans[0].skipped == ["tiktok"]


def test_force_overrides_the_ledger(job):
    ledger = {"published": {"01-hook.mp4": ["tiktok"]}, "requests": []}
    plans = up.plan_uploads(job, ["tiktok"], clips_spec=SPEC, ledger=ledger,
                            force=True)
    assert plans[0].platforms == ["tiktok"]


def test_every_clip_always_gets_a_title_even_with_an_unnameable_file(job):
    """YouTube/Reddit reject a titleless upload; the fallback chain prevents it."""
    for leftover in ("01-hook.mp4", "02-middle-bit.mp4", "03-payoff.mp4"):
        (job / "clips" / leftover).unlink()
    # A name that slugifies to nothing — the worst case for title resolution.
    (job / "clips" / "01-.mp4").write_bytes(b"\x00")
    plans = up.plan_uploads(job, ["youtube"], clips_spec=[{"title": "", "start": 0}])
    assert plans[0].title == "Clip"
    assert all(p.title for p in plans)


def test_title_required_platforms_are_declared():
    assert up.TITLE_REQUIRED == {"youtube", "reddit"}


def test_idempotency_key_is_stable_for_the_same_request():
    a = up.idempotency_key("job-abc", "01-hook.mp4", ["tiktok", "youtube"])
    b = up.idempotency_key("job-abc", "01-hook.mp4", ["youtube", "tiktok"])
    assert a == b, "platform order must not change the key"


def test_idempotency_key_changes_with_clip_job_and_platforms():
    base = up.idempotency_key("job-abc", "01-hook.mp4", ["tiktok"])
    assert base != up.idempotency_key("job-xyz", "01-hook.mp4", ["tiktok"])
    assert base != up.idempotency_key("job-abc", "02-other.mp4", ["tiktok"])
    assert base != up.idempotency_key("job-abc", "01-hook.mp4", ["tiktok", "x"])


# --- form fields ------------------------------------------------------------
def _fields(job, **kwargs):
    plan = up.plan_uploads(job, ["tiktok", "youtube"], clips_spec=SPEC)[0]
    return dict(up.build_fields(plan, "mybrand", **kwargs)), plan


def test_build_fields_sets_the_required_parameters(job):
    fields, plan = _fields(job)
    assert fields["user"] == "mybrand"
    assert fields["title"] == "Otak kamu kenal angka 3."
    assert fields["request_id"] == plan.idempotency_key
    assert fields["external_id"] == "job-abc:01-hook"


def test_build_fields_repeats_platforms_as_pairs(job):
    plan = up.plan_uploads(job, ["tiktok", "youtube"], clips_spec=SPEC)[0]
    pairs = up.build_fields(plan, "mybrand")
    platforms = [v for k, v in pairs if k == "platform[]"]
    assert platforms == ["tiktok", "youtube"]


def test_build_fields_async_by_default_and_schedule_is_opt_in(job):
    fields, _ = _fields(job)
    assert fields["async_upload"] == "true"
    assert "scheduled_date" not in fields

    fields, _ = _fields(job, scheduled_date="2026-09-20T18:00:00",
                        timezone="Asia/Jakarta")
    assert fields["scheduled_date"] == "2026-09-20T18:00:00"
    assert fields["timezone"] == "Asia/Jakarta"


def test_build_fields_sync_mode_drops_the_async_flag(job):
    fields, _ = _fields(job, async_upload=False)
    assert "async_upload" not in fields


# --- response parsing -------------------------------------------------------
DICT_RESPONSE = {
    "request_id": "req-1",
    "results": {
        "tiktok": {"success": True, "post_url": "https://tiktok.com/@me/1"},
        "youtube": {"success": False, "message": "quota"},
    },
}
LIST_RESPONSE = {
    "request_id": "req-2",
    "results": [
        {"platform": "instagram", "success": True, "post_url": "https://ig/1"},
        {"platform": "threads", "success": False},
    ],
}


@pytest.mark.parametrize("response,expected", [
    (DICT_RESPONSE, ["tiktok"]),
    (LIST_RESPONSE, ["instagram"]),
    ({"results": {}}, []),
    ({}, []),
])
def test_successful_platforms_handles_both_shapes(response, expected):
    assert up.successful_platforms(response) == expected


def test_post_urls_extracted_from_both_shapes():
    assert up.post_urls(DICT_RESPONSE)["tiktok"] == "https://tiktok.com/@me/1"
    assert up.post_urls(LIST_RESPONSE)["instagram"] == "https://ig/1"


def test_record_only_credits_platforms_that_actually_succeeded(job):
    """A failed YouTube leg must stay un-ledgered so a re-run retries it."""
    plan = up.plan_uploads(job, ["tiktok", "youtube"], clips_spec=SPEC)[0]
    ledger = up.record(up.load_ledger(job / "nothing.json"), plan, DICT_RESPONSE)
    assert ledger["published"]["01-hook.mp4"] == ["tiktok"]

    plans = up.plan_uploads(job, ["tiktok", "youtube"], clips_spec=SPEC,
                            ledger=ledger)
    assert plans[0].platforms == ["youtube"]


def test_record_with_no_parsable_results_credits_the_whole_attempt(job):
    """Async 202s carry no per-platform results; assume sent, never resend blind."""
    plan = up.plan_uploads(job, ["tiktok"], clips_spec=SPEC)[0]
    ledger = up.record({"published": {}, "requests": []}, plan,
                       {"request_id": "r", "success": True})
    assert ledger["published"]["01-hook.mp4"] == ["tiktok"]


def test_record_appends_an_audit_trail(job):
    plan = up.plan_uploads(job, ["tiktok"], clips_spec=SPEC)[0]
    ledger = up.record({"published": {}, "requests": []}, plan, DICT_RESPONSE)
    entry = ledger["requests"][0]
    assert entry["clip"] == "01-hook.mp4"
    assert entry["request_id"] == "req-1"
    assert entry["external_id"] == "job-abc:01-hook"


def test_load_ledger_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "publish.json"
    path.write_text("{not json")
    assert up.load_ledger(path) == {"published": {}, "requests": []}


def test_load_ledger_missing_file_is_empty(tmp_path):
    assert up.load_ledger(tmp_path / "absent.json")["published"] == {}


# --- polling ----------------------------------------------------------------
def test_poll_status_stops_at_a_terminal_state(monkeypatch):
    calls = []

    def fake_request(url, key, **kwargs):
        calls.append(url)
        return {"status": "processing"} if len(calls) < 3 else {"status": "completed"}

    monkeypatch.setattr(up, "api_request", fake_request)
    result = up.poll_status("req-1", "key", attempts=10, delay=0, sleep=lambda _: None)
    assert result["status"] == "completed"
    assert len(calls) == 3


def test_poll_status_gives_up_after_the_attempt_budget(monkeypatch):
    monkeypatch.setattr(up, "api_request", lambda *a, **k: {"status": "processing"})
    result = up.poll_status("r", "key", attempts=3, delay=0, sleep=lambda _: None)
    assert result["status"] == "processing"


def test_poll_status_survives_a_transient_error(monkeypatch):
    state = {"n": 0}

    def flaky(url, key, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("network error: timed out")
        return {"status": "completed"}

    monkeypatch.setattr(up, "api_request", flaky)
    assert up.poll_status("r", "k", delay=0, sleep=lambda _: None)["status"] == "completed"


def test_poll_status_sends_the_request_id_as_a_query_param(monkeypatch):
    seen = {}
    monkeypatch.setattr(up, "api_request",
                        lambda url, key, **kw: seen.setdefault("url", url)
                        and None or {"status": "completed"})
    up.poll_status("req-xyz", "key", sleep=lambda _: None)
    assert "request_id=req-xyz" in seen["url"]


# --- CLI wiring -------------------------------------------------------------
def test_publish_defaults_to_a_dry_run():
    args = build_parser().parse_args(
        ["publish", "work/job", "--user", "me", "--platform", "tiktok"])
    assert args.yes is False, "publishing must never be the default"
    assert args.force is False
    assert args.max_uploads is None


def test_publish_accepts_repeated_platforms_and_yes():
    args = build_parser().parse_args(
        ["publish", "work/job", "--user", "me",
         "--platform", "tiktok", "--platform", "youtube",
         "--max-uploads", "2", "--yes"])
    assert args.platform == ["tiktok", "youtube"]
    assert args.max_uploads == 2
    assert args.yes is True


def test_publish_user_is_optional_on_the_cli():
    """It lives in .env; requiring it on every run was needless friction."""
    args = build_parser().parse_args(
        ["publish", "work/job", "--platform", "tiktok"])
    assert args.user is None


def test_publish_user_flag_still_overrides():
    args = build_parser().parse_args(
        ["publish", "work/job", "--user", "other", "--platform", "tiktok"])
    assert args.user == "other"


def test_publish_check_needs_no_job_dir():
    args = build_parser().parse_args(["publish", "--check"])
    assert args.check is True
    assert args.job_dir is None


def test_publish_without_a_job_dir_or_check_is_refused(job, monkeypatch):
    from clipper import cmd_publish
    monkeypatch.setattr(up, "load_user", lambda *a, **k: "me")
    args = build_parser().parse_args(["publish", "--platform", "tiktok"])
    with pytest.raises(SystemExit):
        cmd_publish(args)


def test_publish_schedule_and_timezone_parse():
    args = build_parser().parse_args(
        ["publish", "work/job", "--user", "me", "--platform", "tiktok",
         "--schedule", "2026-09-20T18:00:00", "--timezone", "Asia/Jakarta"])
    assert args.schedule == "2026-09-20T18:00:00"
    assert args.timezone == "Asia/Jakarta"


# --- end-to-end dry run (no network) ----------------------------------------
def test_dry_run_sends_nothing_and_writes_no_ledger(job, monkeypatch, capsys):
    from clipper import cmd_publish

    def explode(*a, **k):
        raise AssertionError("dry run must not touch the network")

    monkeypatch.setattr(up, "send_upload", explode)
    monkeypatch.setattr(up, "api_request", explode)

    args = build_parser().parse_args(
        ["publish", str(job), "--user", "me", "--platform", "tiktok"])
    assert cmd_publish(args) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert not (job / "publish.json").exists()


def test_yes_sends_and_records_each_clip(job, monkeypatch, capsys):
    from clipper import cmd_publish

    sent = []

    def fake_send(plan, key, fields):
        sent.append((plan.name, plan.platforms))
        return {"request_id": f"req-{plan.index}",
                "results": {"tiktok": {"success": True,
                                       "post_url": f"https://t/{plan.index}"}}}

    monkeypatch.setattr(up, "send_upload", fake_send)
    monkeypatch.setattr(up, "load_api_key", lambda *a, **k: "test-key")
    monkeypatch.setattr(up, "poll_status", lambda *a, **k: {"status": "completed"})

    args = build_parser().parse_args(
        ["publish", str(job), "--user", "me", "--platform", "tiktok", "--yes"])
    assert cmd_publish(args) == 0
    assert len(sent) == 3

    ledger = json.loads((job / "publish.json").read_text())
    assert set(ledger["published"]) == {"01-hook.mp4", "02-middle-bit.mp4",
                                        "03-payoff.mp4"}


def test_max_uploads_caps_what_is_spent(job, monkeypatch):
    from clipper import cmd_publish

    sent = []
    monkeypatch.setattr(up, "send_upload",
                        lambda plan, key, fields: sent.append(plan.name)
                        or {"request_id": "r", "results": {}})
    monkeypatch.setattr(up, "load_api_key", lambda *a, **k: "k")
    monkeypatch.setattr(up, "poll_status", lambda *a, **k: {"status": "completed"})

    args = build_parser().parse_args(
        ["publish", str(job), "--user", "me", "--platform", "tiktok",
         "--max-uploads", "1", "--yes"])
    cmd_publish(args)
    assert sent == ["01-hook.mp4"], "must stop at the cap"


def test_rerun_after_success_spends_nothing(job, monkeypatch):
    """The whole point of the ledger: a second run is free."""
    from clipper import cmd_publish

    monkeypatch.setattr(up, "load_api_key", lambda *a, **k: "k")
    monkeypatch.setattr(up, "poll_status", lambda *a, **k: {"status": "completed"})
    monkeypatch.setattr(up, "send_upload", lambda plan, key, fields: {
        "request_id": "r",
        "results": {"tiktok": {"success": True, "post_url": "https://t/1"}}})

    argv = ["publish", str(job), "--user", "me", "--platform", "tiktok", "--yes"]
    cmd_publish(build_parser().parse_args(argv))

    def explode(*a, **k):
        raise AssertionError("re-run must not resend an already-published clip")

    monkeypatch.setattr(up, "send_upload", explode)
    assert cmd_publish(build_parser().parse_args(argv)) == 0


def test_a_failed_send_does_not_enter_the_ledger(job, monkeypatch):
    from clipper import cmd_publish

    monkeypatch.setattr(up, "load_api_key", lambda *a, **k: "k")
    monkeypatch.setattr(up, "send_upload", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("HTTP 500: boom")))

    args = build_parser().parse_args(
        ["publish", str(job), "--user", "me", "--platform", "tiktok", "--yes"])
    assert cmd_publish(args) == 1
    ledger = up.load_ledger(job / "publish.json")
    assert ledger["published"] == {}
