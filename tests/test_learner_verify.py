"""The Verifier is the single definition of "did it work" for the learning
system (learner/verify.py). These pin the rules that the 2026-11-12 audit
showed the old system got wrong:

  * "a video file exists" was success -- 48% of saved videos did not play;
  * our own infrastructure failures were counted as attempts/failures;
  * a login wall with a few logo images was a clean success.
"""
from learner import verify as V


def _ev(**kw):
    base = {
        "job_id": "j1", "ts": 1794420000.0, "url": "https://ex.com/video/123/abc-def/",
        "host": "ex.com", "status": "completed", "download_video": True,
        "capture_assets": True, "error": "",
        "assets": {"n": 0, "n_video": 0, "n_image": 0, "n_decor": 0},
        "ytdlp": {"n": 0, "fail": 0}, "video_seen": {"urls": 0, "videos": 0, "iframes": 0},
        "occlusion": {}, "facts": {},
    }
    base.update(kw)
    return base


def _media(**kw):
    m = {"name": "v.mp4", "bytes": 80_000_000, "probed": True, "playable": True,
         "has_video": True, "duration_s": 1200.0, "container": "mov,mp4,m4a"}
    m.update(kw)
    return m


def test_whole_playable_video_is_success():
    v = V.verify(_ev(assets={"n_video": 1}, facts={"media": [_media()]}))
    assert (v.outcome, v.reason, v.intent) == (V.SUCCESS, "video_ok", "video")
    assert v.version == V.VERSION and v.verified_by == "rule"


def test_video_file_that_does_not_play_is_not_success():
    bad = _media(playable=False, has_video=False, duration_s=0.0, bytes=146_000)
    v = V.verify(_ev(assets={"n_video": 1}, facts={"media": [bad]}))
    assert (v.outcome, v.reason) == (V.FAIL, "video_unplayable")


def test_short_clip_is_partial_not_success():
    v = V.verify(_ev(assets={"n_video": 1},
                     facts={"media": [_media(duration_s=30.0, bytes=3_000_000)]}))
    assert (v.outcome, v.reason) == (V.PARTIAL, "video_short")


def test_stream_fragment_is_partial():
    frag = _media(name="200459374_240p_h264_2643_R2rP_1794418490.mp4",
                  duration_s=4.0, bytes=146_164)
    v = V.verify(_ev(assets={"n_video": 1}, facts={"media": [frag]}))
    assert (v.outcome, v.reason) == (V.PARTIAL, "video_fragment")


def test_playable_without_duration_needs_size():
    big = _media(duration_s=0.0, bytes=50_000_000)
    small = _media(duration_s=0.0, bytes=400_000)
    assert V.verify(_ev(assets={"n_video": 1}, facts={"media": [big]})).reason == "video_ok_nodur"
    assert V.verify(_ev(assets={"n_video": 1}, facts={"media": [small]})).outcome == V.PARTIAL


def test_unmeasured_video_is_unknown_never_guessed():
    # Old worker (no facts) or ffprobe unavailable: size must not decide.
    v = V.verify(_ev(assets={"n_video": 1, "bytes_video": 900_000_000}))
    assert (v.outcome, v.reason) == (V.UNKNOWN, "video_unprobed")
    unprobed = _media(probed=False, playable=False, reason="ffprobe_missing")
    v2 = V.verify(_ev(assets={"n_video": 1}, facts={"media": [unprobed]}))
    assert v2.outcome == V.UNKNOWN


def test_video_seen_but_not_captured_is_fail():
    v = V.verify(_ev(video_seen={"urls": 2, "videos": 1}, ytdlp={"n": 1, "fail": 1}))
    assert (v.outcome, v.reason) == (V.FAIL, "video_not_captured")
    v2 = V.verify(_ev(facts={"deferred": {"targets": 1, "files": 0}}))
    assert v2.outcome == V.FAIL


def test_no_trace_of_video_is_na_not_fail():
    v = V.verify(_ev(assets={"n_image": 30}, video_seen={"iframes": 2}))
    assert (v.outcome, v.reason) == (V.NA, "no_video_evidence")
    assert not v.scored


