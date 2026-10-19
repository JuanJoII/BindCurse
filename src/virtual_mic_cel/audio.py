from __future__ import annotations

import atexit
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 48000
CHANNELS = 1
VIRTUAL_SINK_NAME = "virtual_mic_cel_sink"
VIRTUAL_SOURCE_NAME = "virtual_mic_cel_source"
VIRTUAL_MIC_DESCRIPTION = "Virtual_Mic_Cel"


class VirtualMicEngine:
    """
    Manages the virtual microphone device (PipeWire/PulseAudio on Linux, VB-Cable on Windows)
    and optional real-time monitoring through the system's default audio output.
    """

    def __init__(
        self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.platform = (
            "linux"
            if sys.platform.startswith("linux")
            else ("windows" if sys.platform.startswith("win") else sys.platform)
        )

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
        self._sink_module_id: str | None = None
        self._source_module_id: str | None = None

        # Linux pacat subprocesses
        self._mic_pacat: subprocess.Popen[bytes] | None = None
        self._monitor_pacat: subprocess.Popen[bytes] | None = None

        # Sounddevice streams (Windows VB-Cable + fallback monitor)
        self._sd_mic_stream: Any = None
        self._sd_monitor_stream: Any = None

        atexit.register(self.shutdown)

    def setup_virtual_mic(self) -> tuple[bool, str]:
        """Creates or connects to the system virtual microphone."""
        with self._lock:
            self._cleanup_virtual_mic_locked()
            if self.platform == "linux":
                return self._setup_linux_pipewire_locked()
            elif self.platform == "windows":
                return self._setup_windows_vbcable_locked()
            else:
                self.status_message = f"Plataforma no soportada: {self.platform}"
                self.virtual_mic_ready = False
                return False, self.status_message

    def _cleanup_stale_linux_modules(self) -> None:
        """Unloads any leftover virtual_mic_cel modules from previous sessions."""
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
            # Unload remap-source first, then null-sink (reverse order)
            for mod_id in reversed(mod_ids):
                subprocess.run(
                    ["pactl", "unload-module", mod_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                )
        except Exception:
            pass

    def _setup_linux_pipewire_locked(self) -> tuple[bool, str]:
        if not shutil.which("pactl") or not shutil.which("pacat"):
            self.virtual_mic_ready = False
            self.backend_name = "PipeWire / PulseAudio (Falta pactl/pacat)"
            self.status_message = (
                "No se encontró 'pactl' o 'pacat'. Instala pipewire-pulse / libpulse."
            )
            return False, self.status_message

        self._cleanup_stale_linux_modules()

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
            self._sink_module_id = sink_out

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
            self._source_module_id = src_out

            self._start_linux_mic_pacat_locked()

            self.virtual_mic_ready = True
            self.backend_name = "PipeWire / PulseAudio"
            self.device_label = f"{VIRTUAL_MIC_DESCRIPTION} ({VIRTUAL_SOURCE_NAME})"
            self.status_message = f"Micrófono virtual activo: {VIRTUAL_MIC_DESCRIPTION} (48000 Hz, 16-bit, Mono)"
            return True, self.status_message
        except Exception as exc:
            self._cleanup_virtual_mic_locked()
            self.virtual_mic_ready = False
            self.backend_name = "PipeWire / PulseAudio (Error)"
            self.status_message = (
                f"Error creando micrófono virtual en PipeWire/PulseAudio: {exc}"
            )
            return False, self.status_message

    def _start_linux_mic_pacat_locked(self) -> None:
        if self._mic_pacat is not None and self._mic_pacat.poll() is None:
            return
        self._mic_pacat = subprocess.Popen(
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

    def _start_linux_monitor_pacat_locked(self) -> None:
        if self._monitor_pacat is not None and self._monitor_pacat.poll() is None:
            return
        if shutil.which("pacat"):
            self._monitor_pacat = subprocess.Popen(
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

    def _stop_linux_monitor_pacat_locked(self) -> None:
        if self._monitor_pacat is not None:
            try:
                if self._monitor_pacat.stdin:
                    self._monitor_pacat.stdin.close()
                self._monitor_pacat.terminate()
                self._monitor_pacat.wait(timeout=0.5)
            except Exception:
                pass
            self._monitor_pacat = None

    def _setup_windows_vbcable_locked(self) -> tuple[bool, str]:
        try:
            devices = sd.query_devices()
            cable_index: int | None = None
            cable_name: str = ""
            for idx, dev in enumerate(devices):
                name = str(dev.get("name", ""))
                max_out = int(dev.get("max_output_channels", 0))
                if max_out > 0 and (
                    "cable input" in name.lower() or "vb-audio" in name.lower()
                ):
                    cable_index = idx
                    cable_name = name
                    break

            if cable_index is None:
                self.virtual_mic_ready = False
                self.backend_name = "VB-Cable (No detectado)"
                self.device_label = "No encontrado"
                self.status_message = (
                    "No se detectó 'CABLE Input (VB-Audio Virtual Cable)'. "
                    "Instala VB-Cable en Windows y reinicia el micrófono virtual."
                )
                return False, self.status_message

            stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=cable_index,
                latency="low",
            )
            stream.start()
            self._sd_mic_stream = stream
            self.virtual_mic_ready = True
            self.backend_name = "VB-Audio Virtual Cable"
            self.device_label = cable_name
            self.status_message = f"Conectado a VB-Cable: {cable_name}"
            return True, self.status_message
        except Exception as exc:
            self.virtual_mic_ready = False
            self.backend_name = "VB-Cable (Error)"
            self.status_message = f"Error abriendo VB-Cable: {exc}"
            return False, self.status_message

    def set_monitor_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.monitor_enabled = enabled
            if not enabled:
                self._stop_linux_monitor_pacat_locked()
                if self._sd_monitor_stream is not None:
                    try:
                        self._sd_monitor_stream.stop()
                        self._sd_monitor_stream.close()
                    except Exception:
                        pass
                    self._sd_monitor_stream = None
            else:
                if self.platform == "linux" and shutil.which("pacat"):
                    self._start_linux_monitor_pacat_locked()
                else:
                    self._ensure_sd_monitor_locked()

    def _ensure_sd_monitor_locked(self) -> None:
        if self._sd_monitor_stream is not None:
            return
        try:
            stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                latency="low",
            )
            stream.start()
            self._sd_monitor_stream = stream
        except Exception:
            self._sd_monitor_stream = None

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
                samples = np.frombuffer(raw_bytes, dtype="<i2")
                scaled = np.clip(
                    samples.astype(np.float32) * self.mic_gain, -32768.0, 32767.0
                ).astype("<i2")
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
                    samples = np.frombuffer(raw_bytes, dtype="<i2")
                mon_scaled = np.clip(
                    samples.astype(np.float32) * self.monitor_volume, -32768.0, 32767.0
                ).astype("<i2")
                monitor_bytes = mon_scaled.tobytes()
            else:
                monitor_bytes = raw_bytes

        with self._lock:
            # Inject into Virtual Microphone
            if self.virtual_mic_ready:
                if self.platform == "linux":
                    if self._mic_pacat is None or self._mic_pacat.poll() is not None:
                        self._start_linux_mic_pacat_locked()
                    if (
                        self._mic_pacat is not None
                        and self._mic_pacat.stdin is not None
                    ):
                        try:
                            self._mic_pacat.stdin.write(mic_bytes)
                        except BrokenPipeError:
                            self._start_linux_mic_pacat_locked()
                elif self.platform == "windows" and self._sd_mic_stream is not None:
                    try:
                        self._sd_mic_stream.write(mic_bytes)
                    except Exception:
                        pass

            # Inject into System Audio Output Monitor if enabled
            if self.monitor_enabled and monitor_bytes is not None:
                if self.platform == "linux" and shutil.which("pacat"):
                    if (
                        self._monitor_pacat is None
                        or self._monitor_pacat.poll() is not None
                    ):
                        self._start_linux_monitor_pacat_locked()
                    if (
                        self._monitor_pacat is not None
                        and self._monitor_pacat.stdin is not None
                    ):
                        try:
                            self._monitor_pacat.stdin.write(monitor_bytes)
                        except BrokenPipeError:
                            self._start_linux_monitor_pacat_locked()
                else:
                    self._ensure_sd_monitor_locked()
                    if self._sd_monitor_stream is not None:
                        try:
                            self._sd_monitor_stream.write(monitor_bytes)
                        except Exception:
                            pass

    def send_test_tone_udp(self, port: int = 5005, duration_sec: float = 1.5) -> None:
        """
        Spawns a background thread that sends a pleasant 440Hz/554Hz PCM test signal
        over UDP to 127.0.0.1:port so the user can verify the virtual mic and monitor.
        """

        def _worker() -> None:
            chunk_samples = 960  # 20ms at 48000Hz
            total_chunks = int((duration_sec * self.sample_rate) / chunk_samples)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    for i in range(total_chunks):
                        t = (np.arange(chunk_samples) + i * chunk_samples) / float(
                            self.sample_rate
                        )
                        freq = 440.0 if i < (total_chunks // 2) else 554.37
                        # Smooth envelope at start/end
                        wave = np.sin(2.0 * np.pi * freq * t) * 0.35
                        pcm = (wave * 32767.0).astype("<i2").tobytes()
                        sock.sendto(pcm, ("127.0.0.1", port))
                        time.sleep(chunk_samples / float(self.sample_rate))
            except Exception:
                pass

        threading.Thread(target=_worker, name="UDPTestTone", daemon=True).start()

    def _cleanup_virtual_mic_locked(self) -> None:
        if self._mic_pacat is not None:
            try:
                if self._mic_pacat.stdin:
                    self._mic_pacat.stdin.close()
                self._mic_pacat.terminate()
                self._mic_pacat.wait(timeout=0.5)
            except Exception:
                pass
            self._mic_pacat = None

        self._stop_linux_monitor_pacat_locked()

        if self._sd_mic_stream is not None:
            try:
                self._sd_mic_stream.stop()
                self._sd_mic_stream.close()
            except Exception:
                pass
            self._sd_mic_stream = None

        if self._sd_monitor_stream is not None:
            try:
                self._sd_monitor_stream.stop()
                self._sd_monitor_stream.close()
            except Exception:
                pass
            self._sd_monitor_stream = None

        if self._source_module_id is not None:
            subprocess.run(
                ["pactl", "unload-module", self._source_module_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._source_module_id = None

        if self._sink_module_id is not None:
            subprocess.run(
                ["pactl", "unload-module", self._sink_module_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._sink_module_id = None

        self.virtual_mic_ready = False

    def shutdown(self) -> None:
        with self._lock:
            self._cleanup_virtual_mic_locked()
