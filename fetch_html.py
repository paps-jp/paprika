"""CLI entrypoint. Thin wrapper around `core.fetcher.fetch`.

All the heavy lifting lives in `core/fetcher.py` so the WebAPI can reuse it.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import nodriver as uc

from core.fetcher import (
    FetchOptions,
    clone_chrome_profile,
    default_chrome_user_data_dir,
    default_log,
    fetch,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch rendered HTML with nodriver.")
    parser.add_argument("url", help="Target URL")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="HTML output file path (prints to stdout if omitted)",
    )
    parser.add_argument(
        "-a", "--assets", type=Path,
        help="Directory to save loaded images/videos/audio (skipped if omitted)",
    )
    parser.add_argument(
        "-w", "--wait", type=int, default=20,
        help="Max seconds to wait for document.readyState=complete (default: 20)",
    )
    parser.add_argument(
        "-s", "--settle", type=float, default=0.0,
        help="Mandatory wait (sec) after document complete (default: 0)",
    )
    parser.add_argument(
        "--idle", type=float, default=3.0,
        help="Stop waiting after this many seconds of asset network silence "
             "(default: 3.0)",
    )
    parser.add_argument(
        "--max-wait", type=float, default=60.0,
        help="Hard cap (sec) on download wait after document ready (default: 60.0)",
    )
    parser.add_argument(
        "--scroll", action="store_true",
        help="After document ready, scroll the page to trigger lazy-loaded content",
    )
    parser.add_argument(
        "--scroll-step", type=int, default=50,
        help="Pixels per scroll step (default: 50)",
    )
    parser.add_argument(
        "--scroll-max", type=int, default=3000,
        help="Stop scrolling once this many pixels have been scrolled "
             "(default: 3000)",
    )
    parser.add_argument(
        "--scroll-early", type=float, default=5.0,
        help="Start scrolling even before document complete if this many seconds "
             "elapsed and the page is already scrollable (default: 5.0; 0 disables)",
    )
    parser.add_argument(
        "--download-video", action="store_true", dest="download_video",
        help="Install iframe + nested-iframe CDP deep network trace so "
             "cross-origin video players' HLS/DASH URLs are captured.",
    )
    parser.add_argument(
        "--post-click", type=float, default=5.0, dest="post_click",
        help="After pre-scroll player click, wait this many seconds before "
             "scrolling — lets the video player react / start loading the .m3u8 "
             "(default: 5.0)",
    )
    parser.add_argument(
        "--cookies-from", type=str, default=None, metavar="BROWSER",
        help="Pass --cookies-from-browser BROWSER to yt-dlp. "
             "Supported: chrome, firefox, edge, brave, opera, safari, "
             "chromium, vivaldi.",
    )
    parser.add_argument(
        "--referer", type=str, default=None, metavar="URL",
        help="Set a custom Referer header for the main navigation.",
    )

    profile_group = parser.add_mutually_exclusive_group()
    profile_group.add_argument(
        "--user-data-dir", type=Path, default=None, metavar="PATH",
        help="Use this Chrome user-data-dir (profile root). "
             "NOTE: Chrome must be closed.",
    )
    profile_group.add_argument(
        "--chrome-profile", type=str, nargs="?", const="Default",
        default=None, metavar="NAME",
        help="Use Chrome's named profile directly (default: 'Default'). "
             "Requires Chrome to be closed.",
    )
    profile_group.add_argument(
        "--clone-chrome-profile", type=str, nargs="?", const="Default",
        default=None, metavar="NAME",
        help="Clone Chrome's profile (default: 'Default') to a temp directory "
             "and use that. Lets you keep Chrome running. Changes do NOT persist.",
    )
    profile_group.add_argument(
        "--attach", type=str, default=None, metavar="[HOST:]PORT",
        help="Attach to an already-running Chrome started with "
             "--remote-debugging-port=PORT. Default host is 127.0.0.1.",
    )
    parser.add_argument(
        "--keep-open", action="store_true",
        help="Do NOT close the browser when the script finishes.",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run headless (more detectable; non-headless recommended)",
    )
    return parser


def _resolve_profile_options(args) -> tuple[Path | None, Path | None, str | None, int | None]:
    """Returns (user_data_dir, cloned_temp_dir, attach_host, attach_port).
    cloned_temp_dir is set only when we cloned (so the caller can clean up)."""
    user_data_dir: Path | None = None
    cloned_temp_dir: Path | None = None
    attach_host: str | None = None
    attach_port: int | None = None

    if args.attach is not None:
        spec = args.attach.strip()
        if ":" in spec:
            host_str, port_str = spec.rsplit(":", 1)
            attach_host = host_str or "127.0.0.1"
        else:
            attach_host = "127.0.0.1"
            port_str = spec
        attach_port = int(port_str)
    elif args.user_data_dir is not None:
        user_data_dir = args.user_data_dir
    elif args.chrome_profile is not None:
        base = default_chrome_user_data_dir()
        if base is None or not base.exists():
            raise FileNotFoundError(
                "Could not find Chrome User Data dir; "
                "--chrome-profile is unavailable."
            )
        user_data_dir = base
        if args.chrome_profile != "Default":
            cloned_temp_dir = clone_chrome_profile(args.chrome_profile)
            user_data_dir = cloned_temp_dir
        else:
            default_log(
                f"  ... using Chrome 'User Data' root: {base}  "
                f"(profile: Default)"
            )
    elif args.clone_chrome_profile is not None:
        cloned_temp_dir = clone_chrome_profile(args.clone_chrome_profile)
        user_data_dir = cloned_temp_dir

    return user_data_dir, cloned_temp_dir, attach_host, attach_port


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        user_data_dir, cloned_temp_dir, attach_host, attach_port = \
            _resolve_profile_options(args)
    except ValueError:
        print(
            f"!! --attach value invalid: '{args.attach}' "
            f"(expected PORT or HOST:PORT)",
            file=sys.stderr,
        )
        return 2
    except FileNotFoundError as e:
        print(f"!! {e}", file=sys.stderr)
        return 2

    opts = FetchOptions(
        url=args.url,
        wait_seconds=args.wait,
        settle_seconds=args.settle,
        idle_seconds=args.idle,
        max_wait_seconds=args.max_wait,
        scroll=args.scroll,
        scroll_step=args.scroll_step,
        scroll_max=args.scroll_max,
        scroll_early_after=args.scroll_early,
        post_click_seconds=args.post_click,
        download_video=getattr(args, "download_video", False),
        cookies_from=args.cookies_from,
        referer=args.referer,
        user_data_dir=user_data_dir,
        attach_host=attach_host,
        attach_port=attach_port,
        keep_open=args.keep_open,
        headless=args.headless,
        assets_dir=args.assets,
    )

    try:
        result = uc.loop().run_until_complete(fetch(opts))
    finally:
        if cloned_temp_dir is not None:
            if args.keep_open:
                print(
                    f"  ... cloned profile NOT cleaned (--keep-open in use): "
                    f"{cloned_temp_dir}\n"
                    f"      remove manually when the browser is closed.",
                    file=sys.stderr,
                )
            else:
                try:
                    shutil.rmtree(cloned_temp_dir, ignore_errors=True)
                    print(
                        f"  ... cleaned up cloned profile: {cloned_temp_dir}",
                        file=sys.stderr,
                    )
                except Exception:
                    pass

    if args.output:
        args.output.write_text(result.html, encoding="utf-8")
        print(f"Saved {len(result.html)} chars to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(result.html)

    return 0


if __name__ == "__main__":
    sys.exit(main())
