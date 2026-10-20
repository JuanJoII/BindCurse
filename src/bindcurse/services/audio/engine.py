from __future__ import annotations

import atexit
import socket
import struct
import threading
import time

import numpy as np

from typing import TYPE_CHECKING

from bindcurse.config import (
    AUDIO_MAGIC,
    CHANNELS,
    CHUNK_SAMPLES,
    CURRENT_PLATFORM,
    IS_LINUX,
    IS_WINDOWS,
    PCM_DTYPE,
    SAMPLE_RATE,
)

if TYPE_CHECKING:
    from bindcurse.services.audio.linux_backend import LinuxAudioBackend
    from bindcurse.services.audio.windows_backend import WindowsAudioBackend


class VirtualMicEngine:
    """
    Coordinates platform audio backends, gain scaling, mute,
    and system audio monitoring loopback.
    """

    def __init__(
        self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.platform = CURRENT_PLATFORM

        # User-controllable audio parameters
        self.mic_muted: bool = False
        self.mic_gain: float = 1.0
        self.monitor_enabled: bool = False
        self.monitor_volume: float = 1.0

        # Backend state
        self.virtual_mic_ready: bool = False
        self.backend_name: str = "No inicializado"
        self.device_label: str = "-"
        self.status_message: str = "Iniciando..."

        self._lock = threading.Lock()

        # Instantiate OS-specific backends lazily to avoid loading unused native libraries
        self._linux_backend: LinuxAudioBackend | None = None
        self._windows_backend: WindowsAudioBackend | None = None
        if IS_LINUX:
            from bindcurse.services.audio.linux_backend import LinuxAudioBackend

            self._linux_backend = LinuxAudioBackend(sample_rate, channels)
        elif IS_WINDOWS:
            from bindcurse.services.audio.windows_backend import WindowsAudioBackend

            self._windows_backend = WindowsAudioBackend(sample_rate, channels)

        atexit.register(self.shutdown)

    def setup_virtual_mic(self) -> tuple[bool, str]:
        """Creates or connects to the system virtual microphone."""
        with self._lock:
            self._cleanup_virtual_mic_locked()
            if IS_LINUX and self._linux_backend is not None:
                ok, label, msg = self._linux_backend.setup()
                self.virtual_mic_ready = ok
                self.backend_name = "PipeWire / PulseAudio" if ok else "PipeWire / PulseAudio (Error)"
                self.device_label = label
                self.status_message = msg
                return ok, msg
            elif IS_WINDOWS and self._windows_backend is not None:
                ok, label, msg = self._windows_backend.setup()
                self.virtual_mic_ready = ok
                self.backend_name = "VB-Audio Virtual Cable" if ok else "VB-Cable (No detectado)"
                self.device_label = label
                self.status_message = msg
                return ok, msg
            else:
                self.status_message = f"Plataforma no soportada: {self.platform}"
                self.virtual_mic_ready = False
                return False, self.status_message

    def set_monitor_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.monitor_enabled = enabled
            if not enabled:
                if self._linux_backend:
                    self._linux_backend.stop_monitor_pacat()
                if self._windows_backend:
                    self._windows_backend.stop_monitor()
            else:
                if self._linux_backend:
                    self._linux_backend.start_monitor_pacat()
                elif self._windows_backend:
                    self._windows_backend.ensure_monitor()

    def process_audio_chunk(self, raw_bytes: bytes, _addr: tuple[str, int]) -> None:
        """
        Receives int16 mono 48kHz PCM bytes from the UDP receiver and injects them
        in real time into the virtual microphone and (if enabled) system audio monitor.
        Uses a zero-copy fast path when gain/volume are at 100% (1.0).
        """
        if not raw_bytes:
            return

        samples: np.ndarray | None = None

        # 1. Prepare Virtual Microphone PCM buffer
        if not self.mic_muted:
            if abs(self.mic_gain - 1.0) > 1e-3:
                samples = np.frombuffer(raw_bytes, dtype=PCM_DTYPE)
                scaled = np.clip(
                    samples.astype(np.float32) * self.mic_gain, -32768.0, 32767.0
                ).astype(PCM_DTYPE)
                mic_bytes = scaled.tobytes()
            else:
                mic_bytes = raw_bytes
        else:
            mic_bytes = b"\x00" * len(raw_bytes)

        # 2. Prepare System Output Monitor PCM buffer (if enabled)
        monitor_bytes: bytes | None = None
        if self.monitor_enabled:
            if abs(self.monitor_volume - 1.0) > 1e-3:
                if samples is None:
                    samples = np.frombuffer(raw_bytes, dtype=PCM_DTYPE)
                mon_scaled = np.clip(
                    samples.astype(np.float32) * self.monitor_volume,
                    -32768.0,
                    32767.0,
                ).astype(PCM_DTYPE)
                monitor_bytes = mon_scaled.tobytes()
            else:
                monitor_bytes = raw_bytes

        with self._lock:
            # Inject into Virtual Microphone
            if self.virtual_mic_ready:
                if self._linux_backend:
                    self._linux_backend.write_mic(mic_bytes)
                elif self._windows_backend:
                    self._windows_backend.write_mic(mic_bytes)

            # Inject into System Audio Output Monitor if enabled
            if self.monitor_enabled and monitor_bytes is not None:
                if self._linux_backend:
                    self._linux_backend.write_monitor(monitor_bytes)
                elif self._windows_backend:
                    self._windows_backend.write_monitor(monitor_bytes)

    def send_test_tone_udp(self, port: int = 5005, duration_sec: float = 1.5) -> None:
        """
        Spawns a background thread that sends a pleasant 440Hz/554Hz PCM test signal
        over UDP to 127.0.0.1:port so the user can verify the virtual mic and monitor.
        """

        def _worker() -> None:
            chunk_samples = CHUNK_SAMPLES
            total_chunks = int((duration_sec * self.sample_rate) / chunk_samples)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    # Optional handshake before stream
                    sock.sendto(b"BINDCURSE_HELLO|1|48000|16|1|LocalTestTone", ("127.0.0.1", port))
                    for i in range(total_chunks):
                        t = (np.arange(chunk_samples) + i * chunk_samples) / float(
                            self.sample_rate
                        )
                        freq = 440.0 if i < (total_chunks // 2) else 554.37
                        wave = np.sin(2.0 * np.pi * freq * t) * 0.35
                        pcm = (wave * 32767.0).astype(PCM_DTYPE).tobytes()
                        packet = AUDIO_MAGIC + struct.pack("<I", i) + pcm
                        sock.sendto(packet, ("127.0.0.1", port))
                        time.sleep(chunk_samples / float(self.sample_rate))
            except Exception:
                pass

        threading.Thread(target=_worker, name="UDPTestTone", daemon=True).start()

    def _cleanup_virtual_mic_locked(self) -> None:
        if self._linux_backend:
            self._linux_backend.cleanup()
        if self._windows_backend:
            self._windows_backend.cleanup()
        self.virtual_mic_ready = False

    def shutdown(self) -> None:
        with self._lock:
            self._cleanup_virtual_mic_locked()
