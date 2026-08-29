"""Regression: don't spawn yt-dlp for video URLs already seen to be tiny.

2026-08-16 measurement. A fleet-wide slowdown showed the same hosts taking a
median 2.7x longer than four days earlier, with CPU, memory, disk and egress
bandwidth all idle. Tracing one job's worker log found 56s of wall time of
which 38s was yt-dlp: seven spawns, each ~3.5s of process + generic-extractor
startup for a 100-200 KB file. Across three workers, 74-84% of the video
downloads that COMPLETED were under 5 MB -- ad bumpers and ``*_9s.mp4``
hover-preview clips, the same creatives recurring across unrelated jobs -- and
the deferred downloader runs them strictly serially. One job spawned 17.

The bytes were already being thrown away: video output below min_asset_size is
discarded AFTER download. The gate applies that same policy before the spawn.

What must not regress is the set of things it refuses to judge. A manifest is a
few KB that expands into the entire stream; a HostRecipe can register the page
URL itself as a target; and a target that never crossed the network sniffer has
no observed size at all. Gating any of those on "small on the wire" would drop
real videos, which is far worse than the overhead this removes.
"""

import pytest

from core.fetcher import video_target_too_small

TWO_MB = 2 * 1024 * 1024


@pytest.mark.parametrize(
    "url,size",
    [
        # The exact shapes pulled from worker logs on 2026-08-16.
        ("https://n19s.1024cdn.sx/tmv/7041_9s.mp4", 77_546),
        ("https://n19s.1024cdn.sx/tmv/7161_9s.mp4", 177_543),
        ("https://playhubconnect.com/bn/304/20f/78e/30420f78.mp4", 248_722),
        # Recurring ad creative, ~0.79 MB, seen across unrelated jobs.
        ("https://cdn.example.net/creative/bumper.webm", 828_400),
        # Query strings must not defeat the extension match.
        ("https://cdn.example.net/preview.mp4?token=abc123", 120_000),
    ],
)
def test_tiny_direct_video_is_skipped(url, size):
    assert video_target_too_small(url, size, TWO_MB) is True


@pytest.mark.parametrize(
    "url,size",
    [
        # Anything at or above the threshold is a plausible real video.
        ("https://cdn.example.net/movie.mp4", TWO_MB),
        ("https://cdn.example.net/movie.mp4", 91 * 1024 * 1024),
    ],
)
def test_real_sized_video_is_kept(url, size):
    assert video_target_too_small(url, size, TWO_MB) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.net/master.m3u8",
        "https://cdn.example.net/master.m3u8?psch=v2&pkey=NTK9",
        "https://cdn.example.net/manifest.mpd",
        "https://cdn.example.net/manifest.mpd?t=1",
    ],
)
def test_manifest_is_never_gated_on_its_own_size(url):
    """An HLS/DASH manifest is a few KB by definition -- its byte count says
    nothing about the stream behind it. Gating these would kill exactly the
    targets the whole video pipeline is built around."""
    assert video_target_too_small(url, 1_200, TWO_MB) is False


def test_recipe_page_url_is_never_gated():
    """HostRecipe registers "feed this page URL straight to yt-dlp" for sites
    where the video isn't discoverable from the network log. The page's own
    HTML is small; the video behind it is not."""
    assert video_target_too_small(
        "https://site.example/video/12345", 4_096, TWO_MB
    ) is False
    assert video_target_too_small(
        "https://site.example/embed/abc", 8_192, TWO_MB
    ) is False


@pytest.mark.parametrize("size", [None, 0, -1, "77546"])
def test_unobserved_size_falls_through_to_old_behaviour(size):
    """No size on the wire (target came from the DOM, or the response carried
    no length) means we know nothing -- proceed as before rather than guess."""
    assert video_target_too_small(
        "https://cdn.example.net/clip.mp4", size, TWO_MB
    ) is False


@pytest.mark.parametrize("min_bytes", [0, -1])
def test_gate_is_disablable(min_bytes):
    """PAPRIKA_MIN_VIDEO_BYTES=0 restores the pre-2026-08-16 behaviour."""
    assert video_target_too_small(
        "https://n19s.1024cdn.sx/tmv/7041_9s.mp4", 77_546, min_bytes
    ) is False
