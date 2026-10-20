from __future__ import annotations

from typing import Any

from bindcurse.services.audio import VirtualMicEngine
from bindcurse.services.network import (
    DeviceInfo,
    UDPAudioReceiver,
    get_local_ips,
    send_ipc_command,
)
from bindcurse.services.tray import (
    TrayCallbacks,
    TrayIcon,
    TrayState,
    create_tray,
)

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
    if name in ("LinuxAudioBackend", "WindowsAudioBackend"):
        from bindcurse.services import audio

        return getattr(audio, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
