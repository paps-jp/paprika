"""Initial-setup dialogs shown on the very first paprika.exe launch.

Currently asks one question: should the bundled Chromium open with
its window visible (GUI) or hidden (headless)? See
:func:`ask_chrome_visibility`.

All dialogs use stdlib tkinter so paprika.exe doesn't need extra
runtime dependencies. They're blocking (= ``mainloop`` waits for
the operator to click a button) so the rest of the startup sequence
can simply read the persisted setting once the function returns.

Why a custom dialog instead of a checkbox in the admin UI:

  * The operator hasn't seen the admin UI yet at this point
  * The first-time decision impacts whether they SEE Chrome flash
    on the desktop, which is the first impression of paprika
  * Picking headless by default + letting people opt-in to GUI
    later via Settings would also work, but explicit asking once
    sets the right expectation
"""

from __future__ import annotations

import logging
from typing import Literal

log = logging.getLogger(__name__)


def ask_chrome_visibility() -> Literal["headless", "gui"] | None:
    """Modal first-run dialog.

    Returns:
      * ``"headless"`` -- operator picked "隠す" (Chrome ウィンドウ非表示)
      * ``"gui"``      -- operator picked "表示する" (デスクトップに出す)
      * ``None``       -- operator closed the dialog without picking
                          (= treat as headless default, the safer
                          of the two for non-engineers)

    Blocks until the operator clicks a button or closes the window.
    Imports tkinter inside the function so the bare CLI tests (= no
    DISPLAY) don't pay the tk-import cost just by importing this
    module.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        # No tkinter at all (= unlikely on Windows, possible in
        # stripped PyInstaller builds). Fall back to headless.
        log.warning("tkinter unavailable -- defaulting to headless")
        return "headless"

    choice: dict[str, str] = {}

    def _pick(value: str, root: "tk.Tk"):
        choice["value"] = value
        root.destroy()

    root = tk.Tk()
    root.title("paprika 初回設定")
    root.geometry("540x360")
    root.resizable(False, False)
    # Stay on top so the dialog doesn't get buried behind the
    # pywebview window that starts a moment later.
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    # ---- Header --------------------------------------------------
    header = ttk.Label(
        root,
        text="paprika のブラウザを画面に表示しますか？",
        font=("Segoe UI", 13, "bold"),
        padding=(20, 18, 20, 6),
    )
    header.pack(anchor="w")
    desc = ttk.Label(
        root,
        text=(
            "paprika が裏で操作する Chrome ウィンドウについて、起動時の"
            "見た目を選んでください。"
            "あとで [Settings] → [worker_chrome_headless] からいつでも"
            "変更できます。"
        ),
        wraplength=500,
        foreground="#555",
        padding=(20, 0, 20, 12),
    )
    desc.pack(anchor="w")

    # ---- Option 1: GUI -----------------------------------------
    opt_gui = ttk.LabelFrame(root, text="  表示する (GUI モード)  ", padding=(12, 8))
    opt_gui.pack(fill="x", padx=20, pady=4)
    ttk.Label(
        opt_gui,
        text=(
            "デスクトップに Chrome ウィンドウが出ます。"
            "ジョブ実行中の様子をそのまま画面で見られるのでデバッグ向け。"
        ),
        wraplength=460,
    ).pack(anchor="w")
    ttk.Button(
        opt_gui,
        text="表示する を選ぶ",
        command=lambda: _pick("gui", root),
    ).pack(anchor="e", pady=(8, 0))

    # ---- Option 2: headless (recommended) ----------------------
    opt_hl = ttk.LabelFrame(
        root, text="  隠す (headless モード) ── おすすめ  ", padding=(12, 8)
    )
    opt_hl.pack(fill="x", padx=20, pady=4)
    ttk.Label(
        opt_hl,
        text=(
            "Chrome ウィンドウは出ません (作業中のデスクトップを邪魔しない)。"
            "ジョブ実行中の様子は paprika UI の [Live] タブから "
            "screencast viewer で確認できます。"
        ),
        wraplength=460,
    ).pack(anchor="w")
    ttk.Button(
        opt_hl,
        text="隠す を選ぶ",
        command=lambda: _pick("headless", root),
    ).pack(anchor="e", pady=(8, 0))

    # Center on screen.
    root.update_idletasks()
    w = root.winfo_width()
    h = root.winfo_height()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    root.mainloop()
    return choice.get("value") or None  # type: ignore[return-value]
