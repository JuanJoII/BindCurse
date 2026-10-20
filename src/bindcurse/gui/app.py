from __future__ import annotations

import gc
import json
import shutil
import socket
import subprocess
import sys
import threading
from collections import deque

from bindcurse.config import UserSettings
from bindcurse.services.audio import VirtualMicEngine
from bindcurse.services.network import (
    DeviceInfo,
    UDPAudioReceiver,
    get_local_ips,
)
from bindcurse.services.tray import (
    TrayCallbacks,
    TrayIcon,
    TrayState,
    create_tray,
)


def _notify_user(title: str, body: str) -> None:
    """Sends a non-blocking desktop notification if notify-send is available."""
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(
                ["notify-send", "-a", "BindCurse", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


class VirtualMicApp:
    def __init__(self, udp_port: int | None = None) -> None:
        self.settings = UserSettings.load()
        self.udp_port = udp_port if udp_port is not None else self.settings.udp_port
        self.control_port = self.udp_port + 1
        self.audio = VirtualMicEngine()

        # Apply persisted user audio parameters
        self.audio.mic_gain = self.settings.mic_gain
        self.audio.monitor_volume = self.settings.monitor_volume
        self.audio.mic_muted = self.settings.mic_muted
        self.audio.monitor_enabled = self.settings.monitor_enabled

        self.receiver = UDPAudioReceiver(
            port=self.udp_port,
            on_audio_chunk=self.audio.process_audio_chunk,
        )
        self.local_ips: list[tuple[str, str]] = get_local_ips()
        known_ips = [ip for _, ip in self.local_ips]
        if self.settings.preferred_ip and self.settings.preferred_ip in known_ips:
            self.selected_ip = self.settings.preferred_ip
        else:
            self.selected_ip = known_ips[0] if known_ips else "127.0.0.1"

        self.level_history: deque[float] = deque([0.0] * 120, maxlen=120)
        self._large_font: int | str | None = None
        self._known_table_ips: list[str] = []

        # Background / lifecycle state
        self.keep_in_background: bool = self.settings.keep_in_background
        self.gui_active: bool = False
        self._quit_requested: bool = False
        self._hide_requested: bool = False
        self._wake_event = threading.Event()
        self._gui_close_event = threading.Event()
        self._gui_proc: subprocess.Popen[bytes] | None = None
        self._ipc_sock: socket.socket | None = None
        self._ipc_thread: threading.Thread | None = None

        # System tray icon (Waybar/Quickshell via StatusNotifierItem, Windows notification area)
        self.tray: TrayIcon = create_tray(
            TrayCallbacks(
                get_state=self.get_tray_state,
                show_window=lambda: self._handle_ipc_command("SHOW"),
                toggle_monitor=lambda: self._handle_ipc_command("TOGGLE_MONITOR"),
                toggle_mute=lambda: self._handle_ipc_command("TOGGLE_MUTE"),
                quit_app=lambda: self._handle_ipc_command("STOP"),
            )
        )

    def get_tray_state(self) -> TrayState:
        active = 0
        for dev in self.receiver.get_devices_snapshot():
            if dev.is_active and not dev.blocked:
                active += 1
        return TrayState(
            mic_ready=self.audio.virtual_mic_ready,
            muted=self.audio.mic_muted,
            monitor=self.audio.monitor_enabled,
            active_devices=active,
            ip=self.selected_ip,
            port=self.udp_port,
        )

    def start_services(self) -> None:
        self.audio.setup_virtual_mic()
        if self.audio.monitor_enabled:
            self.audio.set_monitor_enabled(True)
        self.receiver.start()
        self._start_ipc_server()
        self.tray.start()

    def stop_services(self) -> None:
        self._quit_requested = True
        self._hide_requested = True
        self._gui_close_event.set()
        self._wake_event.set()
        self._terminate_gui_process()
        self.settings.flush()
        self.tray.stop()
        if self._ipc_sock is not None:
            try:
                self._ipc_sock.close()
            except OSError:
                pass
            self._ipc_sock = None
        self.receiver.stop()
        self.audio.shutdown()

    def _terminate_gui_process(self) -> None:
        proc = self._gui_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _start_ipc_server(self) -> None:
        if self._ipc_sock is not None:
            return
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", self.control_port))
            srv.listen(8)
            srv.settimeout(0.5)
            self._ipc_sock = srv
            self._ipc_thread = threading.Thread(
                target=self._ipc_loop, name="VirtualMicIPC", daemon=True
            )
            self._ipc_thread.start()
        except OSError:
            pass

    def _ipc_loop(self) -> None:
        srv = self._ipc_sock
        if srv is None:
            return
        while not self._quit_requested:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with conn:
                try:
                    conn.settimeout(0.5)
                    raw_cmd = (
                        conn.recv(512)
                        .decode("utf-8", errors="replace")
                        .strip()
                    )
                    resp = self._handle_ipc_command(raw_cmd)
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                except Exception:
                    pass

    def _serialize_devices(
        self, devices: list[DeviceInfo]
    ) -> list[dict[str, object]]:
        return [
            {
                "ip": d.ip,
                "port": d.port,
                "address": d.address_str,
                "display_name": d.display_name,
                "name": d.client_name,
                "state": d.session_state,
                "active": d.is_active,
                "packets": d.packets,
                "lost": d.lost_packets,
                "bytes": d.bytes_received,
                "kbps": d.kbps,
                "pps": d.pps,
                "rms_level": d.rms_level,
                "peak_level": d.peak_level,
                "last_seen": d.last_seen,
                "blocked": d.blocked,
            }
            for d in devices
        ]

    def _handle_ipc_command(self, raw_cmd: str) -> dict[str, object]:
        parts = raw_cmd.split("|", 1)
        cmd = parts[0].strip().upper()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "SHOW":
            self._hide_requested = False
            if not self.gui_active:
                self._wake_event.set()
            return {"ok": True, "action": "show", "gui_active": self.gui_active}
        if cmd == "HIDE":
            self._hide_requested = True
            self._gui_close_event.set()
            return {"ok": True, "action": "hide"}
        if cmd == "HIDE_BG":
            self.keep_in_background = True
            self._hide_requested = True
            self._gui_close_event.set()
            return {"ok": True, "action": "hide_bg"}
        if cmd in ("STOP", "QUIT"):
            self._quit_requested = True
            self._hide_requested = True
            self._gui_close_event.set()
            self._wake_event.set()
            return {"ok": True, "action": "stop"}
        if cmd == "TOGGLE_MONITOR":
            self._on_monitor_toggled(0, not self.audio.monitor_enabled)
            return {"ok": True, "monitor_enabled": self.audio.monitor_enabled}
        if cmd == "SET_MONITOR":
            self._on_monitor_toggled(0, arg in ("1", "TRUE"))
            return {"ok": True, "monitor_enabled": self.audio.monitor_enabled}
        if cmd == "TOGGLE_MUTE":
            self._on_mute_toggled(0, not self.audio.mic_muted)
            return {"ok": True, "mic_muted": self.audio.mic_muted}
        if cmd == "SET_MUTE":
            self._on_mute_toggled(0, arg in ("1", "TRUE"))
            return {"ok": True, "mic_muted": self.audio.mic_muted}
        if cmd == "SET_KEEP_BG":
            self._on_bg_toggled(0, arg in ("1", "TRUE"))
            return {"ok": True, "keep_in_background": self.keep_in_background}
        if cmd == "SET_GAIN":
            try:
                self._on_mic_gain_changed(0, float(arg))
            except ValueError:
                pass
            return {"ok": True, "mic_gain": self.audio.mic_gain}
        if cmd == "SET_MONITOR_VOL":
            try:
                self._on_monitor_vol_changed(0, float(arg))
            except ValueError:
                pass
            return {"ok": True, "monitor_volume": self.audio.monitor_volume}
        if cmd == "SET_IP":
            if arg:
                self.selected_ip = arg
                self.settings.preferred_ip = arg
                self.settings.save()
            return {"ok": True, "ip": self.selected_ip}
        if cmd == "REFRESH_IPS":
            self._refresh_ips_callback()
            return {
                "ok": True,
                "local_ips": self.local_ips,
                "ip": self.selected_ip,
            }
        if cmd == "RECREATE_MIC":
            self._on_recreate_mic()
            return {
                "ok": True,
                "virtual_mic_ready": self.audio.virtual_mic_ready,
                "device_label": self.audio.device_label,
                "backend_name": self.audio.backend_name,
                "status_message": self.audio.status_message,
            }
        if cmd == "TEST_TONE":
            self._on_test_tone()
            return {"ok": True}
        if cmd == "CLEAR_INACTIVE":
            self.receiver.clear_inactive_devices()
            devices = self.receiver.get_devices_snapshot()
            return {"ok": True, "devices": self._serialize_devices(devices)}
        if cmd == "TOGGLE_BLOCK":
            if arg:
                self.receiver.toggle_block_ip(arg)
            devices = self.receiver.get_devices_snapshot()
            return {"ok": True, "devices": self._serialize_devices(devices)}

        # Default: STATUS
        devices = self.receiver.get_devices_snapshot()
        return {
            "ok": True,
            "ip": self.selected_ip,
            "local_ips": self.local_ips,
            "udp_port": self.udp_port,
            "gui_active": self.gui_active,
            "should_close": self._hide_requested or self._quit_requested,
            "keep_in_background": self.keep_in_background,
            "virtual_mic_ready": self.audio.virtual_mic_ready,
            "device_label": self.audio.device_label,
            "backend_name": self.audio.backend_name,
            "status_message": self.audio.status_message,
            "monitor_enabled": self.audio.monitor_enabled,
            "mic_muted": self.audio.mic_muted,
            "mic_gain": self.audio.mic_gain,
            "monitor_volume": self.audio.monitor_volume,
            "current_rms": self.receiver.current_rms,
            "current_peak": self.receiver.current_peak,
            "total_packets": self.receiver.total_packets,
            "total_bytes": self.receiver.total_bytes,
            "devices": self._serialize_devices(devices),
        }

    def _refresh_ips_callback(self) -> None:
        self.local_ips = get_local_ips()
        self.selected_ip = (
            self.local_ips[0][1] if self.local_ips else "127.0.0.1"
        )

    def _on_ip_combo_selected(self, _sender: int, app_data: str) -> None:
        if app_data:
            ip = app_data.split()[0].strip()
            self.selected_ip = ip
            self.settings.preferred_ip = ip
            self.settings.save()

    def _on_monitor_toggled(self, _sender: int, app_data: bool) -> None:
        self.audio.set_monitor_enabled(bool(app_data))
        self.settings.monitor_enabled = self.audio.monitor_enabled
        self.settings.save()

    def _on_mute_toggled(self, _sender: int, app_data: bool) -> None:
        self.audio.mic_muted = bool(app_data)
        self.settings.mic_muted = self.audio.mic_muted
        self.settings.save()

    def _on_bg_toggled(self, _sender: int, app_data: bool) -> None:
        self.keep_in_background = bool(app_data)
        self.settings.keep_in_background = self.keep_in_background
        self.settings.save()

    def _on_mic_gain_changed(self, _sender: int, app_data: float) -> None:
        self.audio.mic_gain = float(app_data) / 100.0
        self.settings.mic_gain = self.audio.mic_gain
        self.settings.save_debounced()

    def _on_monitor_vol_changed(self, _sender: int, app_data: float) -> None:
        self.audio.monitor_volume = float(app_data) / 100.0
        self.settings.monitor_volume = self.audio.monitor_volume
        self.settings.save_debounced()

    def _on_recreate_mic(self) -> None:
        self.audio.setup_virtual_mic()

    def _on_test_tone(self) -> None:
        self.audio.send_test_tone_udp(port=self.udp_port, duration_sec=1.5)

    def _on_hide_to_background(self) -> None:
        self.keep_in_background = True
        self._hide_requested = True
        self._gui_close_event.set()

    def _on_quit_completely(self) -> None:
        self._quit_requested = True
        self._hide_requested = True
        self._gui_close_event.set()

    def _on_clear_inactive(self) -> None:
        self.receiver.clear_inactive_devices()

    def _on_toggle_block_device(
        self, _sender: int, _app_data: object, user_data: str
    ) -> None:
        self.receiver.toggle_block_ip(user_data)

    def _run_gui_session(self) -> None:
        """
        Spawns an isolated child process to host the Dear PyGui / OpenGL viewport.
        When the user closes the window (or HIDE/STOP is requested), the child process
        exits completely, returning 100% of GPU driver, OpenGL, GLFW, and UI heap memory
        to the operating system without polluting the background daemon process.
        """
        self._start_ipc_server()
        self.gui_active = True
        self._hide_requested = False
        self._gui_close_event.clear()
        self.receiver.compute_levels = True

        proc: subprocess.Popen[bytes] | None = None
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "bindcurse.gui.window",
                    str(self.control_port),
                ],
                stdin=subprocess.DEVNULL,
            )
            self._gui_proc = proc

            while proc.poll() is None:
                if self._quit_requested or self._hide_requested:
                    self._terminate_gui_process()
                    break
                self._gui_close_event.wait(timeout=0.05)
        finally:
            self._terminate_gui_process()
            self._gui_proc = None
            self.gui_active = False
            self.receiver.compute_levels = False
            gc.collect()

    def run(self, start_hidden: bool = False) -> None:
        self.start_services()
        try:
            show_window = not start_hidden
            if start_hidden:
                self.receiver.compute_levels = False
                print(
                    f"[BindCurse] Activo en 2do plano en {self.selected_ip}:{self.udp_port}. "
                    "Ejecuta 'uv run bindcurse' para abrir la interfaz o '--stop' para apagarlo."
                )

            while not self._quit_requested:
                if show_window:
                    self._run_gui_session()

                    if self._quit_requested or not self.keep_in_background:
                        break

                    show_window = False

                    if self.tray.available:
                        msg = (
                            f"Sigue activo en la bandeja del sistema ({self.selected_ip}:{self.udp_port}). "
                            "Clic en el icono para reabrir la ventana."
                        )
                    else:
                        msg = (
                            f"Micrófono activo en segundo plano ({self.selected_ip}:{self.udp_port}). "
                            "Ejecuta 'bindcurse' para reabrir la ventana."
                        )
                    print(f"[BindCurse] {msg}")
                    _notify_user("Activo en segundo plano", msg)

                # Ultra-low power sleep (0% GPU/CPU) until woken up via IPC/tray
                self._wake_event.wait()
                self._wake_event.clear()
                if not self._quit_requested:
                    show_window = True
        finally:
            self.stop_services()
