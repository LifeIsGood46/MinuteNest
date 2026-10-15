"""The packaged app's native window (Win32): find it, read its title, capture it."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from PIL import Image, ImageStat

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
WINDOW_CLASS = "MinuteNestWnd"


def wait_for_window(timeout_s: float = 30) -> int:
    """The app's top-level window handle, once it exists (Win32 has no event to await)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = user32.FindWindowW(WINDOW_CLASS, None)
        if hwnd:
            return hwnd
        time.sleep(0.25)
    raise AssertionError(f"no {WINDOW_CLASS} window within {timeout_s}s")


def title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def capture(hwnd: int) -> Image.Image:
    """The window as drawn, even when other windows cover it (PrintWindow)."""
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 2)  # PW_RENDERFULLCONTENT: includes the WebView2 page
    info = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(info), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1).convert("RGB")


def has_content(img: Image.Image) -> bool:
    """Not a blank (single-colour) window: the page actually rendered."""
    return max(ImageStat.Stat(img).stddev) > 10


def wait_for_content(hwnd: int, timeout_s: float = 30) -> Image.Image:
    """The window once the page has painted into it (a blank frame comes first)."""
    deadline = time.monotonic() + timeout_s
    while True:
        img = capture(hwnd)
        if has_content(img) or time.monotonic() > deadline:
            return img
        time.sleep(0.5)
