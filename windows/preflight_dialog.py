"""Tkinter dialog shown when :func:`windows.preflight.run_preflight`
returns missing dependencies.

Tk is in the Python stdlib, so this works even when pywebview is not
installed (= dev mode). No external deps.

Layout (rough)::

    +-- paprika セットアップ ----------+
    |  以下のソフトウェアが必要です:    |
    |                                  |
    |  ✗ Chromium browser              |
    |      (browser automation で必要) |
    |    [ダウンロード] [スキップ]      |
    |                                  |
    |  ✗ Visual C++ Redistributable    |
    |      ...                         |
    |    [Microsoft からダウンロード]   |
    |                                  |
    |          [再チェック] [閉じる]    |
    +----------------------------------+
"""

from __future__ import annotations

import logging
import webbrowser
from typing import Iterable

log = logging.getLogger(__name__)


def show_missing_deps_dialog(missing: Iterable) -> None:
    """Modal dialog enumerating missing prerequisites.

    Each missing dep gets a "Download" button that opens the install
    URL in the default browser. The dialog closes when the user clicks
    [閉じる]; paprika.exe then exits (caller responsibility).

    Imports tkinter inside the function so a bare CLI script that
    never hits this code path doesn't pay the tk-import cost.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        # Some PyInstaller specs strip tkinter to save 10MB. Fall back
        # to printing.
        for m in missing:
            print(
                f"[paprika] missing: {m.name} -- install from {m.install_url}"
            )
        return

    root = tk.Tk()
    root.title("paprika セットアップ")
    root.geometry("520x420")
    root.resizable(False, False)

    header = ttk.Label(
        root,
        text="paprika の動作に必要なソフトウェアが不足しています:",
        wraplength=480,
        padding=(16, 16, 16, 8),
    )
    header.pack(anchor="w")

    for dep in missing:
        frame = ttk.LabelFrame(root, text=f"  ✗ {dep.name}  ", padding=(12, 8))
        frame.pack(fill="x", padx=16, pady=4)

        ttk.Label(frame, text=dep.why, wraplength=460).pack(anchor="w")

        btn_row = ttk.Frame(frame)
        btn_row.pack(anchor="e", pady=(8, 0))

        # Capture dep.install_url at lambda definition (closure
        # variable trap otherwise binds the last iteration's value).
        def _open(url=dep.install_url):
            try:
                webbrowser.open(url)
            except Exception:
                log.exception("failed to open install URL: %s", url)

        ttk.Button(
            btn_row, text="ダウンロードページを開く", command=_open
        ).pack(side="right", padx=4)

    footer = ttk.Frame(root, padding=(16, 8))
    footer.pack(side="bottom", fill="x")

    ttk.Label(
        footer,
        text=(
            "インストール後にこのウィンドウを閉じて、もう一度 "
            "paprika.exe を起動してください。"
        ),
        wraplength=480,
        foreground="#666",
    ).pack(anchor="w", pady=(0, 8))

    ttk.Button(footer, text="閉じる", command=root.destroy).pack(side="right")

    root.mainloop()
