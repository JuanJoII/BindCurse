from __future__ import annotations

from typing import Any

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
        self._monitor_channels: int = channels
        self._monitor_failed: bool = False

    @staticmethod
    def _hostapi_priority(api_name: str) -> int:
        lower = api_name.lower()
        if "mme" in lower:
            return 0
        if "wasapi" in lower:
            return 1
        if "directsound" in lower:
            return 2
        return 3

    def setup(self) -> tuple[bool, str, str]:
        """
        Finds VB-Cable input device and opens a RawOutputStream.
        Prioritizes 'CABLE Input' over 'CABLE In 16ch' and prioritizes MME because
        VB-Audio Virtual Cable's virtual loopback bridge functions on Windows MME/WDM.
        Returns (success, device_label, status_message).
        """
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            hostapis = sd.query_hostapis()

            def _api_rank(hostapi_idx: int) -> int:
                try:
                    return self._hostapi_priority(
                        str(hostapis[hostapi_idx].get("name", ""))
                    )
                except Exception:
                    return 3

            candidates: list[tuple[int, int, int, str]] = []
            for idx, dev in enumerate(devices):
                name = str(dev.get("name", ""))
                lower = name.lower()
                max_out = int(dev.get("max_output_channels", 0))
                if max_out <= 0:
                    continue
                if "cable input" in lower:
                    match_tier = 0
                elif "vb-audio" in lower and "16ch" not in lower:
                    match_tier = 1
                elif "vb-audio" in lower:
                    match_tier = 2
                else:
                    continue
                api_rank = _api_rank(int(dev.get("hostapi", 0)))
                candidates.append((match_tier, api_rank, idx, name))

            if not candidates:
                msg = (
                    "No se detectó 'CABLE Input (VB-Audio Virtual Cable)'. "
                    "Instala VB-Cable en Windows y reinicia el micrófono virtual."
                )
                return False, "No encontrado", msg

            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            last_exc: Exception | None = None
            for _, _, cable_index, cable_name in candidates:
                try:
                    stream = sd.RawOutputStream(
                        samplerate=self.sample_rate,
                        channels=self.channels,
                        dtype="int16",
                        device=cable_index,
                        latency="low",
                    )
                    stream.start()
                    self.sd_mic_stream = stream
                    return True, cable_name, f"Conectado a VB-Cable: {cable_name}"
                except Exception as exc:
                    last_exc = exc

            return False, "Error", f"Error abriendo VB-Cable: {last_exc}"
        except Exception as exc:
            return False, "Error", f"Error abriendo VB-Cable: {exc}"

    def write_mic(self, mic_bytes: bytes) -> None:
        stream = self.sd_mic_stream
        if stream is not None:
            try:
                if stream.stopped:
                    stream.start()
                stream.write(mic_bytes)
            except Exception:
                pass

    def ensure_monitor(self) -> None:
        if self.sd_monitor_stream is not None or self._monitor_failed:
            return
        try:
            import sounddevice as sd

            # 1. Try opening system default device with requested channels
            try:
                stream = sd.RawOutputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="int16",
                    device=None,
                    latency="low",
                )
                stream.start()
                self.sd_monitor_stream = stream
                self._monitor_channels = self.channels
                self._monitor_failed = False
                return
            except Exception:
                pass

            # 2. Fallback: try opening system default device with stereo (2 channels)
            try:
                stream = sd.RawOutputStream(
                    samplerate=self.sample_rate,
                    channels=2,
                    dtype="int16",
                    device=None,
                    latency="low",
                )
                stream.start()
                self.sd_monitor_stream = stream
                self._monitor_channels = 2
                self._monitor_failed = False
                return
            except Exception:
                pass

            self._monitor_failed = True
        except Exception:
            self.sd_monitor_stream = None
            self._monitor_failed = True

    def stop_monitor(self) -> None:
        self._monitor_failed = False
        if self.sd_monitor_stream is not None:
            try:
                self.sd_monitor_stream.stop()
                self.sd_monitor_stream.close()
            except Exception:
                pass
            self.sd_monitor_stream = None

    def write_monitor(self, monitor_bytes: bytes) -> None:
        self.ensure_monitor()
        stream = self.sd_monitor_stream
        if stream is not None:
            try:
                if stream.stopped:
                    stream.start()
                if self._monitor_channels == 2 and self.channels == 1:
                    stereo_bytes = bytearray(len(monitor_bytes) * 2)
                    stereo_bytes[0::4] = monitor_bytes[0::2]
                    stereo_bytes[1::4] = monitor_bytes[1::2]
                    stereo_bytes[2::4] = monitor_bytes[0::2]
                    stereo_bytes[3::4] = monitor_bytes[1::2]
                    stream.write(stereo_bytes)
                else:
                    stream.write(monitor_bytes)
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
