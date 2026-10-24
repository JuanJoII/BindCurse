from __future__ import annotations

from bindcurse.core import get_orchestrator


def find_system_font(bold: bool = False) -> str | None:
    """Finds a readable system TTF font supporting Latin-1 / Spanish characters."""
    return get_orchestrator().find_system_font(bold=bold)


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024.0:.1f} KB"
    return f"{num_bytes / (1024.0 * 1024.0):.2f} MB"


def terminate_dpg_glfw() -> None:
    """
    Cleans up native display server resources after destroying the DearPyGui context.
    Delegates platform-specific teardown (GLFW on Linux vs native Win32/DX11 on Windows)
    to the platform orchestrator.
    """
    get_orchestrator().cleanup_gui_viewport()
