from __future__ import annotations

from typing import Any

from bindcurse.gui.helpers import find_system_font, format_bytes
from bindcurse.gui.theme import create_danger_button_theme, setup_theme_and_fonts

__all__ = [
    "VirtualMicApp",
    "find_system_font",
    "format_bytes",
    "setup_theme_and_fonts",
    "create_danger_button_theme",
]


def __getattr__(name: str) -> Any:
    if name == "VirtualMicApp":
        from bindcurse.gui.app import VirtualMicApp

        return VirtualMicApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
