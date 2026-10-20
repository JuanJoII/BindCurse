from __future__ import annotations

import shutil
import subprocess
import threading

from bindcurse.config import (
    CHANNELS,
    SAMPLE_RATE,
    VIRTUAL_MIC_DESCRIPTION,
    VIRTUAL_SINK_NAME,
    VIRTUAL_SOURCE_NAME,
)

PACAT_IDLE_TIMEOUT: float = 5.0


class LinuxAudioBackend:
    """Manages virtual mic and monitor using PipeWire/PulseAudio via pactl/pacat."""

    def __init__(
        self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.sink_module_id: str | None = None
        self.source_module_id: str | None = None
        self.mic_pacat: subprocess.Popen[bytes] | None = None
        self.monitor_pacat: subprocess.Popen[bytes] | None = None
        self._idle_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        return bool(shutil.which("pactl") and shutil.which("pacat"))

    def cleanup_stale_modules(self) -> None:
        if not shutil.which("pactl"):
            return
        try:
            out = subprocess.check_output(
                ["pactl", "list", "short", "modules"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
            mod_ids: list[str] = []
            for line in out.splitlines():
                if VIRTUAL_SINK_NAME in line or VIRTUAL_SOURCE_NAME in line:
                    parts = line.split("\t")
                    if parts and parts[0].strip().isdigit():
                        mod_ids.append(parts[0].strip())
            # Unload in reverse order
            for mod_id in reversed(mod_ids):
                subprocess.run(
                    ["pactl", "unload-module", mod_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                )
        except Exception:
            pass

    def setup(self) -> tuple[bool, str, str]:
        """
        Loads null-sink and remap-source modules, starts mic pacat stream.
        Returns (success, device_label, status_message).
        """
        if not self.is_available():
            return (
                False,
                "No disponible",
                "No se encontró 'pactl' o 'pacat'. Instala pipewire-pulse / libpulse.",
            )

        self.cleanup_stale_modules()

        try:
            sink_out = subprocess.check_output(
                [
                    "pactl",
                    "load-module",
                    "module-null-sink",
                    f"sink_name={VIRTUAL_SINK_NAME}",
                    f"sink_properties=device.description={VIRTUAL_MIC_DESCRIPTION}_Sink",
                    f"rate={self.sample_rate}",
                    f"channels={self.channels}",
                ],
                text=True,
                stderr=subprocess.PIPE,
                timeout=4.0,
            ).strip()
            self.sink_module_id = sink_out

            src_out = subprocess.check_output(
                [
                    "pactl",
                    "load-module",
                    "module-remap-source",
                    f"master={VIRTUAL_SINK_NAME}.monitor",
                    f"source_name={VIRTUAL_SOURCE_NAME}",
                    f"source_properties=device.description={VIRTUAL_MIC_DESCRIPTION}",
                    f"rate={self.sample_rate}",
                    f"channels={self.channels}",
                ],
                text=True,
                stderr=subprocess.PIPE,
                timeout=4.0,
            ).strip()
            self.source_module_id = src_out

            # pacat is started on demand when audio packets arrive and closed when idle
            device_label = f"{VIRTUAL_MIC_DESCRIPTION} ({VIRTUAL_SOURCE_NAME})"
            msg = f"Micrófono virtual activo: {VIRTUAL_MIC_DESCRIPTION} (48000 Hz, 16-bit, Mono)"
            return True, device_label, msg
        except Exception as exc:
            self.cleanup()
            return (
                False,
                "Error",
                f"Error creando micrófono virtual en PipeWire/PulseAudio: {exc}",
            )

    def _reset_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        timer = threading.Timer(PACAT_IDLE_TIMEOUT, self._on_idle_timeout)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _on_idle_timeout(self) -> None:
        with self._lock:
            self._idle_timer = None
            self.stop_mic_pacat()
            self.stop_monitor_pacat()

    def start_mic_pacat(self) -> None:
        if self.mic_pacat is not None and self.mic_pacat.poll() is None:
            return
        self.mic_pacat = subprocess.Popen(
            [
                "pacat",
                "--playback",
                "-d",
                VIRTUAL_SINK_NAME,
                "--format=s16le",
                f"--rate={self.sample_rate}",
                f"--channels={self.channels}",
                "--raw",
                "--latency-msec=20",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def stop_mic_pacat(self) -> None:
        if self.mic_pacat is not None:
            try:
                if self.mic_pacat.stdin:
                    self.mic_pacat.stdin.close()
                self.mic_pacat.terminate()
                self.mic_pacat.wait(timeout=0.5)
            except Exception:
                pass
            self.mic_pacat = None

    def write_mic(self, mic_bytes: bytes) -> None:
        with self._lock:
            if self.mic_pacat is None or self.mic_pacat.poll() is not None:
                self.start_mic_pacat()
            if self.mic_pacat is not None and self.mic_pacat.stdin is not None:
                try:
                    self.mic_pacat.stdin.write(mic_bytes)
                except BrokenPipeError:
                    self.start_mic_pacat()
            self._reset_idle_timer()

    def start_monitor_pacat(self) -> None:
        if self.monitor_pacat is not None and self.monitor_pacat.poll() is None:
            return
        if shutil.which("pacat"):
            self.monitor_pacat = subprocess.Popen(
                [
                    "pacat",
                    "--playback",
                    "--format=s16le",
                    f"--rate={self.sample_rate}",
                    f"--channels={self.channels}",
                    "--raw",
                    "--latency-msec=25",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )

    def stop_monitor_pacat(self) -> None:
        if self.monitor_pacat is not None:
            try:
                if self.monitor_pacat.stdin:
                    self.monitor_pacat.stdin.close()
                self.monitor_pacat.terminate()
                self.monitor_pacat.wait(timeout=0.5)
            except Exception:
                pass
            self.monitor_pacat = None

    def write_monitor(self, monitor_bytes: bytes) -> None:
        with self._lock:
            if self.monitor_pacat is None or self.monitor_pacat.poll() is not None:
                self.start_monitor_pacat()
            if self.monitor_pacat is not None and self.monitor_pacat.stdin is not None:
                try:
                    self.monitor_pacat.stdin.write(monitor_bytes)
                except BrokenPipeError:
                    self.start_monitor_pacat()
            self._reset_idle_timer()

    def cleanup(self) -> None:
        with self._lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            self.stop_mic_pacat()
            self.stop_monitor_pacat()

        if self.source_module_id is not None:
            subprocess.run(
                ["pactl", "unload-module", self.source_module_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.source_module_id = None

        if self.sink_module_id is not None:
            subprocess.run(
                ["pactl", "unload-module", self.sink_module_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.sink_module_id = None
