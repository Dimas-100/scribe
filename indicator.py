"""
=============================================================================
 SCRIBE INDICATOR - the capsule that appears while you dictate.
=============================================================================

 Scribe's mark, brought to life. Press the hotkey and a small dark capsule
 opens at the bottom of your screen around a text CARET, and a line draws out
 of it. While you talk the line becomes a smooth wave flowing INTO the caret,
 as tall as your voice is loud. Let go and the wave settles into a straight
 line (your speech became text) with a soft light gliding along it while
 Scribe works; when the text lands, the line slips into the caret, the
 capsule folds around it and fades away. Idle, nothing is on screen.

 Three parts, so each can be tested on its own:

   Motion          WHAT the capsule looks like at any moment - pure maths.
                   set_state(state, now) and step(now, voice_level) -> a
                   Frame (or None: nothing to show). Every property glides
                   by time, and a new state always starts from where the
                   capsule IS (press again mid-close: it just re-opens).
   render()        a Frame -> the picture (PIL, RGBA), for a display scale.
   LayeredWindow   shows that picture on screen: a Windows "layered" window
                   with a real alpha channel - smooth edges and a soft
                   shadow over any background, fades done by Windows
                   itself. Click-through, never takes focus, always on top.

 app.py drives it from the Tk main thread about 60 times a second while it
 is visible (see _indicator_tick) and stops completely when it's hidden.
=============================================================================
"""

import ctypes
import math
import sys
from collections import OrderedDict, namedtuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# --- Sizes, in 100%-scaling pixels (multiplied by the display scale) --------
CAP_W, CAP_H = 104, 32     # the open capsule
DOT = 32                   # closed: a circle around the caret
MARGIN = 18                # room around the capsule for its soft shadow
OVERSHOOT_ROOM = 1.06      # the window is a little wider: the opening springs
BOTTOM_GAP = 24            # capsule bottom above the taskbar

CARET_INSET = DOT / 2      # caret centre from the capsule's right edge (the
                           # dot's centre - so the closed dot is centred on it)
CARET_W, CARET_H = 2.0, 14
LINE_LEFT = 15             # the line starts this far inside the left edge...
LINE_GAP = 8               # ...and ends this far before the caret
LINE_W = 2.0
WAVE_H = 8.5               # the tallest wave, up or down
WAVE_PERIODS = 1.5

# --- Colours -----------------------------------------------------------------
FILL = (22, 22, 24, 246)           # near-black, very slightly see-through
EDGE = (255, 255, 255, 24)         # a hairline, so it reads on dark screens
INK = (246, 246, 248)              # the line and the caret
CARET = INK
CARET_WARN = (236, 178, 92)        # the Groq daily quota is 70% used
CARET_DANGER = (238, 112, 122)     # ...90% used
SHADOW = (0, 0, 0, 72)
SHADOW_BLUR, SHADOW_DROP = 7, 3

# --- Motion --------------------------------------------------------------------
LEVEL_GAIN, LEVEL_CURVE = 12.0, 0.7   # mic RMS -> 0..1 (speech sits mid-range)
LEVEL_FLOOR = 0.08                    # listening: never dead flat
ATTACK, RELEASE = 0.045, 0.16         # seconds: rises fast, falls gently
SPEED = (0.55, 0.9)                   # wave travel: cycles/s at rest, + at full voice
SHIMMER_PERIOD = 0.9                  # one glide along the line while working

SS = 3                                # drawn 3x bigger, then scaled down: smooth


# =============================================================================
#  MOTION
# =============================================================================

def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def ease_in_cubic(t):
    return t ** 3


def ease_in_out_cubic(t):
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out_back(t, c=1.1):
    """Ease out with a small spring past the end (~4%) - an opening that
    feels alive rather than mechanical."""
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


