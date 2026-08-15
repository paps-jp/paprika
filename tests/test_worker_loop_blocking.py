"""Profile install must not run on the worker's event loop.

Found by the loop-stall watchdog on 2026-08-15, not by reading code. Three
sampled workers reported stalls of 1.0-1.5s, and the captured stacks named the
callers directly:

    10  shutil.py copytree          <- lanes.py use_profile
     9  shutil.py _copytree
     3  tarfile.py _extract_one     <- _mix_profile.py _fetch_to_temp
     3  tarfile.py extractall
     2  gzip.py read

A Chrome profile is tens of thousands of small files. ``Path.rename`` is cheap
only within one filesystem, and the profile cache (/tmp) and the lane
(/ram/chrome) are different ones -- so the rename raises OSError every time and
the fallback copies the whole tree, synchronously, on the loop.

1.5s is not near the 120s ping timeout on its own. It matters because it is
headroom: a worker already spending seconds unable to answer has that much less
margin before a keepalive ping goes unanswered, websockets closes with 1011,
and every job that worker is running is failed as "disconnected before the job
finished". That is the exact chain that took the fleet from 0.97 to 0.50 the
same day, and this is the same class of bug the hub was fixed for earlier
(``hub-eventloop-stalls``: sync CPU/FS/HTML-parse moved to ``to_thread``).
"""

import ast
import inspect
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _fn_source(rel: str, name: str) -> str:
    """Source of one function, found by walking the AST rather than importing
    -- lanes.py pulls in Chrome/X11 plumbing that a test box has no business
    starting."""
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(
                (_ROOT / rel).read_text(encoding="utf-8"), node
            ) or ""
    raise AssertionError(f"{name} not found in {rel}")


@pytest.mark.parametrize("rel,fn,blocking", [
    ("server/worker/lanes.py", "use_profile", "shutil.copytree"),
    ("server/worker/agent/_mix_profile.py", "_fetch_to_temp", "tar.extractall"),
])
def test_the_blocking_call_is_handed_to_a_thread(rel, fn, blocking):
    src = _fn_source(rel, fn)
    assert blocking in src, f"{blocking} moved -- re-point this test"
    assert "asyncio.to_thread" in src, (
        f"{fn} runs {blocking} on the event loop; the watchdog measured "
        f"1.0-1.5s stalls from exactly this"
    )


def test_the_swap_is_atomic_within_one_thread_hop():
    """rmtree + rename + copytree must go over together. Splitting them across
    hops would leave the lane with no profile directory at all between two
    awaits, and Chrome respawns on a 2-second watchdog."""
    src = _fn_source("server/worker/lanes.py", "use_profile")
    assert src.count("asyncio.to_thread") == 1
    swap = src[src.index("def _swap"):src.index("asyncio.to_thread")]
    for op in ("rmtree", "rename", "copytree"):
        assert op in swap, f"{op} left outside the thread hop"


def test_the_tar_traversal_check_stays_with_the_extract():
    """The member-name check is a security boundary: it must run inside the
    same thread call as extractall, or a refactor could leave the extract
    running without it."""
    src = _fn_source("server/worker/agent/_mix_profile.py", "_fetch_to_temp")
    body = src[src.index("def _extract"):]
    guard = body.index("escapes extract dir")
    extract = body.index("tar.extractall")
    assert guard < extract, "traversal check must precede the extract"
    assert "asyncio.to_thread(_extract)" in src


def test_extract_failures_are_still_caught():
    """to_thread re-raises in the caller; the existing except must still wrap
    the call or a bad tarball would take the worker down instead of being
    logged and skipped."""
    src = _fn_source("server/worker/agent/_mix_profile.py", "_fetch_to_temp")
    at = src.index("asyncio.to_thread(_extract)")
    assert "try:" in src[:at][-40:], "the thread hop is not inside the try"
    assert "except Exception" in src[at:at + 120]


def test_the_chrome_reap_is_off_the_loop_at_every_async_caller():
    """``_kill_chrome_proc`` blocks up to 5s in ``proc.wait`` reaping the
    Chrome it just killed -- 5s in which the worker answers nothing, keepalive
    ping included. It was the top frame in 2 of 3 stalls remaining after the
    profile-copy fix.

    The sync form stays for ``stop()``: shutdown has no loop left to protect.
    Any NEW async caller must use the async form, which is why this counts
    call sites rather than checking one."""
    src = (_ROOT / "server/worker/lanes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    sync_callers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_kill_chrome_proc"):
                sync_callers.append(f"{node.name}:{sub.lineno}")
    assert not sync_callers, (
        f"async callers still block on the sync reap: {sync_callers}"
    )


def test_the_sync_reap_survives_for_shutdown():
    src = (_ROOT / "server/worker/lanes.py").read_text(encoding="utf-8")
    assert "def _kill_chrome_proc" in src
    assert "await asyncio.to_thread(self._kill_chrome_proc)" in src
