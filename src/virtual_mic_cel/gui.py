from __future__ import annotations

import gc
import json
import math
import os
import shutil
import socket
import subprocess
import threading
import time
from collections import deque

import dearpygui.dearpygui as dpg

from virtual_mic_cel.audio import VirtualMicEngine
from virtual_mic_cel.network import (
    DEFAULT_UDP_PORT,
    DeviceInfo,
    UDPAudioReceiver,
    get_local_ips,
)
from virtual_mic_cel.tray import TrayCallbacks, TrayIcon, TrayState, create_tray


def _find_system_font(bold: bool = False) -> str | None:
    """Finds a readable system TTF font supporting Latin-1 / Spanish characters."""
    try:
        pattern = "sans-serif:bold" if bold else "sans-serif"
        out = subprocess.check_output(
            ["fc-match", "-f", "%{file}\n", pattern],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ).strip()
        if out and os.path.isfile(out):
            return out
    except Exception:
        pass

    candidates = (
        [
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/noto/NotoSans-Bold.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/noto/NotoSans-Regular.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024.0:.1f} KB"
    return f"{num_bytes / (1024.0 * 1024.0):.2f} MB"


def _notify_user(title: str, body: str) -> None:
    """Sends a non-blocking desktop notification if notify-send is available."""
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(
                ["notify-send", "-a", "Virtual Mic Cel", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def send_ipc_command(control_port: int, command: str) -> dict[str, object] | None:
    """
    Sends a control command ('SHOW', 'HIDE', 'STOP', 'STATUS', 'TOGGLE_MONITOR', 'TOGGLE_MUTE')
    to an already running VirtualMicApp instance on 127.0.0.1:control_port.
    Returns parsed JSON response if an instance is running, or None if not running.
    """
    try:
        with socket.create_connection(("127.0.0.1", control_port), timeout=0.6) as sock:
            sock.sendall((command.strip().upper() + "\n").encode("utf-8"))
            raw = sock.recv(8192).decode("utf-8", errors="replace").strip()
            if raw:
                return json.loads(raw)
            return {"ok": True}
    except OSError:
        return None
    except Exception:
        return {"ok": True}


class VirtualMicApp:
    def __init__(self, udp_port: int = DEFAULT_UDP_PORT) -> None:
        self.udp_port = udp_port
        self.control_port = udp_port + 1
        self.audio = VirtualMicEngine()
        self.receiver = UDPAudioReceiver(
            port=self.udp_port,
            on_audio_chunk=self.audio.process_audio_chunk,
        )
        self.local_ips: list[tuple[str, str]] = get_local_ips()
        self.selected_ip: str = self.local_ips[0][1] if self.local_ips else "127.0.0.1"
        self.level_history: deque[float] = deque([0.0] * 120, maxlen=120)
        self._large_font: int | str | None = None
        self._known_table_ips: list[str] = []

        # Background / lifecycle state
        self.keep_in_background: bool = True
        self.gui_active: bool = False
        self._quit_requested: bool = False
        self._hide_requested: bool = False
        self._wake_event = threading.Event()
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
        self.receiver.start()
        self._start_ipc_server()
        self.tray.start()

    def stop_services(self) -> None:
        self._quit_requested = True
        self._wake_event.set()
        self.tray.stop()
        if self._ipc_sock is not None:
            try:
                self._ipc_sock.close()
            except OSError:
                pass
            self._ipc_sock = None
        self.receiver.stop()
        self.audio.shutdown()

    def _start_ipc_server(self) -> None:
        if self._ipc_sock is not None:
            return
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", self.control_port))
            srv.listen(4)
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
                    cmd = (
                        conn.recv(256).decode("utf-8", errors="replace").strip().upper()
                    )
                    resp = self._handle_ipc_command(cmd)
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                except Exception:
                    pass

    def _handle_ipc_command(self, cmd: str) -> dict[str, object]:
        if cmd == "SHOW":
            self._hide_requested = False
            self._wake_event.set()
            return {"ok": True, "action": "show", "gui_active": True}
        if cmd == "HIDE":
            self._hide_requested = True
            return {"ok": True, "action": "hide"}
        if cmd in ("STOP", "QUIT"):
            self._quit_requested = True
            self._hide_requested = True
            self._wake_event.set()
            return {"ok": True, "action": "stop"}
        if cmd == "TOGGLE_MONITOR":
            self.audio.set_monitor_enabled(not self.audio.monitor_enabled)
            return {"ok": True, "monitor_enabled": self.audio.monitor_enabled}
        if cmd == "TOGGLE_MUTE":
            self.audio.mic_muted = not self.audio.mic_muted
            return {"ok": True, "mic_muted": self.audio.mic_muted}
        # Default: STATUS
        devices = self.receiver.get_devices_snapshot()
        return {
            "ok": True,
            "ip": self.selected_ip,
            "udp_port": self.udp_port,
            "gui_active": self.gui_active,
            "virtual_mic_ready": self.audio.virtual_mic_ready,
            "device_label": self.audio.device_label,
            "monitor_enabled": self.audio.monitor_enabled,
            "mic_muted": self.audio.mic_muted,
            "total_packets": self.receiver.total_packets,
            "total_bytes": self.receiver.total_bytes,
            "devices": [
                {
                    "address": d.address_str,
                    "active": d.is_active,
                    "packets": d.packets,
                    "bytes": d.bytes_received,
                }
                for d in devices
            ],
        }

    def _refresh_ips_callback(self) -> None:
        self.local_ips = get_local_ips()
        items = [f"{ip}  [{label}]" for label, ip in self.local_ips]
        self.selected_ip = self.local_ips[0][1] if self.local_ips else "127.0.0.1"
        if dpg.does_item_exist("ip_combo"):
            dpg.configure_item(
                "ip_combo", items=items, default_value=items[0] if items else ""
            )
        self._update_connection_banner()

    def _on_ip_combo_selected(self, _sender: int, app_data: str) -> None:
        if app_data:
            ip = app_data.split()[0].strip()
            self.selected_ip = ip
            self._update_connection_banner()

    def _update_connection_banner(self) -> None:
        if dpg.does_item_exist("big_ip_port_text"):
            dpg.set_value(
                "big_ip_port_text",
                f"IP: {self.selected_ip}    |    Puerto UDP: {self.udp_port}",
            )
        if dpg.does_item_exist("copy_status_text"):
            dpg.set_value("copy_status_text", "")

    def _copy_address_callback(self) -> None:
        target = f"{self.selected_ip}:{self.udp_port}"
        dpg.set_clipboard_text(target)
        if dpg.does_item_exist("copy_status_text"):
            dpg.set_value("copy_status_text", f"Copiado: {target}")

    def _on_monitor_toggled(self, _sender: int, app_data: bool) -> None:
        self.audio.set_monitor_enabled(bool(app_data))

    def _on_mute_toggled(self, _sender: int, app_data: bool) -> None:
        self.audio.mic_muted = bool(app_data)

    def _on_bg_toggled(self, _sender: int, app_data: bool) -> None:
        self.keep_in_background = bool(app_data)

    def _on_mic_gain_changed(self, _sender: int, app_data: float) -> None:
        self.audio.mic_gain = float(app_data) / 100.0

    def _on_monitor_vol_changed(self, _sender: int, app_data: float) -> None:
        self.audio.monitor_volume = float(app_data) / 100.0

    def _on_recreate_mic(self) -> None:
        self.audio.setup_virtual_mic()
        self._update_mic_status_ui()

    def _on_test_tone(self) -> None:
        self.audio.send_test_tone_udp(port=self.udp_port, duration_sec=1.5)

    def _on_hide_to_background(self) -> None:
        self.keep_in_background = True
        self._hide_requested = True
        dpg.stop_dearpygui()

    def _on_quit_completely(self) -> None:
        self._quit_requested = True
        dpg.stop_dearpygui()

    def _on_clear_inactive(self) -> None:
        self.receiver.clear_inactive_devices()
        self._rebuild_devices_table(self.receiver.get_devices_snapshot())

    def _on_toggle_block_device(
        self, _sender: int, _app_data: object, user_data: str
    ) -> None:
        self.receiver.toggle_block_ip(user_data)
        self._rebuild_devices_table(self.receiver.get_devices_snapshot())

    def _update_mic_status_ui(self) -> None:
        if not dpg.does_item_exist("mic_status_text"):
            return
        if self.audio.virtual_mic_ready:
            dpg.set_value("mic_status_text", f"ACTIVO — {self.audio.device_label}")
            dpg.configure_item("mic_status_text", color=(80, 230, 130, 255))
        else:
            dpg.set_value("mic_status_text", f"ERROR — {self.audio.status_message}")
            dpg.configure_item("mic_status_text", color=(255, 95, 95, 255))

        if dpg.does_item_exist("backend_text"):
            dpg.set_value("backend_text", f"Backend: {self.audio.backend_name}")

    def _setup_theme_and_fonts(self) -> None:
        reg_font_path = _find_system_font(bold=False)
        bold_font_path = _find_system_font(bold=True) or reg_font_path
        self._large_font = None

        if reg_font_path:
            with dpg.font_registry():
                default_font = dpg.add_font(reg_font_path, 16)
                dpg.bind_font(default_font)

                if bold_font_path:
                    self._large_font = dpg.add_font(bold_font_path, 25)

        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 14, 14)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 6)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 10, 8)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 8)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 5)
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (18, 20, 26, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (26, 30, 40, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (38, 43, 56, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (50, 58, 75, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Button, (45, 95, 190, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (65, 118, 220, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (35, 78, 160, 255))
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, (80, 220, 140, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, (70, 140, 240, 255))
                dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, (70, 210, 135, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, (34, 40, 54, 255))
        dpg.bind_theme(global_theme)

    def _build_ui(self) -> None:
        ip_items = [f"{ip}  [{label}]" for label, ip in self.local_ips]
        selected_combo = next(
            (item for item in ip_items if item.startswith(self.selected_ip + " ")),
            ip_items[0] if ip_items else "",
        )

        with dpg.theme() as danger_btn_theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (175, 45, 55, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (205, 60, 70, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (145, 35, 45, 255))

        with dpg.window(tag="primary_window"):
            # --- 1. CONNECTION CARD FOR MOBILE DEVICE ---
            with dpg.child_window(height=155, border=True):
                with dpg.group(horizontal=True):
                    dpg.add_text(
                        "CONEXIÓN PARA DISPOSITIVO MÓVIL (TRANSMISOR UDP)",
                        color=(140, 185, 255, 255),
                    )
                    dpg.add_spacer(width=30)
                    dpg.add_button(
                        label="Minimizar a 2do Plano",
                        callback=lambda: self._on_hide_to_background(),
                    )
                    quit_btn = dpg.add_button(
                        label="Apagar y Salir",
                        callback=lambda: self._on_quit_completely(),
                    )
                    dpg.bind_item_theme(quit_btn, danger_btn_theme)

                big_txt = dpg.add_text(
                    f"IP: {self.selected_ip}    |    Puerto UDP: {self.udp_port}",
                    tag="big_ip_port_text",
                    color=(255, 235, 120, 255),
                )
                if self._large_font is not None:
                    dpg.bind_item_font(big_txt, self._large_font)

                dpg.add_text(
                    "Formato requerido: PCM 16-bit (s16le)  •  48000 Hz  •  Mono (1 canal)  •  Socket UDP",
                    color=(180, 190, 205, 255),
                )

                with dpg.group(horizontal=True):
                    dpg.add_text("Interfaz de red:")
                    dpg.add_combo(
                        items=ip_items,
                        default_value=selected_combo,
                        width=270,
                        tag="ip_combo",
                        callback=self._on_ip_combo_selected,
                    )
                    dpg.add_button(
                        label="Copiar IP:Puerto",
                        callback=lambda: self._copy_address_callback(),
                    )
                    dpg.add_button(
                        label="Actualizar IPs",
                        callback=lambda: self._refresh_ips_callback(),
                    )
                    dpg.add_text("", tag="copy_status_text", color=(100, 230, 150, 255))

            # --- 2. VIRTUAL MICROPHONE & SYSTEM MONITOR CONTROLS ---
            with dpg.child_window(height=255, border=True):
                with dpg.group(horizontal=True):
                    dpg.add_text(
                        "MICRÓFONO VIRTUAL Y MONITOREO DE AUDIO",
                        color=(140, 185, 255, 255),
                    )
                    dpg.add_spacer(width=20)
                    dpg.add_text("", tag="backend_text", color=(170, 175, 190, 255))

                with dpg.group(horizontal=True):
                    dpg.add_text("Estado del Micrófono:")
                    dpg.add_text("Iniciando...", tag="mic_status_text")

                dpg.add_separator()

                with dpg.group(horizontal=True):
                    dpg.add_checkbox(
                        label="Escuchar micrófono por el output de audio del sistema (Monitor)",
                        default_value=self.audio.monitor_enabled,
                        tag="monitor_checkbox",
                        callback=self._on_monitor_toggled,
                    )
                    dpg.add_spacer(width=20)
                    dpg.add_checkbox(
                        label="Silenciar micrófono virtual (Mute)",
                        default_value=self.audio.mic_muted,
                        tag="mute_checkbox",
                        callback=self._on_mute_toggled,
                    )

                dpg.add_checkbox(
                    label="Mantener activo en segundo plano al cerrar la ventana (ahorro de GPU/CPU)",
                    default_value=self.keep_in_background,
                    tag="bg_checkbox",
                    callback=self._on_bg_toggled,
                )

                with dpg.group(horizontal=True):
                    dpg.add_slider_float(
                        label="Ganancia Mic (%)",
                        default_value=self.audio.mic_gain * 100.0,
                        min_value=0.0,
                        max_value=250.0,
                        format="%.0f%%",
                        width=220,
                        callback=self._on_mic_gain_changed,
                    )
                    dpg.add_spacer(width=20)
                    dpg.add_slider_float(
                        label="Volumen Monitor (%)",
                        default_value=self.audio.monitor_volume * 100.0,
                        min_value=0.0,
                        max_value=200.0,
                        format="%.0f%%",
                        width=220,
                        callback=self._on_monitor_vol_changed,
                    )

                with dpg.group(horizontal=True):
                    dpg.add_text("Nivel de Entrada:")
                    dpg.add_progress_bar(
                        default_value=0.0,
                        width=320,
                        overlay="-inf dB",
                        tag="vu_meter_bar",
                    )
                    dpg.add_simple_plot(
                        default_value=list(self.level_history),
                        width=180,
                        height=24,
                        min_scale=0.0,
                        max_scale=1.0,
                        tag="vu_history_plot",
                    )
                    dpg.add_button(
                        label="Probar Tono UDP",
                        callback=lambda: self._on_test_tone(),
                    )
                    dpg.add_button(
                        label="Reiniciar Mic",
                        callback=lambda: self._on_recreate_mic(),
                    )

            # --- 3. CONNECTED DEVICES TABLE ---
            with dpg.child_window(border=True):
                with dpg.group(horizontal=True):
                    dpg.add_text(
                        "DISPOSITIVOS CONECTADOS (CLIENTES UDP)",
                        color=(140, 185, 255, 255),
                    )
                    dpg.add_spacer(width=20)
                    dpg.add_text(
                        "Total: 0 paquetes (0 B)",
                        tag="total_traffic_text",
                        color=(180, 190, 205, 255),
                    )
                    dpg.add_spacer(width=20)
                    dpg.add_button(
                        label="Limpiar Inactivos",
                        callback=lambda: self._on_clear_inactive(),
                    )

                with dpg.table(
                    tag="devices_table",
                    header_row=True,
                    borders_innerH=True,
                    borders_outerH=True,
                    borders_innerV=True,
                    borders_outerV=True,
                    row_background=True,
                    resizable=True,
                ):
                    dpg.add_table_column(
                        label="Dispositivo (IP:Puerto)",
                        width_stretch=True,
                        init_width_or_weight=1.4,
                    )
                    dpg.add_table_column(
                        label="Estado", width_stretch=True, init_width_or_weight=1.0
                    )
                    dpg.add_table_column(
                        label="Nivel Audio",
                        width_stretch=True,
                        init_width_or_weight=1.1,
                    )
                    dpg.add_table_column(
                        label="Velocidad", width_stretch=True, init_width_or_weight=1.2
                    )
                    dpg.add_table_column(
                        label="Datos Recibidos",
                        width_stretch=True,
                        init_width_or_weight=1.3,
                    )
                    dpg.add_table_column(
                        label="Última Señal",
                        width_stretch=True,
                        init_width_or_weight=0.9,
                    )
                    dpg.add_table_column(
                        label="Acción", width_fixed=True, init_width_or_weight=95
                    )

        self._update_mic_status_ui()
        self._rebuild_devices_table(self.receiver.get_devices_snapshot())

    def _rebuild_devices_table(self, devices: list[DeviceInfo]) -> None:
        if not dpg.does_item_exist("devices_table"):
            return

        children = dpg.get_item_children("devices_table", 1) or []
        for child in children:
            dpg.delete_item(child)

        self._known_table_ips = [d.ip for d in devices]

        if not devices:
            with dpg.table_row(parent="devices_table"):
                dpg.add_text(
                    f"Esperando paquetes UDP en puerto {self.udp_port}...",
                    color=(140, 145, 160, 255),
                )
                dpg.add_text("-")
                dpg.add_text("-")
                dpg.add_text("-")
                dpg.add_text("-")
                dpg.add_text("-")
                dpg.add_text("-")
            return

        now = time.time()
        for dev in devices:
            ip_tag = dev.ip.replace(".", "_")
            with dpg.table_row(parent="devices_table"):
                dpg.add_text(dev.address_str, tag=f"dev_addr_{ip_tag}")
                status_str, status_color = self._device_status_label(dev)
                dpg.add_text(status_str, tag=f"dev_status_{ip_tag}", color=status_color)
                dpg.add_progress_bar(
                    default_value=min(1.0, dev.peak_level),
                    width=-1,
                    overlay=f"{int(dev.peak_level * 100)}%",
                    tag=f"dev_level_{ip_tag}",
                )
                dpg.add_text(
                    f"{dev.kbps:.0f} kbps ({dev.pps:.0f} pkt/s)",
                    tag=f"dev_rate_{ip_tag}",
                )
                dpg.add_text(
                    f"{_format_bytes(dev.bytes_received)} ({dev.packets} pkts)",
                    tag=f"dev_bytes_{ip_tag}",
                )
                ago = max(0.0, now - dev.last_seen)
                dpg.add_text(f"hace {ago:.1f}s", tag=f"dev_ago_{ip_tag}")
                btn_label = "Permitir" if dev.blocked else "Bloquear"
                dpg.add_button(
                    label=btn_label,
                    tag=f"dev_btn_{ip_tag}",
                    user_data=dev.ip,
                    callback=self._on_toggle_block_device,
                )

    @staticmethod
    def _device_status_label(dev: DeviceInfo) -> tuple[str, tuple[int, int, int, int]]:
        if dev.blocked:
            return "BLOQUEADO", (255, 95, 95, 255)
        if dev.is_active:
            return "TRANSMITIENDO", (80, 235, 135, 255)
        return "INACTIVO", (160, 165, 175, 255)

    def _update_frame(self) -> None:
        devices = self.receiver.get_devices_snapshot()

        # Keep checkboxes in sync with changes made from the tray icon / CLI
        if (
            dpg.does_item_exist("monitor_checkbox")
            and dpg.get_value("monitor_checkbox") != self.audio.monitor_enabled
        ):
            dpg.set_value("monitor_checkbox", self.audio.monitor_enabled)
        if (
            dpg.does_item_exist("mute_checkbox")
            and dpg.get_value("mute_checkbox") != self.audio.mic_muted
        ):
            dpg.set_value("mute_checkbox", self.audio.mic_muted)

        peak = self.receiver.current_peak
        rms = self.receiver.current_rms
        display_level = min(1.0, max(peak, rms * 1.4))
        self.level_history.append(display_level)

        if dpg.does_item_exist("vu_meter_bar"):
            if rms > 1e-4:
                db = 20.0 * math.log10(rms)
                overlay = f"{db:.1f} dBFS ({int(display_level * 100)}%)"
            else:
                overlay = "-inf dBFS (0%)"
            dpg.set_value("vu_meter_bar", display_level)
            dpg.configure_item("vu_meter_bar", overlay=overlay)

        if dpg.does_item_exist("vu_history_plot"):
            dpg.set_value("vu_history_plot", list(self.level_history))

        if dpg.does_item_exist("total_traffic_text"):
            dpg.set_value(
                "total_traffic_text",
                f"Total: {self.receiver.total_packets} paquetes ({_format_bytes(self.receiver.total_bytes)})",
            )

        current_ips = [d.ip for d in devices]
        if current_ips != self._known_table_ips:
            self._rebuild_devices_table(devices)
        else:
            now = time.time()
            for dev in devices:
                ip_tag = dev.ip.replace(".", "_")
                if dpg.does_item_exist(f"dev_addr_{ip_tag}"):
                    dpg.set_value(f"dev_addr_{ip_tag}", dev.address_str)
                if dpg.does_item_exist(f"dev_status_{ip_tag}"):
                    status_str, status_color = self._device_status_label(dev)
                    dpg.set_value(f"dev_status_{ip_tag}", status_str)
                    dpg.configure_item(f"dev_status_{ip_tag}", color=status_color)
                if dpg.does_item_exist(f"dev_level_{ip_tag}"):
                    lvl = min(1.0, dev.peak_level)
                    dpg.set_value(f"dev_level_{ip_tag}", lvl)
                    dpg.configure_item(
                        f"dev_level_{ip_tag}", overlay=f"{int(lvl * 100)}%"
                    )
                if dpg.does_item_exist(f"dev_rate_{ip_tag}"):
                    dpg.set_value(
                        f"dev_rate_{ip_tag}",
                        f"{dev.kbps:.0f} kbps ({dev.pps:.0f} pkt/s)",
                    )
                if dpg.does_item_exist(f"dev_bytes_{ip_tag}"):
                    dpg.set_value(
                        f"dev_bytes_{ip_tag}",
                        f"{_format_bytes(dev.bytes_received)} ({dev.packets} pkts)",
                    )
                if dpg.does_item_exist(f"dev_ago_{ip_tag}"):
                    ago = max(0.0, now - dev.last_seen)
                    dpg.set_value(f"dev_ago_{ip_tag}", f"hace {ago:.1f}s")

    def _run_gui_session(self) -> None:
        """
        Opens the Dear PyGui window and runs the render loop until the user closes it.
        When closed, completely destroys the OpenGL/DearPyGui context to free GPU/CPU.
        """
        self.gui_active = True
        self._hide_requested = False
        self.receiver.compute_levels = True

        dpg.create_context()
        try:
            self._setup_theme_and_fonts()
            self._build_ui()

            dpg.create_viewport(
                title="Virtual Mic Cel — Receptor de Micrófono UDP (48kHz 16-bit Mono)",
                width=940,
                height=675,
                min_width=780,
                min_height=540,
            )
            dpg.setup_dearpygui()
            dpg.show_viewport()
            dpg.set_primary_window("primary_window", True)

            last_ui_update = 0.0
            while (
                dpg.is_dearpygui_running()
                and not self._quit_requested
                and not self._hide_requested
            ):
                now = time.time()
                if now - last_ui_update >= 0.05:
                    self._update_frame()
                    last_ui_update = now
                dpg.render_dearpygui_frame()
        finally:
            dpg.destroy_context()
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
                    f"[Virtual Mic Cel] Activo en 2do plano en {self.selected_ip}:{self.udp_port}. "
                    "Ejecuta 'uv run virtual-mic-cel' para abrir la interfaz o '--stop' para apagarlo."
                )

            while not self._quit_requested:
                if show_window:
                    self._run_gui_session()
                    # Discard SHOW requests that arrived while the window was already open
                    self._wake_event.clear()

                    if self._quit_requested or not self.keep_in_background:
                        break

                    if self.tray.available:
                        msg = (
                            f"Sigue activo en la bandeja del sistema ({self.selected_ip}:{self.udp_port}). "
                            "Clic en el icono para reabrir la ventana."
                        )
                    else:
                        msg = (
                            f"Micrófono activo en segundo plano ({self.selected_ip}:{self.udp_port}). "
                            "Ejecuta 'virtual-mic-cel' para reabrir la ventana."
                        )
                    print(f"[Virtual Mic Cel] {msg}")
                    _notify_user("Activo en segundo plano", msg)

                # Sleep in ultra-low-power mode (0% GPU, 0% GUI CPU) until woken via IPC or signal
                self._wake_event.wait()
                if not self._quit_requested:
                    show_window = True
        finally:
            self.stop_services()