class _Glide:
    """One animated number: from where it is, to a target, over a duration,
    along an easing curve - by the clock, so a late frame never slows it."""

    def __init__(self, value):
        self.a = self.b = float(value)
        self.t0, self.dur, self.ease = 0.0, 0.0, ease_out_cubic

    def value(self, now):
        if now <= self.t0:
            return self.a
        if self.dur <= 0 or now >= self.t0 + self.dur:
            return self.b
        return self.a + (self.b - self.a) * self.ease((now - self.t0) / self.dur)

    def to(self, target, now, dur, ease=ease_out_cubic, delay=0.0):
        self.a = self.value(now)            # always from where it is NOW
        self.b = float(target)
        self.t0, self.dur, self.ease = now + delay, dur, ease

    def done(self, now):
        return now >= self.t0 + self.dur


Frame = namedtuple("Frame", "width alpha scale draw_in amp phase shimmer shimmer_mix caret")
Frame.__doc__ = """What to draw: capsule `width` (100% px), `alpha` and `scale`
(0..1), how much of the line is drawn (`draw_in`, from the caret leftward),
wave height `amp` (0..1) and travel `phase`, the working light's position
`shimmer` (0 = line start, 1 = caret) and strength `shimmer_mix`, and the
`caret` colour."""


class Motion:
    """The capsule's state over time. Not thread-safe: the Tk main thread
    owns it (app.update_overlay / _indicator_tick)."""

    def __init__(self):
        self.state = "idle"
        self.width = _Glide(DOT)
        self.alpha = _Glide(0.0)
        self.scale = _Glide(0.85)
        self.draw_in = _Glide(0.0)
        self.flat = _Glide(1.0)          # 1 = the wave pressed into a straight line
        self.shimmer_mix = _Glide(0.0)
        self.level = 0.0                 # the voice, smoothed
        self.phase = 0.0
        self.shimmer_t0 = 0.0
        self.caret = CARET
        self._last = None

    def _open(self, now, from_nothing):
        self.alpha.to(1.0, now, 0.09, ease_out_cubic)
        self.scale.to(1.0, now, 0.18, ease_out_back)
        self.width.to(CAP_W, now, 0.28, ease_out_back, delay=0.04 if from_nothing else 0.0)
        self.draw_in.to(1.0, now, 0.22, ease_out_cubic, delay=0.10 if from_nothing else 0.0)

    def set_state(self, state, now):
        """"recording", "transcribing" or "idle" (anything else = idle)."""
        state = state if state in ("recording", "transcribing") else "idle"
        if state == self.state:
            return
        from_nothing = self.alpha.value(now) < 0.01
        if from_nothing and state != "idle":
            # Start every appearance the same way: a small dot at the caret.
            for glide, value in ((self.width, DOT), (self.scale, 0.85), (self.draw_in, 0.0),
                                 (self.flat, 1.0), (self.shimmer_mix, 0.0)):
                glide.__init__(value)
            self.level = self.phase = 0.0
        self.state = state
        if state == "recording":
            self._open(now, from_nothing)
            self.flat.to(0.0, now, 0.15, ease_in_out_cubic)
            self.shimmer_mix.to(0.0, now, 0.12)
        elif state == "transcribing":
            self._open(now, from_nothing)           # (a very short press: open anyway)
            self.flat.to(1.0, now, 0.18, ease_in_out_cubic)
            self.shimmer_mix.to(1.0, now, 0.25, ease_in_out_cubic, delay=0.15)
            self.shimmer_t0 = now + 0.15
        else:
            self.flat.to(1.0, now, 0.12, ease_in_out_cubic)
            self.shimmer_mix.to(0.0, now, 0.12)
            self.draw_in.to(0.0, now, 0.15, ease_in_cubic)
            self.width.to(DOT, now, 0.18, ease_in_out_cubic, delay=0.12)
            self.scale.to(0.9, now, 0.13, ease_in_cubic, delay=0.23)
            self.alpha.to(0.0, now, 0.13, ease_in_cubic, delay=0.23)

    def hidden(self, now):
        """Nothing to show: idle and fully faded."""
        return self.state == "idle" and self.alpha.done(now) and self.alpha.value(now) <= 0.001

    def step(self, now, level=0.0):
        """The Frame at time `now` (seconds, monotonic) with the mic's current
        RMS `level` - or None when there is nothing to show."""
        dt = 0.0 if self._last is None else min(0.1, max(0.0, now - self._last))
        self._last = now
        target = 0.0
        if self.state == "recording":
            target = min(1.0, (max(0.0, level) * LEVEL_GAIN) ** LEVEL_CURVE)
        tau = ATTACK if target > self.level else RELEASE
        if dt > 0:
            self.level += (target - self.level) * (1 - math.exp(-dt / tau))
        if self.hidden(now):
            self.level = 0.0
            return None
        self.phase = (self.phase + dt * 2 * math.pi * (SPEED[0] + SPEED[1] * self.level)) \
            % (2 * math.pi * 1000)
        amp = max(LEVEL_FLOOR, self.level) * (1 - self.flat.value(now))
        p = ((now - self.shimmer_t0) / SHIMMER_PERIOD) % 1.0
        shimmer = -0.15 + 1.3 * (0.5 - 0.5 * math.cos(math.pi * p))   # glides in and out
        return Frame(width=self.width.value(now), alpha=max(0.0, min(1.0, self.alpha.value(now))),
                     scale=self.scale.value(now), draw_in=max(0.0, min(1.0, self.draw_in.value(now))),
                     amp=amp, phase=self.phase, shimmer=shimmer,
                     shimmer_mix=max(0.0, min(1.0, self.shimmer_mix.value(now))), caret=self.caret)


