from __future__ import annotations

from typing import Any

from bindcurse.services.network.ipc import send_ipc_command

__all__ = [
    "DeviceInfo",
    "UDPAudioReceiver",
    "get_local_ips",
    "send_ipc_command",
    "get_linux_ips",
    "get_windows_ips",
    "probe_primary_ip",
]


def __getattr__(name: str) -> Any:
    if name in ("get_linux_ips", "get_windows_ips", "probe_primary_ip"):
        from bindcurse.services.network import os_network

        return getattr(os_network, name)
    if name in ("DeviceInfo", "UDPAudioReceiver", "get_local_ips"):
        from bindcurse.services.network import udp_receiver

        return getattr(udp_receiver, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