def test_review_status_is_blocked_with_kind():
    v = V.verify(_ev(status="review", occlusion={"hasPassword": True, "coverage": 0.97},
                     assets={"n_image": 4}, review_reason="全面オーバーレイ"))
    assert (v.outcome, v.reason) == (V.BLOCKED, "blocked_login")
    v2 = V.verify(_ev(status="review", download_video=False, assets={"n_image": 4},
                      review_reason="全面オーバーレイでコンテンツが取得できていません (ログインフォーム, ...)"))
    assert (v2.outcome, v2.reason, v2.intent) == (V.BLOCKED, "blocked_login", "page")


def test_overlay_without_credentials_is_a_solvable_fail_not_blocked():
    # Age gate / consent / modal: 60% of the review bucket on 2026-11-12.
    # These are what a learned click sequence is FOR, so they must score as
    # failures (and feed the scheduler), not as walls we give up on.
    reason = "全面オーバーレイでコンテンツが取得できていません (モーダルダイアログ, 背景スクロールロック)"
    page = V.verify(_ev(status="review", download_video=False, assets={"n_image": 17},
                        occlusion={"ariaModal": True, "scrollLock": True, "hasPassword": False},
                        review_reason=reason))
    assert (page.outcome, page.reason) == (V.FAIL, "overlay_wall") and page.scored
    video = V.verify(_ev(status="review", occlusion={"ariaModal": True}, review_reason=reason))
    assert (video.outcome, video.reason) == (V.FAIL, "overlay_wall")


def test_a_real_video_beats_a_wall_marker():
    # The overlay was there but the download still produced the video.
    v = V.verify(_ev(status="review", occlusion={"hasPassword": True},
                     assets={"n_video": 1}, facts={"media": [_media()]}))
    assert v.outcome == V.SUCCESS


def test_infra_failures_are_never_a_learning_signal():
    for err in (
        "worker w51185 disconnected and did not finish this job within 600s (restart / crash); re-submit",
        "worker w5199 disconnected before the job finished (restart / crash / self-update)",
        "worker w5158 restarted and no longer holds this job's session",
        "worker w5012 no longer in the live fleet (orphaned by a hub/worker restart",
        "downloading exceeded 7200s and worker w5140 is gone fleet-wide; no partial save possible",
        "queued for >180s without assignment (no worker/lane available)",
        "codegen call failed: EngineThermalThrottled: engine 'qwen3-vl-4b-b' throttled",
        "codegen call failed: ConnectError: All connection attempts failed",
    ):
        v = V.verify(_ev(status="failed", error=err))
        assert v.outcome == V.INFRA, err
        assert not v.scored


def test_dead_url_and_blocked_errors():
    assert V.verify(_ev(status="failed", error="net::ERR_NAME_NOT_RESOLVED")).outcome == V.DEAD
    assert V.verify(_ev(status="failed", error="HTTP 404 Not Found")).outcome == V.DEAD
    v = V.verify(_ev(status="failed", error="HTTP 403 Forbidden (cloudflare challenge)"))
    assert (v.outcome, v.reason) == (V.BLOCKED, "blocked_challenge")
    assert V.verify(_ev(status="failed", error="boom")).outcome == V.UNKNOWN


def test_page_intent():
    ok = V.verify(_ev(download_video=False, assets={"n_image": 12, "n_decor": 3}))
    assert (ok.outcome, ok.reason, ok.intent) == (V.SUCCESS, "content_ok", "page")
    decor_only = V.verify(_ev(download_video=False, assets={"n": 3, "n_decor": 3}))
    assert (decor_only.outcome, decor_only.reason) == (V.FAIL, "no_content")
    html = V.verify(_ev(download_video=False, capture_assets=False))
    assert (html.outcome, html.reason) == (V.SUCCESS, "html_only")


def test_in_progress_and_malformed_events_do_not_raise():
    assert V.verify(_ev(status="downloading")).reason == "download_in_progress"
    assert V.verify(_ev(status="running")).outcome == V.UNKNOWN
    assert V.verify(None).outcome == V.UNKNOWN
    assert V.verify({}).outcome == V.UNKNOWN
    assert V.verify({"status": "completed", "download_video": True,
                     "facts": {"media": ["junk", None]}, "assets": None}).outcome == V.NA


def test_verify_is_pure():
    ev = _ev(assets={"n_video": 1}, facts={"media": [_media()]})
    import copy
    before = copy.deepcopy(ev)
    assert V.verify(ev) == V.verify(ev)
    assert ev == before