# =============================================================================
#  THE PICTURE
# =============================================================================

def window_size(scale):
    """The window's size in real pixels at display `scale`: the capsule at its
    widest (with room for the spring) plus the shadow's margin."""
    return (int(round((CAP_W * OVERSHOOT_ROOM + 2 * MARGIN) * scale)),
            int(round((CAP_H + 2 * MARGIN) * scale)))


def place(work_area, scale):
    """Top-left corner for the window: centred on `work_area` (left, top,
    right, bottom - the screen minus the taskbar), the capsule's bottom
    BOTTOM_GAP above the taskbar."""
    left, _top, right, bottom = work_area
    w, _h = window_size(scale)
    return (left + (right - left - w) // 2,
            bottom - int(round((BOTTOM_GAP + MARGIN + CAP_H) * scale)))


_bodies = OrderedDict()      # (width, height, scale) -> shadow + capsule


def _body(width, height, scale):
    """The shadow and the empty capsule. Cached: while you talk the capsule
    keeps its size, so only the line is drawn each frame."""
    key = (round(width * 2) / 2, round(height * 2) / 2, scale)
    img = _bodies.get(key)
    if img is not None:
        _bodies.move_to_end(key)
        return img
    W, H = window_size(scale)
    cx, cy = W / 2, H / 2
    sw, sh = key[0] * scale, key[1] * scale
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    drop = SHADOW_DROP * scale
    ImageDraw.Draw(shadow).rounded_rectangle(
        (cx - sw / 2, cy - sh / 2 + drop, cx + sw / 2, cy + sh / 2 + drop), radius=sh / 2, fill=SHADOW)
    shadow = shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR * scale))
    big = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    box = (SS * (cx - sw / 2), SS * (cy - sh / 2), SS * (cx + sw / 2), SS * (cy + sh / 2))
    ImageDraw.Draw(big).rounded_rectangle(box, radius=SS * sh / 2, fill=FILL)
    edge = Image.new("RGBA", big.size, (0, 0, 0, 0))     # blended OVER the fill
    ImageDraw.Draw(edge).rounded_rectangle(box, radius=SS * sh / 2, outline=EDGE,
                                           width=max(1, int(round(SS * scale))))
    capsule = Image.alpha_composite(big, edge).resize((W, H), Image.LANCZOS)
    img = Image.alpha_composite(shadow, capsule)
    _bodies[key] = img
    while len(_bodies) > 96:
        _bodies.popitem(last=False)
    return img


