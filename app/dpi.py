from __future__ import annotations

"""Windows high-DPI setup.

This module deliberately imports neither tkinter nor any application module.
Windows DPI awareness must be selected before the first native window exists;
otherwise Windows may bitmap-scale the whole Tk window and make text blurry.
"""

import ctypes
import sys


PER_MONITOR_AWARE_V2 = -4


def _context(value: int) -> ctypes.c_void_p:
    return ctypes.c_void_p(value & ((1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1))


def enable_high_dpi_awareness() -> str:
    """Request the sharpest DPI mode available and return a diagnostic label."""
    if not sys.platform.startswith("win"):
        return "not-windows"

    errors: list[str] = []
    context = _context(PER_MONITOR_AWARE_V2)

    # Windows 10 1703+: Per-Monitor V2 gives crisp client/non-client rendering
    # and automatically handles a window moving between monitors.
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_process = user32.SetProcessDpiAwarenessContext
        set_process.argtypes = [ctypes.c_void_p]
        set_process.restype = ctypes.c_int
        if set_process(context):
            return "per-monitor-v2"
        errors.append(f"SetProcessDpiAwarenessContext={ctypes.get_last_error()}")

        # A Python/packaged executable manifest or Windows compatibility rule
        # may have fixed the process mode already.  A per-thread context still
        # applies to every Tk window created on this main UI thread.
        set_thread = user32.SetThreadDpiAwarenessContext
        set_thread.argtypes = [ctypes.c_void_p]
        set_thread.restype = ctypes.c_void_p
        if set_thread(context):
            return "per-monitor-v2-thread"
        errors.append(f"SetThreadDpiAwarenessContext={ctypes.get_last_error()}")
    except (AttributeError, OSError) as exc:
        errors.append(f"user32={type(exc).__name__}")

    # Windows 8.1 fallback.
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        set_awareness = shcore.SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        result = int(set_awareness(2))  # PROCESS_PER_MONITOR_DPI_AWARE
        if result == 0:
            return "per-monitor-v1"
        errors.append(f"SetProcessDpiAwareness=0x{result & 0xFFFFFFFF:08X}")
    except (AttributeError, OSError) as exc:
        errors.append(f"shcore={type(exc).__name__}")

    # Vista/Windows 7 fallback.  System-aware is still much sharper than
    # Windows' DPI-unaware bitmap enlargement on a single-monitor setup.
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_legacy = user32.SetProcessDPIAware
        set_legacy.argtypes = []
        set_legacy.restype = ctypes.c_int
        if set_legacy():
            return "system-aware"
        errors.append(f"SetProcessDPIAware={ctypes.get_last_error()}")
    except (AttributeError, OSError) as exc:
        errors.append(f"legacy={type(exc).__name__}")

    return "unchanged (" + "; ".join(errors) + ")"


def normalize_tk_scaling(root, *, minimum: float = 1.35, maximum: float = 2.25) -> tuple[float, float]:
    """Keep Tk point sizes usable after enabling per-monitor awareness.

    Some Windows/Python combinations report the monitor's full device scaling
    to Tk while geometry and widget widths are already expressed in physical
    pixels.  Alpha.5 capped the value too aggressively at 1.75: that prevented
    overlap, but made the interface tiny on a 2K high-DPI display.  The layout
    is now responsive, so a 2.25 ceiling keeps text readable while still
    avoiding extreme values from some Windows/Python combinations.
    The returned tuple is ``(reported, applied)`` for diagnostics and tests.
    """
    try:
        reported = float(root.tk.call("tk", "scaling"))
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - defensive
        return 1.0, 1.0
    applied = min(max(reported, float(minimum)), float(maximum))
    root.tk.call("tk", "scaling", applied)
    return reported, applied
