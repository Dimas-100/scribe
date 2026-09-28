"""
=============================================================================
 SCRIBE BRAND - the mark: a wave flowing into a text cursor.
=============================================================================

 Scribe's logo is one idea drawn in two strokes: your VOICE (one period of a
 sine wave) arriving at the text CARET where Scribe types it. Monochrome ink,
 like the rest of the app. Everything that shows the logo gets it from here,
 so it is drawn one way everywhere:

   app_icon(size)       the rounded dark tile with the white mark - windows,
                        shortcuts, scribe.ico (write_ico)
   tray_icon(state)     the bare mark for the notification area, white on a
                        dark taskbar and ink on a light one; the caret turns
                        red while you talk, the mark dims while Scribe works
   mark_svg()           the same mark as SVG, for the dashboard's sidebar
   logo_svg(dark)       the whole tile as SVG, for the README

 Small sizes are DRAWN at their size (with a slightly heavier stroke), never
 scaled down from a big picture - scaling blurs a 2-pixel line into grey mush.
 Pure drawing (PIL); taskbar_is_light() only reads the registry.
=============================================================================
"""

import io
import math
import os

from PIL import Image, ImageDraw

# --- The mark, on a 24-unit grid (the same grid as the dashboard's icons) ---
WAVE_X0, WAVE_X1 = 3.6, 12.6      # the wave: one period, starting upward
WAVE_AMP = 3.1
CARET_X = 17.2                    # the caret: a tall vertical line
CARET_TOP, CARET_BOTTOM = 4.4, 19.6

INK = (28, 28, 31)                # the app's text colour
PAPER = (250, 250, 250)
TILE_TOP, TILE_BOTTOM = (46, 46, 51), (21, 21, 24)   # a quiet top-to-bottom sheen
TILE_RADIUS = 0.29                # corner radius, share of the tile
LISTENING_RED = (229, 72, 77)     # the tray caret while you talk

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
TRAY_SIZES = (16, 20, 24, 32, 40, 48)   # 100% to 300% display scaling


def _weight(size):
    """Stroke width (grid units) for an icon of `size` pixels: finer when big,
    heavier when tiny so the line stays a solid pixel or two."""
    if size >= 128:
        return 2.2
    if size <= 20:
        return 2.8
    return 2.5


def mark_paths(points=120):
    """The mark as two polylines on the 24-unit grid - (wave, caret) - moved
    so the pair sits optically centred."""
    wave = [(WAVE_X0 + (WAVE_X1 - WAVE_X0) * i / points,
             12 - WAVE_AMP * math.sin(2 * math.pi * i / points)) for i in range(points + 1)]
    caret = [(CARET_X, CARET_TOP), (CARET_X, CARET_BOTTOM)]
    dx = 12 - ((WAVE_X0 - 1.25) + (CARET_X + 1.25)) / 2
    return ([(x + dx, y) for x, y in wave], [(x + dx, y) for x, y in caret])