def render(frame, scale):
    """The picture for `frame` at display `scale`: RGBA, window_size(scale).
    (frame.alpha is NOT applied here - the window fades the whole picture.)"""
    s = max(0.05, frame.scale)
    width, height = max(DOT, frame.width) * s, CAP_H * s
    img = _body(width, height, scale).copy()
    W, H = img.size
    k = scale * SS
    layer = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = W * SS / 2, H * SS / 2
    right = cx + width * k / 2
    caret_x = right - CARET_INSET * s * k
    cw, ch = CARET_W * s * k, CARET_H * s * k
    d.rounded_rectangle((caret_x - cw / 2, cy - ch / 2, caret_x + cw / 2, cy + ch / 2),
                        radius=cw / 2, fill=tuple(frame.caret) + (255,))
    x0 = cx - width * k / 2 + LINE_LEFT * s * k
    x1 = caret_x - LINE_GAP * s * k
    if frame.draw_in > 0.01 and x1 - x0 > 2 * k:
        start = x1 - (x1 - x0) * frame.draw_in
        lw = LINE_W * s * k
        n = 96
        pts = []
        for i in range(n + 1):
            x = start + (x1 - start) * i / n
            t = (x - x0) / (x1 - x0)
            env = math.sin(math.pi * t) ** 1.6              # calm at both ends
            y = cy - WAVE_H * s * k * frame.amp * env * math.sin(
                2 * math.pi * WAVE_PERIODS * t - frame.phase)
            pts.append((x, y, t))
        r = lw / 2
        if frame.shimmer_mix < 0.01:
            color = INK + (255,)
            d.line([(x, y) for x, y, _t in pts], fill=color, width=max(1, int(round(lw))),
                   joint="curve")
            for x, y, _t in (pts[0], pts[-1]):
                d.ellipse((x - r, y - r, x + r, y + r), fill=color)
        else:
            # A light gliding along the line: each piece as bright as its
            # distance from the light, over a dimmed line.
            for (xa, ya, ta), (xb, yb, tb) in zip(pts, pts[1:]):
                glow = math.exp(-(((ta + tb) / 2 - frame.shimmer) / 0.13) ** 2)
                bright = 0.38 + 0.62 * glow
                a = 1 - frame.shimmer_mix * (1 - bright)
                color = INK + (int(255 * a),)
                d.line((xa, ya, xb, yb), fill=color, width=max(1, int(round(lw))))
                d.ellipse((xb - r, yb - r, xb + r, yb + r), fill=color)
            xa, ya, ta = pts[0]
            a = 1 - frame.shimmer_mix * (1 - (0.38 + 0.62 * math.exp(-((ta - frame.shimmer) / 0.13) ** 2)))
            d.ellipse((xa - r, ya - r, xa + r, ya + r), fill=INK + (int(255 * a),))
    return Image.alpha_composite(img, layer.resize((W, H), Image.LANCZOS))


# =============================================================================
#  THE WINDOW  (Windows only)
# =============================================================================

if sys.platform == "win32":
    from ctypes import wintypes

    # Private library handles: argtypes set here never leak into the rest of
    # the app's ctypes.windll calls.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _winmm = ctypes.WinDLL("winmm")

    class _BLEND(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class _BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class _BMI(ctypes.Structure):
        _fields_ = [("bmiHeader", _BIH), ("bmiColors", wintypes.DWORD * 3)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    _user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                        wintypes.HINSTANCE, wintypes.LPVOID]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                            ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                            ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                            ctypes.POINTER(_BLEND), wintypes.DWORD]
    _user32.UpdateLayeredWindow.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HMONITOR
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BMI), wintypes.UINT,
                                        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    _EX_STYLE = (0x00080000      # WS_EX_LAYERED: our own alpha channel
                 | 0x00000020    # WS_EX_TRANSPARENT: clicks go through it
                 | 0x00000008    # WS_EX_TOPMOST
                 | 0x00000080    # WS_EX_TOOLWINDOW: no taskbar button, no Alt+Tab
                 | 0x08000000)   # WS_EX_NOACTIVATE: never takes focus
    _WS_POPUP = 0x80000000
    _HWND_TOPMOST = wintypes.HWND(-1)
    _SWP_FLAGS = 0x0001 | 0x0002 | 0x0010   # no size, no move, no activate
    _SW_HIDE, _SW_SHOWNA = 0, 8


