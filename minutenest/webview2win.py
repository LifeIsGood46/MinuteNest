"""Native app window via WebView2 (Edge runtime), COM-only — no .NET.

Uses the official WebView2 SDK type library (``native/WebView2.tlb``) plus
the loader DLL (``native/WebView2Loader.dll``) to host the WebView2 control
inside a plain Win32 window: standard title bar, taskbar entry, logo icon.

The Edge runtime ships with Windows 10/11 (Edge updates), so this is a real
desktop window on ~every machine. If the runtime is missing or COM wiring
fails, the caller falls back to the chromeless Edge app window, then the
system browser.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
from pathlib import Path

WND_CLASS = "MinuteNestWnd"
WM_DESTROY = 0x0002
WM_MOVE = 0x0003
WM_SIZE = 0x0005
WM_SETFOCUS = 0x0007
WM_TIMER = 0x0113
WM_WINDOWPOSCHANGED = 0x0047
WM_EXITSIZEMOVE = 0x0232
WM_DPICHANGED = 0x02E0

LRESULT = ctypes.c_ssize_t
UINT = ctypes.c_uint
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t

WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_SHOW = 5
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000004
SPI_GETWORKAREA = 0x0030

_NATIVE = Path(__file__).resolve().parent / "native"


# Native frame colours. APP_BG must equal --bg in webapp/static/style.css
# (a test enforces it) so the caption blends into the page.
APP_BG = (0x0C, 0x0F, 0x16)
APP_TEXT = (0xE6, 0xE9, 0xF0)

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


def rgb_to_colorref(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return (b << 16) | (g << 8) | r


def _apply_titlebar_theme(hwnd, dwm) -> None:
    """Dark title bar in the app's own colours (Windows 11; harmless elsewhere).

    Each attribute is optional: older builds reject the colour ones, which must
    never stop the window from opening.
    """
    settings = (
        (DWMWA_USE_IMMERSIVE_DARK_MODE, 1),
        (DWMWA_BORDER_COLOR, rgb_to_colorref(APP_BG)),
        (DWMWA_CAPTION_COLOR, rgb_to_colorref(APP_BG)),
        (DWMWA_TEXT_COLOR, rgb_to_colorref(APP_TEXT)),
    )
    for attr, value in settings:
        v = ctypes.c_int(value)
        with contextlib.suppress(OSError, AttributeError):   # an older Windows without this attribute
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.pointer(v), ctypes.sizeof(v))


def _set_dpi_aware() -> None:
    """Declare per-monitor DPI awareness before any window is created.

    A DPI-unaware process gets virtualized coordinates on scaled displays:
    a 1180x800 window request becomes ~1475x1000 physical pixels at 125%
    scaling — larger than a 1536x960 screen ("window bigger than the
    screen"). Declaring awareness makes pixels real again.
    """
    # lambdas: each DLL function is looked up only when tried (an older Windows lacks the newer ones)
    declarations = (
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703+)
        lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),  # PER_MONITOR_DPI_AWARE (8.1+)
        lambda: ctypes.windll.user32.SetProcessDPIAware(),  # noqa: PLW0108 - Vista+; looked up when tried
    )
    for declare in declarations:
        try:
            declare()
        except (OSError, AttributeError):
            continue          # an older Windows: try the older call
        return


def _loader():
    """Load WebView2Loader.dll from the package (or beside a frozen exe)."""
    candidates = [
        _NATIVE / "WebView2Loader.dll",
        Path(sys.executable).resolve().parent / "WebView2Loader.dll",
        Path(sys.executable).resolve().parent / "_internal" / "WebView2Loader.dll",
    ]
    for p in candidates:
        if p.is_file():
            try:
                lib = ctypes.WinDLL(str(p))
                lib.CreateCoreWebView2EnvironmentWithOptions.restype = ctypes.c_int
                lib.CreateCoreWebView2EnvironmentWithOptions.argtypes = [
                    ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                    ctypes.c_void_p]
                return lib
            except OSError:
                continue
    return None


def _tlb_path() -> Path | None:
    p = _NATIVE / "WebView2.tlb"
    return p if p.is_file() else None


def _user_data_dir() -> str:
    if getattr(sys, "frozen", False):
        d = Path(sys.executable).parent / "data" / "webview2"
    else:
        d = Path.home() / ".minutenest" / "webview2"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def webview2_available() -> bool:
    """Cheap capability check: loader + typelib present and the runtime installed."""
    if not sys.platform.startswith("win"):
        return False
    lib = _loader()
    if lib is None or _tlb_path() is None:
        return False
    try:
        f = lib.GetAvailableCoreWebView2BrowserVersionString
        f.restype = ctypes.c_int
        f.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_wchar_p)]
        ver = ctypes.c_wchar_p()
        return f(None, ctypes.byref(ver)) == 0 and bool(ver.value)
    except OSError:
        return False


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", UINT), ("style", UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(LRESULT, wt.HWND, UINT, WPARAM, LPARAM)),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, UINT, WPARAM, LPARAM)
SETTLE_TICKS = 30      # re-assert the web content's size every 50 ms for ~1.5 s after a resize
SETTLE_TIMER_MS = 50

# Chromium flags that matter on VMs / RDP / slow machines. The defaults assume a
# desktop with stable hardware compositing; in a VM they leave stale frames (the
# guide "only appears after a click"), white bands on maximize, and laggy
# clicks. Occlusion + background throttling pause rendering of windows Chromium
# thinks are covered, and GPU compositing misbehaves under paravirtual graphics.
_VM_BROWSER_ARGS = (
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-gpu-compositing",
)


def _add_vm_browser_args() -> None:
    existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    missing = [a for a in _VM_BROWSER_ARGS if a not in existing]
    if missing:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"{existing} {' '.join(missing)}".strip()


def _declare_signatures(user32, kernel32) -> None:
    """ctypes argument/return types of the Win32 calls used here (64-bit safe handles)."""
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wt.HWND, UINT, WPARAM, LPARAM]
    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [UINT, wt.LPCWSTR, wt.LPCWSTR, UINT,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.PeekMessageW.restype = ctypes.c_int
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, UINT, UINT, UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.GetClientRect.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, UINT, ctypes.c_void_p]
    user32.KillTimer.restype = ctypes.c_int
    user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
    user32.SystemParametersInfoW.restype = ctypes.c_int
    user32.SystemParametersInfoW.argtypes = [UINT, UINT, ctypes.c_void_p, UINT]
    user32.LoadImageW.restype = wt.HANDLE
    user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, UINT, ctypes.c_int, ctypes.c_int, UINT]
    kernel32.GetModuleHandleW.restype = wt.HINSTANCE
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]


def _system_dpi(user32) -> int:
    """The system DPI (96 = 100 %); GetDpiForSystem needs Windows 10 1607+."""
    try:
        user32.GetDpiForSystem.restype = UINT
        user32.GetDpiForSystem.argtypes = []
        return int(user32.GetDpiForSystem()) or 96
    except (AttributeError, OSError):
        pass    # older Windows: ask the screen device context below
    try:
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.restype = wt.HDC
        user32.GetDC.argtypes = [wt.HWND]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        gdi32.GetDeviceCaps.restype = ctypes.c_int
        gdi32.GetDeviceCaps.argtypes = [wt.HDC, ctypes.c_int]
        hdc = user32.GetDC(None)
        dpi = int(gdi32.GetDeviceCaps(hdc, 88)) or 96  # LOGPIXELSX
        user32.ReleaseDC(None, hdc)
        return dpi
    except (AttributeError, OSError):
        return 96


def _window_rect(user32, width: int, height: int) -> tuple[int, int, int, int]:
    """(x, y, w, h): the requested logical size at the screen's DPI, centred, never
    larger than the work area (a small VM display left the title bar off-screen)."""
    scale = _system_dpi(user32) / 96.0
    if scale > 1.0:
        width, height = int(width * scale), int(height * scale)
    wa = wt.RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(wa), 0):
        width = min(width, max(640, wa.right - wa.left - 16))
        height = min(height, max(480, wa.bottom - wa.top - 16))
    scr_w, scr_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    return max(0, (scr_w - width) // 2), max(0, (scr_h - height) // 2), width, height


class _Host:
    """The Win32 window that hosts the WebView2 control, and its message loop."""

    def __init__(self, user32, kernel32):
        self.user32 = user32
        self.hinstance = kernel32.GetModuleHandleW(None)
        self.closed = threading.Event()
        self.ctrl = None            # the WebView2 controller, once created
        self.settle = 0             # timer ticks left to re-assert the bounds
        self.applied: tuple | None = None
        self.hwnd = None
        self._proc = _WNDPROC(self.wnd_proc)   # kept alive as long as the window

    def open(self, title: str, width: int, height: int, icon_ico: Path | None) -> bool:
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = self.hinstance
        wc.lpszClassName = WND_CLASS
        if icon_ico and Path(icon_ico).is_file():
            wc.hIcon = self.user32.LoadImageW(None, str(icon_ico), IMAGE_ICON, 0, 0,
                                              LR_LOADFROMFILE | LR_DEFAULTSIZE)
            wc.hIconSm = wc.hIcon
        if not self.user32.RegisterClassExW(ctypes.byref(wc)):
            return False
        x, y, w, h = _window_rect(self.user32, width, height)
        self.hwnd = self.user32.CreateWindowExW(0, WND_CLASS, title, WS_OVERLAPPEDWINDOW, x, y, w, h,
                                                None, None, self.hinstance, None)
        if not self.hwnd:
            self.user32.UnregisterClassW(WND_CLASS, self.hinstance)
            return False
        try:
            _apply_titlebar_theme(self.hwnd, ctypes.windll.dwmapi)
        except OSError:   # no dwmapi.dll: the default title bar is fine
            sys.stderr.write("webview2: dark title bar unavailable\n")
        return True

    def sync_bounds(self) -> bool:
        """Keep the WebView2 control exactly filling the client area; True when it does.

        Setting ``Bounds`` is asynchronous across the WebView2 processes, so one
        set right after a maximize can land while the window is still resizing:
        the content then shows its old size for seconds (black bands).
        """
        if self.ctrl is None:
            return True
        r = wt.RECT()
        self.user32.GetClientRect(self.hwnd, ctypes.byref(r))
        current = (r.left, r.top, r.right, r.bottom)
        if self.applied == current:
            return True
        try:
            self.ctrl.Bounds = r
        except Exception:  # noqa: BLE001 - a COM error inside a window callback must not escape it
            return False   # the settle timer tries again
        self.applied = current
        return True

    def start_settle(self) -> None:
        """Re-assert bounds for ~1.5 s after a geometry change (bounded: SETTLE_TICKS)."""
        self.settle = SETTLE_TICKS
        self.user32.SetTimer(self.hwnd, 1, SETTLE_TIMER_MS, None)

    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_DESTROY:
            self.closed.set()
            self.user32.PostQuitMessage(0)
        elif msg == WM_SETFOCUS:
            self._focus_content()
            return 0
        elif msg == WM_DPICHANGED:
            self._move_to_suggested_rect(hwnd, lparam)
            self.sync_bounds()
            self.start_settle()
            return 0
        elif msg in (WM_SIZE, WM_MOVE, WM_WINDOWPOSCHANGED, WM_EXITSIZEMOVE):
            self.sync_bounds()
            # While a resize is in flight keep re-asserting briefly; the control's
            # async Bounds set otherwise lags the window on slow VMs.
            if msg in (WM_SIZE, WM_EXITSIZEMOVE):
                self.start_settle()
        elif msg == WM_TIMER:
            self.settle -= 1 if self.sync_bounds() else 0
            if self.settle <= 0:
                self.user32.KillTimer(hwnd, 1)
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _focus_content(self) -> None:
        """Hand keyboard focus to the web content. Without this the first click only
        activates the window ("buttons need a double click" on the VM)."""
        if self.ctrl is None:
            return
        try:
            self.ctrl.MoveFocus(0)  # COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC
        except Exception:  # noqa: BLE001 - a COM error inside a window callback must not escape it
            sys.stderr.write("webview2: could not move focus to the page\n")

    def _move_to_suggested_rect(self, hwnd, lparam) -> None:
        """Moving to a monitor with another DPI: Windows suggests the new window rect."""
        if not lparam:
            return
        sug = ctypes.cast(lparam, ctypes.POINTER(wt.RECT)).contents
        self.user32.SetWindowPos(hwnd, None, sug.left, sug.top, sug.right - sug.left, sug.bottom - sug.top,
                                 0x0004 | 0x0010)  # SWP_NOZORDER | SWP_NOACTIVATE

    def pump(self, until: threading.Event | None) -> None:
        """Dispatch messages until ``until`` is set or the window closes."""
        msg = wt.MSG()
        while not self.closed.is_set() and (until is None or not until.is_set()):
            if not self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                self.user32.WaitMessage()
                continue
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        if self.hwnd:
            self.user32.DestroyWindow(self.hwnd)
        self.user32.UnregisterClassW(WND_CLASS, self.hinstance)


def _start_webview(host: _Host, loader, tlb: Path, url: str) -> bool:
    """Create the WebView2 environment and controller in ``host`` and open ``url``."""
    import comtypes
    import comtypes.client

    if getattr(sys, "frozen", False):
        comtypes.client.gen_dir = None   # frozen packages are read-only; generate wrappers in memory
    comtypes.client.GetModule(str(tlb))
    import comtypes.gen.WebView2 as W
    from comtypes import COMObject

    got: dict = {}
    env_ready, ctrl_ready = threading.Event(), threading.Event()

    class EnvCb(COMObject):
        _com_interfaces_ = [W.ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler]  # noqa: RUF012

        def Invoke(self, errorCode, createdEnvironment):  # noqa: ARG002 - COM signature
            got["env"] = createdEnvironment
            env_ready.set()

    class CtrlCb(COMObject):
        _com_interfaces_ = [W.ICoreWebView2CreateCoreWebView2ControllerCompletedHandler]  # noqa: RUF012

        def Invoke(self, errorCode, createdController):  # noqa: ARG002 - COM signature
            got["ctrl"] = createdController
            ctrl_ready.set()

    env_cb = EnvCb()
    env_ptr = ctypes.cast(env_cb.QueryInterface(comtypes.IUnknown), ctypes.c_void_p).value
    if loader.CreateCoreWebView2EnvironmentWithOptions(None, _user_data_dir(), None, ctypes.c_void_p(env_ptr)):
        return False
    host.pump(env_ready)
    if got.get("env") is None:
        return False
    ctrl_cb = CtrlCb()
    got["env"].CreateCoreWebView2Controller(host.hwnd, ctrl_cb)
    host.pump(ctrl_ready)
    if got.get("ctrl") is None:
        return False
    host.ctrl = controller = got["ctrl"]
    r = wt.RECT()
    host.user32.GetClientRect(host.hwnd, ctypes.byref(r))
    controller.Bounds = r
    # The controller starts INVISIBLE — without this, WebView2 never paints and
    # the window shows a blank white page.
    controller.IsVisible = True
    controller.CoreWebView2.Navigate(url)
    return True


def open_webview2_window(url: str, title: str, width: int, height: int,
                         icon_ico: Path | None = None) -> bool:
    """Create a Win32 window hosting the WebView2 control. Blocks until the
    window closes. Returns False fast when WebView2 is unavailable.

    All COM work happens on this thread's STA with a message pump, so the
    WebView2 callbacks stay apartment-safe.
    """
    if not sys.platform.startswith("win"):
        return False
    _set_dpi_aware()
    import comtypes

    loader, tlb = _loader(), _tlb_path()
    if loader is None or tlb is None:
        return False
    _add_vm_browser_args()
    user32 = ctypes.windll.user32
    _declare_signatures(user32, ctypes.windll.kernel32)
    host = _Host(user32, ctypes.windll.kernel32)
    if not host.open(title, width, height, icon_ico):
        return False
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        if not _start_webview(host, loader, tlb, url):
            return False
        user32.ShowWindow(host.hwnd, SW_SHOW)
        user32.UpdateWindow(host.hwnd)
        # The controller's async Bounds set can lag the first paint on slow
        # VMs ("took a few seconds to adjust to screen size"); settle it.
        host.start_settle()
        host.pump(None)  # run until the window closes
        return True
    except Exception:  # noqa: BLE001 - any COM failure: the caller falls back to an Edge app window
        import traceback

        traceback.print_exc(file=sys.stderr)
        return False
    finally:
        with contextlib.suppress(OSError):   # COM was never initialised on this thread
            comtypes.CoUninitialize()
        host.close()
