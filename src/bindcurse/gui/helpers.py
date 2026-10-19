from __future__ import annotations

import os
import subprocess


def find_system_font(bold: bool = False) -> str | None:
    """Finds a readable system TTF font supporting Latin-1 / Spanish characters."""
    try:
        pattern = "sans-serif:bold" if bold else "sans-serif"
        out = subprocess.check_output(
            ["fc-match", "-f", "%{file}\n", pattern],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ).strip()
        if out and os.path.isfile(out):
            return out
    except Exception:
        pass

    candidates = (
        [
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/noto/NotoSans-Bold.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/noto/NotoSans-Regular.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024.0:.1f} KB"
    return f"{num_bytes / (1024.0 * 1024.0):.2f} MB"


def terminate_dpg_glfw() -> None:
    """
    Terminates the GLFW backend after destroying DearPyGui context.

    DearPyGui's `destroy_context()` only frees its internal ImGui data
    structures, leaving the native GLFW window mapped on the display server
    (X11 / Wayland / Win32). Because event processing stops when the render loop
    exits, that un-destroyed window freezes on screen and does not respond to events.

    Explicitly invoking `glfwTerminate()` properly closes the native GLFW window
    and cleans up display surfaces so that the application can cleanly run in
    the background with zero lingering windows.
    """
    try:
        import ctypes
        import dearpygui._dearpygui as _dpg_ext

        lib = ctypes.CDLL(_dpg_ext.__file__)
        if hasattr(lib, "glfwTerminate"):
            lib.glfwTerminate.restype = None
            lib.glfwTerminate.argtypes = []
            lib.glfwTerminate()
    except Exception:
        pass

