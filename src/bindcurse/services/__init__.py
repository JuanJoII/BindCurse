from __future__ import annotations

from typing import Any

__all__ = [
    "VirtualMicEngine",
    "LinuxAudioBackend",
    "WindowsAudioBackend",
    "DeviceInfo",
    "UDPAudioReceiver",
    "get_local_ips",
    "send_ipc_command",
    "TrayCallbacks",
    "TrayIcon",
    "TrayState",
    "create_tray",
]


def __getattr__(name: str) -> Any:
    if name in ("VirtualMicEngine", "LinuxAudioBackend", "WindowsAudioBackend"):
        from bindcurse.services import audio

        return getattr(audio, name)
    if name in ("DeviceInfo", "UDPAudioReceiver", "get_local_ips", "send_ipc_command"):
        from bindcurse.services import network

        return getattr(network, name)
    if name in ("TrayCallbacks", "TrayIcon", "TrayState", "create_tray"):
        from bindcurse.services import tray

        return getattr(tray, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
