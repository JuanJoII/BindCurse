from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class TrayState:
    mic_ready: bool
    muted: bool
    monitor: bool
    active_devices: int
    ip: str
    port: int

    @property
    def icon_state(self) -> str:
        if not self.mic_ready:
            return "error"
        if self.muted:
            return "muted"
        if self.active_devices > 0:
            return "streaming"
        return "idle"

    @property
    def status_line(self) -> str:
        if not self.mic_ready:
            return "Micrófono virtual no disponible"
        if self.muted:
            return "Micrófono silenciado"
        if self.active_devices == 1:
            return "1 dispositivo transmitiendo"
        if self.active_devices > 1:
            return f"{self.active_devices} dispositivos transmitiendo"
        return "Esperando conexión del móvil"

    @property
    def tooltip(self) -> str:
        mon = "  •  Monitor ON" if self.monitor else ""
        return f"{self.status_line}{mon}\nConectar a {self.ip}:{self.port} (UDP)"


@dataclass
class TrayCallbacks:
    get_state: Callable[[], TrayState]
    show_window: Callable[[], None]
    toggle_monitor: Callable[[], None]
    toggle_mute: Callable[[], None]
    quit_app: Callable[[], None]


class TrayIcon:
    """Common interface. `start()` never raises; if no tray is available it just stays inactive."""

    def __init__(self, callbacks: TrayCallbacks) -> None:
        self.cb = callbacks
        self.available = False
        self.error: Optional[str] = None

    def start(self) -> None: ...

    def stop(self) -> None: ...
