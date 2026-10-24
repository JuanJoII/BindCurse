from __future__ import annotations

from typing import Any

from bindcurse.services.audio.engine import VirtualMicEngine

__all__ = ["VirtualMicEngine", "LinuxAudioBackend", "WindowsAudioBackend"]


def __getattr__(name: str) -> Any:
    if name == "LinuxAudioBackend":
        from bindcurse.services.audio.linux_backend import LinuxAudioBackend

        return LinuxAudioBackend
    if name == "WindowsAudioBackend":
        from bindcurse.services.audio.windows_backend import WindowsAudioBackend

        return WindowsAudioBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
