r"""
Standalone probe for brand.py - Scribe's mark (a wave flowing into a text
cursor): the app icon, the tray icon and the dashboard's SVG.

Run from the project root:   venv\Scripts\python tests\brand_test.py

Safe: draws images in memory and writes one .ico into a temp folder; the
taskbar-theme check only READS the registry.
"""

import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import brand  # noqa: E402


def _px(img):
    """The pixels as an (n, 4) RGBA array."""
    return np.asarray(img.convert("RGBA")).reshape(-1, 4).astype(int)


def _opaque_share(img):
    return float((_px(img)[:, 3] > 128).mean())


def test_app_icon_is_a_tile_with_a_white_mark():
    for size in brand.ICO_SIZES:
        icon = brand.app_icon(size)
        assert icon.size == (size, size) and icon.mode == "RGBA"
        assert _opaque_share(icon) > 0.7, "a solid tile (big sizes keep a 4% margin)"
        r, g, b, a = icon.getpixel((size // 2, int(size * 0.09)))
        assert a > 200 and max(r, g, b) < 80, ("dark ink tile", size, (r, g, b, a))
        p = _px(icon)
        whites = int(((p[:, 3] > 200) & (p[:, :3].min(axis=1) > 200)).sum())
        assert whites > size * size * 0.04, ("a white mark on it", size)
    print("PASS  the app icon is a dark tile with a white mark, at every size.")


def test_small_sizes_are_drawn_not_shrunk():
    # A 16 px icon drawn at 16 px is crisper than 256 px scaled down: more of
    # its mark pixels are fully white.
    drawn = brand.app_icon(16)
    shrunk = brand.app_icon(256).resize((16, 16), Image.LANCZOS)

    def crisp(img):
        p = _px(img)
        return int(((p[:, 3] > 200) & (p[:, :3].min(axis=1) > 235)).sum())
    assert crisp(drawn) > crisp(shrunk), (crisp(drawn), crisp(shrunk))
    print("PASS  small icon sizes are drawn for their size (crisper than scaling down).")


def test_the_tray_mark_follows_the_taskbar_and_the_state():
    dark_bar = brand.tray_icon("idle", light_taskbar=False)
    light_bar = brand.tray_icon("idle", light_taskbar=True)

    def mean_mark(img):
        p = _px(img)
        return float(p[p[:, 3] > 200][:, :3].mean())
    assert mean_mark(dark_bar) > 200, "white mark on a dark taskbar"
    assert mean_mark(light_bar) < 60, "ink mark on a light taskbar"
    listening = brand.tray_icon("recording", light_taskbar=False)
    p = _px(listening)
    assert ((p[:, 3] > 200) & (p[:, 0] > 180) & (p[:, 1] < 110)).any(), "listening: the caret turns red"
    working = brand.tray_icon("transcribing", light_taskbar=False)
    assert _px(working)[:, 3].max() < 170, "working: the mark is dimmed"
    assert _opaque_share(dark_bar) < 0.5, "the tray mark has no tile"
    print("PASS  the tray mark is white/ink by taskbar theme; red caret listening; dim working.")


def test_the_tray_icon_carries_crisp_frames():
    img = brand.tray_icon("idle", light_taskbar=False)
    frames = img.info["frames"]
    for size in (16, 20, 24, 32, 40, 48):
        assert frames[size].size == (size, size)
    data = brand.ico_bytes(img)
    back = Image.open(__import__("io").BytesIO(data))
    assert {(16, 16), (24, 24), (32, 32)} <= set(back.info["sizes"]), back.info["sizes"]
    print("PASS  the tray icon brings its own frame for every tray size.")


def test_write_ico_has_every_size():
    path = os.path.join(tempfile.mkdtemp(prefix="scribe-brand-"), "scribe.ico")
    brand.write_ico(path)
    sizes = Image.open(path).info["sizes"]
    assert {(s, s) for s in brand.ICO_SIZES} <= set(sizes), sizes
    print("PASS  scribe.ico holds every size, each drawn for itself.")


def test_the_dashboard_mark_matches_the_logo():
    svg = brand.mark_svg()
    assert svg.startswith("<svg") and 'viewBox="0 0 24 24"' in svg
    assert 'stroke="currentColor"' in svg and 'stroke-linecap="round"' in svg
    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    marks = re.findall(r'<span class="mark">(.*?)</span>', html)
    assert len(marks) == 2 and all(m == svg for m in marks), "sidebar and welcome use brand.mark_svg()"
    print("PASS  the dashboard's mark is brand.mark_svg() - one source for the logo.")


def test_taskbar_theme_never_raises():
    assert brand.taskbar_is_light() in (True, False)
    print("PASS  reading the taskbar theme never raises.")


if __name__ == "__main__":
    test_app_icon_is_a_tile_with_a_white_mark()
    test_small_sizes_are_drawn_not_shrunk()
    test_the_tray_mark_follows_the_taskbar_and_the_state()
    test_the_tray_icon_carries_crisp_frames()
    test_write_ico_has_every_size()
    test_the_dashboard_mark_matches_the_logo()
    test_taskbar_theme_never_raises()
    print("\nAll brand tests passed.")
