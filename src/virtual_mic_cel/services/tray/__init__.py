from __future__ import annotations

from virtual_mic_cel.config import IS_LINUX, IS_WINDOWS
from virtual_mic_cel.services.tray.base import TrayCallbacks, TrayIcon, TrayState
from virtual_mic_cel.services.tray.icons import render_icon, to_argb32_bytes


def create_tray(callbacks: TrayCallbacks) -> TrayIcon:
    if IS_LINUX:
        from virtual_mic_cel.services.tray.linux_tray import LinuxSNITray

        return LinuxSNITray(callbacks)
    if IS_WINDOWS:
        from virtual_mic_cel.services.tray.windows_tray import WindowsTray

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