class LayeredWindow:
    """A borderless, click-through, always-on-top window showing a picture
    with a real alpha channel (UpdateLayeredWindow). Make it, show() and
    hide() it, and close() it - all on ONE thread (the Tk main thread in
    the app, whose message loop serves it). Raises OSError if Windows
    refuses to make it."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.visible = False
        self.hwnd = self._screen = self._mem = self._dib = self._old = None
        self._bits = ctypes.c_void_p()
        self.hwnd = _user32.CreateWindowExW(_EX_STYLE, "STATIC", "Scribe", _WS_POPUP, 0, 0,
                                            width, height, None, None,
                                            _kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise OSError(ctypes.get_last_error(), "couldn't create the indicator window")
        try:
            self._screen = _user32.GetDC(None)
            self._mem = _gdi32.CreateCompatibleDC(self._screen)
            bmi = _BMI()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BIH)
            bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = width, -height   # top-down rows
            bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
            self._dib = _gdi32.CreateDIBSection(self._mem, ctypes.byref(bmi), 0,
                                                ctypes.byref(self._bits), None, 0)
            if not self._dib or not self._bits:
                raise OSError(ctypes.get_last_error(), "couldn't create the indicator bitmap")
            self._old = _gdi32.SelectObject(self._mem, self._dib)
        except Exception:
            self.close()
            raise

    def show(self, image, x, y, alpha):
        """Put `image` (RGBA, exactly width x height) at (x, y), faded to
        `alpha` (0..1) as a whole."""
        rgba = np.asarray(image, dtype=np.uint16)
        a = rgba[..., 3:4]
        bgra = np.empty((self.height, self.width, 4), dtype=np.uint8)
        bgra[..., :3] = ((rgba[..., 2::-1] * a + 127) // 255)   # premultiplied, B G R
        bgra[..., 3] = rgba[..., 3]
        ctypes.memmove(self._bits, bgra.tobytes(), bgra.nbytes)
        blend = _BLEND(0, 0, max(0, min(255, int(round(alpha * 255)))), 1)   # AC_SRC_ALPHA
        ok = _user32.UpdateLayeredWindow(self.hwnd, self._screen, ctypes.byref(wintypes.POINT(x, y)),
                                         ctypes.byref(wintypes.SIZE(self.width, self.height)),
                                         self._mem, ctypes.byref(wintypes.POINT(0, 0)), 0,
                                         ctypes.byref(blend), 2)                # ULW_ALPHA
        if not ok:
            raise OSError(ctypes.get_last_error(), "couldn't draw the indicator")
        if not self.visible:
            _user32.ShowWindow(self.hwnd, _SW_SHOWNA)
            # Back on top: another always-on-top window may have come since.
            _user32.SetWindowPos(self.hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_FLAGS)
            self.visible = True

    def hide(self):
        if self.visible:
            _user32.ShowWindow(self.hwnd, _SW_HIDE)
            self.visible = False

    def close(self):
        """Free everything (safe to call twice)."""
        if self._old:
            _gdi32.SelectObject(self._mem, self._old)
            self._old = None
        if self._dib:
            _gdi32.DeleteObject(self._dib)
            self._dib = None
        if self._mem:
            _gdi32.DeleteDC(self._mem)
            self._mem = None
        if self._screen:
            _user32.ReleaseDC(None, self._screen)
            self._screen = None
        if self.hwnd:
            _user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        self.visible = False


def work_area():
    """(left, top, right, bottom) of the screen you're working on, minus its
    taskbar - the monitor of the window in front. None if Windows won't say."""
    if sys.platform != "win32":
        return None
    try:
        monitor = _user32.MonitorFromWindow(_user32.GetForegroundWindow(), 2)  # nearest
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if monitor and _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return None


def fine_timer(on):
    """Ask Windows for 1 ms timer precision while animating (60 steady frames
    a second instead of ~64 Hz jitter) - and give it back when idle, which
    saves battery. Pair every on with an off."""
    if sys.platform != "win32":
        return
    try:
        (_winmm.timeBeginPeriod if on else _winmm.timeEndPeriod)(1)
    except Exception:
        pass
