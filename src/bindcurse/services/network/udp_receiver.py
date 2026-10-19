from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Tuple

import numpy as np

from bindcurse.config import (
    DEFAULT_BUFFER_SIZE,
    DEFAULT_UDP_PORT,
    IS_LINUX,
    IS_WINDOWS,
    PCM_DTYPE,
)
from bindcurse.services.network.os_network import (
    get_linux_ips,
    get_windows_ips,
    probe_primary_ip,
)


@dataclass
class DeviceInfo:
    ip: str
    port: int
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    packets: int = 0
    bytes_received: int = 0
    kbps: float = 0.0
    pps: float = 0.0
    rms_level: float = 0.0
    peak_level: float = 0.0
    blocked: bool = False
    _window_bytes: int = 0
    _window_packets: int = 0
    _window_start: float = field(default_factory=time.time)

    @property
    def address_str(self) -> str:
        return f"{self.ip}:{self.port}"

    @property
    def is_active(self) -> bool:
        return (time.time() - self.last_seen) < 2.0


def get_local_ips() -> List[Tuple[str, str]]:
    """
    Returns a list of (interface_or_label, ip_address) tuples for active local IPv4 addresses.
    The primary outbound LAN IP is placed first.
    """
    results: List[Tuple[str, str]] = []
    seen_ips: set[str] = set()

    primary_ip = probe_primary_ip()

    if IS_LINUX:
        results.extend(get_linux_ips(primary_ip))
    elif IS_WINDOWS:
        results.extend(get_windows_ips(primary_ip))

    for _, ip in results:
        seen_ips.add(ip)

    # Fallback if primary wasn't in interface lists
    if primary_ip and primary_ip not in seen_ips:
        seen_ips.add(primary_ip)
        results.insert(0, ("Principal", primary_ip))

    # Generic fallback
    if not results:
        results.extend(get_windows_ips(primary_ip))
        for _, ip in results:
            seen_ips.add(ip)

    if not results:
        results.append(("Localhost", "127.0.0.1"))

    return results


class UDPAudioReceiver:
    """
    Listens for raw PCM (16-bit signed little-endian, 48000Hz, mono) UDP packets
    on `port` (default 5005), tracks connected devices, and forwards audio buffers
    in real time to `on_audio_chunk`.

    When `compute_levels` is False (background mode with GUI closed), skips all
    NumPy float32 RMS/Peak calculations to minimize CPU and memory overhead.
    """

    def __init__(
        self,
        port: int = DEFAULT_UDP_PORT,
        on_audio_chunk: Callable[[bytes, tuple[str, int]], None] | None = None,
    ) -> None:
        self.port = port
        self.on_audio_chunk = on_audio_chunk
        self.running = False
        self.compute_levels = True
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._devices: dict[str, DeviceInfo] = {}
        self.total_packets = 0
        self.total_bytes = 0
        self.current_rms = 0.0
        self.current_peak = 0.0
        self.last_packet_time = 0.0
        self.last_error: str | None = None

    def start(self) -> None:
        if self.running:
            return
        self.last_error = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, DEFAULT_BUFFER_SIZE)
            except OSError:
                pass
            sock.bind(("0.0.0.0", self.port))
            sock.settimeout(0.25)
            self._sock = sock
        except OSError as exc:
            self.last_error = f"No se pudo abrir UDP :{self.port} ({exc})"
            raise

        self.running = True
        self._thread = threading.Thread(
            target=self._recv_loop, name="UDPAudioReceiver", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def get_devices_snapshot(self) -> list[DeviceInfo]:
        now = time.time()
        with self._lock:
            snapshot: list[DeviceInfo] = []
            for dev in self._devices.values():
                elapsed = now - dev._window_start
                if elapsed >= 1.0:
                    dev.kbps = (dev._window_bytes * 8.0) / (elapsed * 1000.0)
                    dev.pps = dev._window_packets / elapsed
                    dev._window_bytes = 0
                    dev._window_packets = 0
                    dev._window_start = now
                if now - dev.last_seen > 0.25:
                    dev.rms_level *= 0.6
                    dev.peak_level *= 0.6
                snapshot.append(
                    DeviceInfo(
                        ip=dev.ip,
                        port=dev.port,
                        first_seen=dev.first_seen,
                        last_seen=dev.last_seen,
                        packets=dev.packets,
                        bytes_received=dev.bytes_received,
                        kbps=dev.kbps,
                        pps=dev.pps,
                        rms_level=dev.rms_level,
                        peak_level=dev.peak_level,
                        blocked=dev.blocked,
                    )
                )
            if now - self.last_packet_time > 0.15:
                self.current_rms *= 0.7
                self.current_peak *= 0.7
            return sorted(snapshot, key=lambda d: d.last_seen, reverse=True)

    def toggle_block_ip(self, ip: str) -> bool:
        with self._lock:
            dev = self._devices.get(ip)
            if dev is not None:
                dev.blocked = not dev.blocked
                return dev.blocked
        return False

    def clear_inactive_devices(self) -> int:
        now = time.time()
        removed = 0
        with self._lock:
            to_delete = [
                ip for ip, d in self._devices.items() if (now - d.last_seen) >= 2.0
            ]
            for ip in to_delete:
                del self._devices[ip]
                removed += 1
        return removed

    def _recv_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return

        while self.running:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    time.sleep(0.05)
                break

            if not data:
                continue

            valid_len = len(data) & ~1
            if valid_len <= 0:
                continue
            if valid_len != len(data):
                data = data[:valid_len]

            ip, port = addr[0], addr[1]
            now = time.time()

            rms = 0.0
            peak = 0.0
            if self.compute_levels:
                samples = np.frombuffer(data, dtype=PCM_DTYPE)
                if samples.size > 0:
                    float_samples = samples.astype(np.float32) * (1.0 / 32768.0)
                    rms = float(np.sqrt(np.mean(float_samples * float_samples)))
                    peak = float(np.max(np.abs(float_samples)))

            with self._lock:
                dev = self._devices.get(ip)
                if dev is None:
                    dev = DeviceInfo(ip=ip, port=port, first_seen=now, last_seen=now)
                    self._devices[ip] = dev
                else:
                    dev.port = port
                    dev.last_seen = now

                if dev.blocked:
                    continue

                dev.packets += 1
                dev.bytes_received += valid_len
                dev._window_bytes += valid_len
                dev._window_packets += 1

                if self.compute_levels:
                    dev.rms_level = min(1.0, rms)
                    dev.peak_level = min(1.0, peak)
                    self.current_rms = min(1.0, rms)
                    self.current_peak = min(1.0, peak)

                elapsed = now - dev._window_start
                if elapsed >= 0.5:
                    dev.kbps = (dev._window_bytes * 8.0) / (elapsed * 1000.0)
                    dev.pps = dev._window_packets / elapsed
                    dev._window_bytes = 0
                    dev._window_packets = 0
                    dev._window_start = now

                self.total_packets += 1
                self.total_bytes += valid_len
                self.last_packet_time = now

            if self.on_audio_chunk is not None:
                try:
                    self.on_audio_chunk(data, (ip, port))
                except Exception:
                    pass
