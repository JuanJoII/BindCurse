from __future__ import annotations

from bindcurse.config import IS_LINUX, IS_WINDOWS
from bindcurse.services.tray.base import TrayCallbacks, TrayIcon, TrayState
from bindcurse.services.tray.icons import render_icon, to_argb32_bytes


def create_tray(callbacks: TrayCallbacks) -> TrayIcon:
    if IS_LINUX:
        from bindcurse.services.tray.linux_tray import LinuxSNITray

        return LinuxSNITray(callbacks)
    if IS_WINDOWS:
        from bindcurse.services.tray.windows_tray import WindowsTray

        return WindowsTray(callbacks)
    return TrayIcon(callbacks)


__all__ = [
    "TrayCallbacks",
    "TrayIcon",
    "TrayState",
    "create_tray",
    "render_icon",
    "to_argb32_bytes",
]
