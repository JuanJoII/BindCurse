from __future__ import annotations

import array
import atexit
import math
import queue
import socket
import struct
import sys
import threading
import time
from typing import TYPE_CHECKING

from bindcurse.core.config import (
    AUDIO_MAGIC,
    CHANNELS,
    CHUNK_SAMPLES,
    CURRENT_PLATFORM,
    SAMPLE_RATE,
)
from bindcurse.core import get_orchestrator

if TYPE_CHECKING:
    from bindcurse.services.audio.linux_backend import LinuxAudioBackend
    from bindcurse.services.audio.windows_backend import WindowsAudioBackend


def _scale_pcm_s16le(raw_bytes: bytes, gain: float) -> bytes:
    """Scales 16-bit signed little-endian PCM samples with clipping using stdlib array."""
    valid_len = len(raw_bytes) & ~1
    if valid_len <= 0:
        return b""
    arr = array.array("h")
    arr.frombytes(raw_bytes[:valid_len])
    if sys.byteorder != "little":
        arr.byteswap()
    scaled = array.array(
        "h",
        (
            -32768 if (v := int(s * gain)) < -32768 else (32767 if v > 32767 else v)
            for s in arr
        ),
    )
    if sys.byteorder != "little":
        scaled.byteswap()
    return scaled.tobytes()


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
        self.orchestrator = get_orchestrator()

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
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=20)
        self._worker_running = True
        self._worker_thread = threading.Thread(
            target=self._audio_worker, name="VirtualMicEngineWorker", daemon=True
        )
        self._worker_thread.start()

        # Instantiate OS-specific backends lazily via the platform orchestrator
        self._linux_backend: LinuxAudioBackend | None
        self._windows_backend: WindowsAudioBackend | None
        self._linux_backend, self._windows_backend = (
            self.orchestrator.create_audio_backends(sample_rate, channels)
        )

        atexit.register(self.shutdown)

    def setup_virtual_mic(self) -> tuple[bool, str]:
        """Creates or connects to the system virtual microphone."""
        with self._lock:
            self._cleanup_virtual_mic_locked()
            ok, backend_name, label, msg = self.orchestrator.setup_audio_backend(
                self._linux_backend, self._windows_backend
            )
            self.virtual_mic_ready = ok
            self.backend_name = backend_name
            self.device_label = label
            self.status_message = msg
            return ok, msg

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
                if self._windows_backend:
                    self._windows_backend.ensure_monitor()

    def process_audio_chunk(self, raw_bytes: bytes, _addr: tuple[str, int]) -> None:
        """
        Receives int16 mono 48kHz PCM bytes from the UDP receiver and enqueues them
        for non-blocking injection into the virtual microphone and monitor.
        Drops oldest frame if queue is full to preserve real-time low latency.
        """
        if not raw_bytes or not self._worker_running:
            return
        try:
            self._audio_queue.put_nowait(raw_bytes)
        except queue.Full:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._audio_queue.put_nowait(raw_bytes)
            except queue.Full:
                pass

    def _audio_worker(self) -> None:
        while self._worker_running:
            try:
                raw_bytes = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            mic_muted = self.mic_muted
            mic_gain = self.mic_gain
            monitor_enabled = self.monitor_enabled
            monitor_volume = self.monitor_volume

            # 1. Prepare Virtual Microphone PCM buffer
            if not mic_muted:
                if abs(mic_gain - 1.0) > 1e-3:
                    mic_bytes = _scale_pcm_s16le(raw_bytes, mic_gain)
                else:
                    mic_bytes = raw_bytes
            else:
                mic_bytes = b"\x00" * len(raw_bytes)

            # 2. Prepare System Output Monitor PCM buffer (if enabled)
            monitor_bytes: bytes | None = None
            if monitor_enabled:
                if abs(monitor_volume - 1.0) > 1e-3:
                    if abs(monitor_volume - mic_gain) <= 1e-4 and not mic_muted:
                        monitor_bytes = mic_bytes
                    else:
                        monitor_bytes = _scale_pcm_s16le(raw_bytes, monitor_volume)
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
                if monitor_enabled and monitor_bytes is not None:
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
                    sock.sendto(
                        b"BINDCURSE_HELLO|1|48000|16|1|LocalTestTone",
                        ("127.0.0.1", port),
                    )
                    for i in range(total_chunks):
                        freq = 440.0 if i < (total_chunks // 2) else 554.37
                        step = 2.0 * math.pi * freq / float(self.sample_rate)
                        base = i * chunk_samples
                        amp = 0.35 * 32767.0
                        pcm_arr = array.array(
                            "h",
                            (
                                int(math.sin((base + k) * step) * amp)
                                for k in range(chunk_samples)
                            ),
                        )
                        if sys.byteorder != "little":
                            pcm_arr.byteswap()
                        pcm = pcm_arr.tobytes()
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
        self._worker_running = False
        with self._lock:
            self._cleanup_virtual_mic_locked()