def _stroke(draw, pts, width, fill):
    """A smooth line with round ends: circles stamped close together along the
    path (PIL's own thick lines have jagged joins)."""
    r = width / 2
    dense = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0) / (r * 0.2)))
        dense += [(x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps) for k in range(steps)]
    dense.append(pts[-1])
    for x, y in dense:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def _supersample(size):
    """How many times bigger to draw before scaling down (smooth edges)."""
    return max(4, 128 // size)


def app_icon(size):
    """The app icon at `size` px: a dark rounded tile with the white mark."""
    ss = _supersample(size)
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    inset = 0 if size <= 32 else big * 0.04          # big icons breathe a little
    box = (inset, inset, big - 1 - inset, big - 1 - inset)
    radius = (big - 2 * inset) * TILE_RADIUS
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    if size > 24:
        column = Image.new("RGBA", (1, big))
        for y in range(big):
            t = y / (big - 1)
            column.putpixel((0, y), tuple(int(a + (b - a) * t)
                                          for a, b in zip(TILE_TOP, TILE_BOTTOM)) + (255,))
        tile = column.resize((big, big))
    else:
        tile = Image.new("RGBA", (big, big), INK + (255,))   # tiny: flat is crisper
    img.paste(tile, (0, 0), mask)
    if size >= 32:
        # A hairline of light around the edge, so the dark tile still reads
        # on a dark desktop or taskbar. Its own layer, blended OVER the tile
        # (drawing a see-through colour straight onto the image would
        # replace the tile's pixels instead).
        edge = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ImageDraw.Draw(edge).rounded_rectangle(box, radius=radius, outline=(255, 255, 255, 30),
                                               width=ss)
        img = Image.alpha_composite(img, edge)
    draw = ImageDraw.Draw(img)
    k = (big - 2 * inset) / 24
    for pts in mark_paths():
        _stroke(draw, [(inset + x * k, inset + y * k) for x, y in pts], _weight(size) * k, PAPER)
    return img.resize((size, size), Image.LANCZOS)


def _tray_frame(size, state, light_taskbar):
    """The bare mark at `size` px for the notification area."""
    ss = _supersample(size)
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fg = INK if light_taskbar else (255, 255, 255)
    alpha = 128 if state == "transcribing" else 255
    zoom = 1.3                                        # no tile: the mark fills the icon
    k = big / 24 * zoom
    wave, caret = mark_paths()

    def place(pts):
        return [((x - 12) * k + big / 2, (y - 12) * k + big / 2) for x, y in pts]
    width = _weight(size) * 1.06 * k
    _stroke(draw, place(wave), width, fg + (alpha,))
    caret_fill = LISTENING_RED + (255,) if state == "recording" else fg + (alpha,)
    _stroke(draw, place(caret), width, caret_fill)
    return img.resize((size, size), Image.LANCZOS)


def tray_icon(state="idle", light_taskbar=False):
    """The tray picture for `state` ("idle", "recording" or "transcribing").
    Returns the 48 px image; its info["frames"] holds one frame per tray size,
    each drawn for that size (see ico_bytes)."""
    frames = {s: _tray_frame(s, state, light_taskbar) for s in TRAY_SIZES}
    img = frames[max(TRAY_SIZES)].copy()
    img.info["frames"] = frames
    return img


def _ico(frames, out):
    """Write frames {size: image} as one .ico to the file object `out`."""
    sizes = sorted(frames)
    biggest = frames[sizes[-1]]
    biggest.save(out, format="ICO", sizes=[(s, s) for s in sizes],
                 append_images=[frames[s] for s in sizes[:-1]])


def ico_bytes(img):
    """An .ico of a tray_icon() picture, with its hand-drawn frames."""
    buf = io.BytesIO()
    _ico(img.info.get("frames") or {img.width: img}, buf)
    return buf.getvalue()


def write_ico(path):
    """Write scribe.ico (every size in ICO_SIZES, each drawn for itself).
    Written to a temp file first, so a failure never leaves half an icon."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        _ico({s: app_icon(s) for s in ICO_SIZES}, f)
    os.replace(tmp, path)


def _svg_points(pts):
    return " ".join(f"{x:.2f} {y:.2f}".replace(".00", "") for x, y in pts)


def mark_svg(stroke_width=2.5):
    """The mark as inline SVG (strokes in currentColor), for the dashboard's
    sidebar tile - the page colours it for its theme."""
    wave, caret = mark_paths(points=32)
    return ('<svg viewBox="0 0 24 24" aria-hidden="true">'
            f'<path d="M{_svg_points(wave[:1])} L{_svg_points(wave[1:])}" fill="none" '
            f'stroke="currentColor" stroke-width="{stroke_width}" stroke-linecap="round" '
            'stroke-linejoin="round"/>'
            f'<path d="M{_svg_points(caret[:1])} L{_svg_points(caret[1:])}" fill="none" '
            f'stroke="currentColor" stroke-width="{stroke_width}" stroke-linecap="round"/>'
            '</svg>')


def logo_svg(dark=False):
    """The whole logo (tile + mark) as a standalone SVG for the README: the
    dark tile, or - for a dark page - a light tile with an ink mark."""
    top, bottom, mark = ((TILE_TOP, TILE_BOTTOM, PAPER) if not dark
                         else ((250, 250, 250), (228, 228, 231), INK))

    def hexc(c):
        return "#%02x%02x%02x" % c
    inner = mark_svg(2.2).split(">", 1)[1].rsplit("</svg>", 1)[0].replace("currentColor", hexc(mark))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="96" height="96">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{hexc(top)}"/><stop offset="1" stop-color="{hexc(bottom)}"/>'
            '</linearGradient></defs>'
            f'<rect x="0.5" y="0.5" width="23" height="23" rx="{23 * TILE_RADIUS:.2f}" fill="url(#g)"/>'
            f'{inner}</svg>\n')


def taskbar_is_light():
    """Does Windows draw a LIGHT taskbar (so the tray mark should be ink)?
    False when it can't tell - the default Windows taskbar is dark."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except Exception:
        return False
