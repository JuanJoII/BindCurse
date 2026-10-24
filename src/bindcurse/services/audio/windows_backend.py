from __future__ import annotations

from typing import Any

import sounddevice as sd

from bindcurse.core.config import CHANNELS, SAMPLE_RATE


class WindowsAudioBackend:
    """Manages virtual mic via VB-Cable and monitoring via PortAudio/sounddevice."""

    def __init__(
        self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.sd_mic_stream: Any = None
        self.sd_monitor_stream: Any = None

    def setup(self) -> tuple[bool, str, str]:
        """
        Finds VB-Cable input device and opens a RawOutputStream.
        Returns (success, device_label, status_message).
        """
        try:
            devices = sd.query_devices()
            cable_index: int | None = None
            cable_name: str = ""
            last_exc: Exception | None = None

            for idx, dev in enumerate(devices):
                name = str(dev.get("name", ""))
                max_out = int(dev.get("max_output_channels", 0))
                if max_out > 0 and (
                    "cable input" in name.lower() or "vb-audio" in name.lower()
                ):
                    try:
                        stream = sd.RawOutputStream(
                            samplerate=self.sample_rate,
                            channels=self.channels,
                            dtype="int16",
                            device=idx,
                            latency="low",
                        )
                        stream.start()
                        self.sd_mic_stream = stream
                        cable_index = idx
                        cable_name = name
                        return True, cable_name, f"Conectado a VB-Cable: {cable_name}"
                    except Exception as exc:
                        last_exc = exc
                        continue

            if cable_index is None:
                if last_exc is not None:
                    return False, "Error", f"Error abriendo VB-Cable: {last_exc}"
                msg = (
                    "No se detectó 'CABLE Input (VB-Audio Virtual Cable)'. "
                    "Instala VB-Cable en Windows y reinicia el micrófono virtual."
                )
                return False, "No encontrado", msg

            return True, cable_name, f"Conectado a VB-Cable: {cable_name}"
        except Exception as exc:
            return False, "Error", f"Error abriendo VB-Cable: {exc}"

    def write_mic(self, mic_bytes: bytes) -> None:
        if self.sd_mic_stream is not None:
            try:
                self.sd_mic_stream.write(mic_bytes)
            except Exception:
                pass

    def ensure_monitor(self) -> None:
        if self.sd_monitor_stream is not None:
            return
        try:
            stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                latency="low",
            )
            stream.start()
            self.sd_monitor_stream = stream
        except Exception:
            self.sd_monitor_stream = None

    def stop_monitor(self) -> None:
        if self.sd_monitor_stream is not None:
            try:
                self.sd_monitor_stream.stop()
                self.sd_monitor_stream.close()
            except Exception:
                pass
            self.sd_monitor_stream = None

    def write_monitor(self, monitor_bytes: bytes) -> None:
        self.ensure_monitor()
        if self.sd_monitor_stream is not None:
            try:
                self.sd_monitor_stream.write(monitor_bytes)
            except Exception:
                pass

    def cleanup(self) -> None:
        if self.sd_mic_stream is not None:
            try:
                self.sd_mic_stream.stop()
                self.sd_mic_stream.close()
            except Exception:
                pass
            self.sd_mic_stream = None

        self.stop_monitor()
