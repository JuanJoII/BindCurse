from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

# Application Identity
APP_NAME: str = "BindCurse"
APP_ID: str = "bindcurse"

# Protocol Specifications & Handshakes
BINDCURSE_VERSION: int = 1
PROTOCOL_PREFIX: bytes = b"BINDCURSE_"
AUDIO_MAGIC: bytes = b"BCPM"
AUDIO_HEADER_SIZE: int = 8  # 4 bytes magic ('BCPM') + 4 bytes uint32 LE sequence

# Audio Stream & PCM Specifications
DEFAULT_UDP_PORT: int = 5005
DEFAULT_CONTROL_PORT: int = 5006
SAMPLE_RATE: int = 48000
CHANNELS: int = 1
BYTES_PER_SAMPLE: int = 2  # 16-bit PCM (s16le)
PCM_DTYPE: str = "<i2"

# Audio buffer & packet sizes
CHUNK_SAMPLES: int = 960  # 20ms at 48000Hz
DEFAULT_BUFFER_SIZE: int = 262144  # 256 KB socket rcvbuf

# Device naming
VIRTUAL_SINK_NAME: str = "bindcurse_sink"
VIRTUAL_SOURCE_NAME: str = "bindcurse_source"
VIRTUAL_MIC_DESCRIPTION: str = "BindCurse_Mic"

# Platform detection
IS_LINUX: bool = sys.platform.startswith("linux")
IS_WINDOWS: bool = sys.platform.startswith("win")
CURRENT_PLATFORM: str = (
    "linux" if IS_LINUX else ("windows" if IS_WINDOWS else sys.platform)
)


def get_config_dir() -> Path:
    """Returns the platform-specific directory where user configuration is stored."""
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / APP_NAME
        return Path.home() / f".{APP_ID}"

    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / APP_ID
    return Path.home() / ".config" / APP_ID


def get_config_file_path() -> Path:
    """Returns the full path to config.json."""
    return get_config_dir() / "config.json"


@dataclass
class UserSettings:
    """
    Persistent application settings saved between sessions in JSON format.
    Handles atomic writes, corrupted file fallback, and debounced saving for smooth sliders.
    """

    mic_gain: float = 1.0
    monitor_volume: float = 1.0
    monitor_enabled: bool = False
    mic_muted: bool = False
    keep_in_background: bool = True
    udp_port: int = DEFAULT_UDP_PORT
    preferred_ip: str = ""

    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _save_timer: threading.Timer | None = field(default=None, init=False, repr=False)

    @classmethod
    def load(cls, config_path: Path | None = None) -> UserSettings:
        path = config_path or get_config_file_path()
        if not path.is_file():
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return cls()

            gain = float(data.get("mic_gain", 1.0))
            gain = max(0.0, min(5.0, gain))

            mon_vol = float(data.get("monitor_volume", 1.0))
            mon_vol = max(0.0, min(5.0, mon_vol))

            port = int(data.get("udp_port", DEFAULT_UDP_PORT))
            if not (1024 <= port <= 65535):
                port = DEFAULT_UDP_PORT

            return cls(
                mic_gain=gain,
                monitor_volume=mon_vol,
                monitor_enabled=bool(data.get("monitor_enabled", False)),
                mic_muted=bool(data.get("mic_muted", False)),
                keep_in_background=bool(data.get("keep_in_background", True)),
                udp_port=port,
                preferred_ip=str(data.get("preferred_ip", "")),
            )
        except Exception:
            return cls()

    def to_dict(self) -> dict[str, object]:
        return {
            "mic_gain": round(self.mic_gain, 4),
            "monitor_volume": round(self.monitor_volume, 4),
            "monitor_enabled": self.monitor_enabled,
            "mic_muted": self.mic_muted,
            "keep_in_background": self.keep_in_background,
            "udp_port": self.udp_port,
            "preferred_ip": self.preferred_ip,
        }

    def save(self, config_path: Path | None = None) -> None:
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
            self._write_to_disk(config_path)

    def save_debounced(
        self, delay: float = 0.4, config_path: Path | None = None
    ) -> None:
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            timer = threading.Timer(delay, self._write_to_disk, args=(config_path,))
            timer.daemon = True
            self._save_timer = timer
            timer.start()

    def flush(self, config_path: Path | None = None) -> None:
        self.save(config_path)

    def _write_to_disk(self, config_path: Path | None = None) -> None:
        path = config_path or get_config_file_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            content = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content + "\n")
            tmp_path.replace(path)
        except Exception:
            pass
