from __future__ import annotations

from bindcurse.core import get_orchestrator
from bindcurse.services.tray.base import TrayCallbacks, TrayIcon, TrayState
from bindcurse.services.tray.icons import (
    render_argb32_icon,
    render_icon,
    to_argb32_bytes,
)


def create_tray(callbacks: TrayCallbacks) -> TrayIcon:
    return get_orchestrator().create_tray(callbacks)


__all__ = [
    "TrayCallbacks",
    "TrayIcon",
    "TrayState",
    "create_tray",
    "render_argb32_icon",
    "render_icon",
    "to_argb32_bytes",
]
