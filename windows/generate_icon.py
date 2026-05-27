"""Regenerate windows/paprika.ico from docs/icon.svg.

Run whenever the source SVG changes:

    python -m windows.generate_icon

Pipeline:
  1. Render docs/icon.svg via the bundled Chromium (headless) into a
     512x512 PNG. The Chrome render gives us pixel-perfect output and
     respects the SVG's gradients / fills, unlike pure-Python SVG libs.
  2. Crop to the alpha bounding box. The source SVG places its content
     in the upper-right of a 1254x1254 viewBox (not centred), so a
     naive screencast looks shifted + tiny inside the Windows icon
     slot. The bbox crop strips the empty padding.
  3. Re-paste centred onto a 256x256 transparent canvas at 92% fill
     (= 8px margin all sides, matching Chrome / Edge / Windows-app
     conventions).
  4. Save multi-resolution ICO with the standard Windows shell sizes.

Bundled Chromium must already be downloaded (= ``paprika.exe`` was
run at least once, or run ``windows/preflight.py`` first).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "docs" / "icon.svg"
CHROME = ROOT / "data" / "chromium" / "chrome.exe"
OUT = ROOT / "windows" / "paprika.ico"
RENDER_SIZE = 512  # higher render res = cleaner downscale to small ICO frames
FILL_RATIO = 0.92  # how much of the icon slot the artwork fills


def fit_centred(img: Image.Image, target: int, fill_ratio: float) -> Image.Image:
    """Resize ``img`` to fit within ``target x target`` (preserving
    aspect) at ``fill_ratio`` of the slot, centred on a transparent
    canvas."""
    canvas = Image.new("RGBA", (target, target), (0, 0, 0, 0))
    inner = int(target * fill_ratio)
    w, h = img.size
    scale = min(inner / w, inner / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    canvas.paste(resized, ((target - nw) // 2, (target - nh) // 2), resized)
    return canvas


def main() -> int:
    if not SVG.exists():
        print(f"!! source SVG not found: {SVG}")
        return 1
    if not CHROME.exists():
        print(
            f"!! bundled Chromium not found at {CHROME}. "
            f"Run paprika.exe once (= triggers preflight download) and retry."
        )
        return 2

    tmp_png = ROOT / "icon-render-tmp.png"
    try:
        subprocess.run(
            [
                str(CHROME), "--headless", "--hide-scrollbars",
                "--disable-gpu", "--no-sandbox",
                f"--screenshot={tmp_png}",
                f"--window-size={RENDER_SIZE},{RENDER_SIZE}",
                "--default-background-color=00000000",
                "file:///" + str(SVG.resolve()).replace("\\", "/"),
            ],
            check=True,
            capture_output=True,
        )

        src = Image.open(tmp_png).convert("RGBA")
        bbox = src.getbbox()
        if bbox is None:
            print("!! rendered PNG is fully transparent -- SVG render failed")
            return 3
        cropped = src.crop(bbox)
        print(f"src {src.size} → bbox {bbox} → cropped {cropped.size}")

        centred = fit_centred(cropped, 256, FILL_RATIO)
        centred.save(
            OUT, format="ICO",
            sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)],
        )
        print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
        return 0
    finally:
        if tmp_png.exists():
            tmp_png.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
