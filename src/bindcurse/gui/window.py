from __future__ import annotations

import math
import os
import signal
import sys
import threading
import time
from collections import deque
from typing import Any

import dearpygui.dearpygui as dpg

from bindcurse.core import get_orchestrator
from bindcurse.gui.helpers import format_bytes, terminate_dpg_glfw
from bindcurse.gui.theme import create_danger_button_theme, setup_theme_and_fonts
from bindcurse.services.network.ipc import send_ipc_command

_COALESCED_COMMANDS = {"SET_GAIN", "SET_MONITOR_VOL"}


class GuiSessionWindow:
    """
    Runs the Dear PyGui viewport inside an isolated child process.
    Communicates with the background VirtualMicApp daemon over local IPC.
    When the window closes, the child process exits so the OS kernel reclaims 100%
    of the OpenGL/DirectX, GLFW/Win32, and DearPyGui heap allocations.
    """

    def __init__(self, control_port: int) -> None:
        self.control_port = control_port
        self.level_history: deque[float] = deque([0.0] * 120, maxlen=120)
        self._large_font: int | str | None = None
        self._known_table_ips: list[str] = []
        self._closing: bool = False
        self._consecutive_ipc_failures: int = 0
        self._state_lock = threading.Lock()
        self._last_user_mutation: float = 0.0

        # Coalescing async command queue (avoids spawning hundreds of threads/sockets on slider drag)
        self._cmd_lock = threading.Lock()
        self._cmd_event = threading.Event()
        self._pending_coalesced: dict[str, str] = {}
        self._pending_ordered: deque[str] = deque()

        # Initial snapshot from daemon
        state = send_ipc_command(self.control_port, "STATUS") or {}
        self._latest_state: dict[str, Any] = dict(state)
        self.udp_port: int = int(state.get("udp_port", self.control_port - 1))
        self.selected_ip: str = str(state.get("ip", "127.0.0.1"))
        raw_ips = state.get("local_ips")
        self.local_ips: list[tuple[str, str]] = (
            [(str(item[0]), str(item[1])) for item in raw_ips if len(item) >= 2]
            if isinstance(raw_ips, list) and raw_ips
            else [("Principal", self.selected_ip)]
        )
        self.keep_in_background: bool = bool(state.get("keep_in_background", True))
        self.monitor_enabled: bool = bool(state.get("monitor_enabled", False))
        self.mic_muted: bool = bool(state.get("mic_muted", False))
        self.mic_gain: float = float(state.get("mic_gain", 1.0))
        self.monitor_volume: float = float(state.get("monitor_volume", 1.0))
        self._initial_state: dict[str, Any] = state

    def _ipc(self, command: str) -> dict[str, Any]:
        return send_ipc_command(self.control_port, command) or {}

    def _send_async_ipc(self, command: str) -> None:
        prefix = command.split("|", 1)[0].strip().upper()
        with self._cmd_lock:
            if prefix in _COALESCED_COMMANDS:
                self._pending_coalesced[prefix] = command
            else:
                self._pending_ordered.append(command)
        self._cmd_event.set()

    def _cmd_worker(self) -> None:
        while not self._closing:
            self._cmd_event.wait(timeout=0.1)
            self._cmd_event.clear()
            while not self._closing:
                cmd: str | None = None
                with self._cmd_lock:
                    if self._pending_ordered:
                        cmd = self._pending_ordered.popleft()
                    elif self._pending_coalesced:
                        key = next(iter(self._pending_coalesced))
                        cmd = self._pending_coalesced.pop(key)
                if cmd is None:
                    break
                resp = send_ipc_command(self.control_port, cmd)
                if isinstance(resp, dict):
                    with self._state_lock:
                        for k in (
                            "monitor_enabled",
                            "mic_muted",
                            "keep_in_background",
                            "mic_gain",
                            "monitor_volume",
                            "ip",
                        ):
                            if k in resp:
                                self._latest_state[k] = resp[k]

    def _poll_worker(self) -> None:
        while not self._closing:
            poll_started = time.monotonic()
            state = send_ipc_command(self.control_port, "STATUS")
            if state is not None:
                with self._state_lock:
                    # If the user mutated a toggle while this STATUS poll was in flight
                    # (or within 350ms), preserve the user's optimistic toggle values.
                    if poll_started < self._last_user_mutation + 0.35:
                        for key in (
                            "monitor_enabled",
                            "mic_muted",
                            "keep_in_background",
                            "mic_gain",
                            "monitor_volume",
                            "ip",
                        ):
                            if key in self._latest_state:
                                state[key] = self._latest_state[key]
                    self._latest_state = state
                    self._consecutive_ipc_failures = 0
                if state.get("should_close"):
                    self._closing = True
                    try:
                        dpg.stop_dearpygui()
                    except Exception:
                        pass
                    break
            else:
                self._consecutive_ipc_failures += 1
                if self._consecutive_ipc_failures > 50:
                    self._closing = True
                    try:
                        dpg.stop_dearpygui()
                    except Exception:
                        pass
                    break
            time.sleep(0.08)

    def _refresh_ips_callback(self) -> None:
        resp = self._ipc("REFRESH_IPS")
        raw_ips = resp.get("local_ips")
        if isinstance(raw_ips, list) and raw_ips:
            self.local_ips = [
                (str(item[0]), str(item[1])) for item in raw_ips if len(item) >= 2
            ]
        self.selected_ip = str(
            resp.get("ip", self.local_ips[0][1] if self.local_ips else "127.0.0.1")
        )
        items = [f"{ip}  [{label}]" for label, ip in self.local_ips]
        if dpg.does_item_exist("ip_combo"):
            dpg.configure_item(
                "ip_combo", items=items, default_value=items[0] if items else ""
            )
        self._update_connection_banner()

    def _on_ip_combo_selected(self, _sender: int, app_data: str) -> None:
        if app_data:
            ip = app_data.split()[0].strip()
            self.selected_ip = ip
            with self._state_lock:
                self._last_user_mutation = time.monotonic()
                self._latest_state["ip"] = ip
            self._send_async_ipc(f"SET_IP|{ip}")
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
        self.monitor_enabled = bool(app_data)
        with self._state_lock:
            self._last_user_mutation = time.monotonic()
            self._latest_state["monitor_enabled"] = self.monitor_enabled
        self._send_async_ipc(f"SET_MONITOR|{1 if self.monitor_enabled else 0}")

    def _on_mute_toggled(self, _sender: int, app_data: bool) -> None:
        self.mic_muted = bool(app_data)
        with self._state_lock:
            self._last_user_mutation = time.monotonic()
            self._latest_state["mic_muted"] = self.mic_muted
        self._send_async_ipc(f"SET_MUTE|{1 if self.mic_muted else 0}")

    def _on_bg_toggled(self, _sender: int, app_data: bool) -> None:
        self.keep_in_background = bool(app_data)
        with self._state_lock:
            self._last_user_mutation = time.monotonic()
            self._latest_state["keep_in_background"] = self.keep_in_background
        self._send_async_ipc(f"SET_KEEP_BG|{1 if self.keep_in_background else 0}")

    def _on_mic_gain_changed(self, _sender: int, app_data: float) -> None:
        val = float(app_data)
        self.mic_gain = val / 100.0
        with self._state_lock:
            self._last_user_mutation = time.monotonic()
            self._latest_state["mic_gain"] = self.mic_gain
        self._send_async_ipc(f"SET_GAIN|{val}")

    def _on_monitor_vol_changed(self, _sender: int, app_data: float) -> None:
        val = float(app_data)
        self.monitor_volume = val / 100.0
        with self._state_lock:
            self._last_user_mutation = time.monotonic()
            self._latest_state["monitor_volume"] = self.monitor_volume
        self._send_async_ipc(f"SET_MONITOR_VOL|{val}")

    def _on_recreate_mic(self) -> None:
        resp = self._ipc("RECREATE_MIC")
        if resp:
            with self._state_lock:
                self._last_user_mutation = time.monotonic()
                self._latest_state.update(resp)
            self._update_mic_status_ui(resp)

    def _on_test_tone(self) -> None:
        self._send_async_ipc("TEST_TONE")

    def _on_hide_to_background(self) -> None:
        self._closing = True
        self._cmd_event.set()
        self._ipc("HIDE_BG")
        dpg.stop_dearpygui()

    def _on_quit_completely(self) -> None:
        self._closing = True
        self._cmd_event.set()
        self._ipc("STOP")
        dpg.stop_dearpygui()

    def _on_clear_inactive(self) -> None:
        resp = self._ipc("CLEAR_INACTIVE")
        devices = resp.get("devices")
        if isinstance(devices, list):
            with self._state_lock:
                self._latest_state["devices"] = devices
            self._rebuild_devices_table(devices)

    def _on_toggle_block_device(
        self, _sender: int, _app_data: object, user_data: str
    ) -> None:
        resp = self._ipc(f"TOGGLE_BLOCK|{user_data}")
        devices = resp.get("devices")
        if isinstance(devices, list):
            with self._state_lock:
                self._latest_state["devices"] = devices
            self._rebuild_devices_table(devices)

    def _update_mic_status_ui(self, state: dict[str, Any]) -> None:
        if not dpg.does_item_exist("mic_status_text"):
            return
        if state.get("virtual_mic_ready"):
            dpg.set_value(
                "mic_status_text", f"ACTIVO — {state.get('device_label', '-')}"
            )
            dpg.configure_item("mic_status_text", color=(80, 230, 130, 255))
        else:
            dpg.set_value(
                "mic_status_text",
                f"ERROR — {state.get('status_message', 'Iniciando...')}",
            )
            dpg.configure_item("mic_status_text", color=(255, 95, 95, 255))

        if dpg.does_item_exist("backend_text"):
            dpg.set_value(
                "backend_text",
                f"Backend: {state.get('backend_name', 'No inicializado')}",
            )

    @staticmethod
    def _device_status_label(
        dev: dict[str, Any],
    ) -> tuple[str, tuple[int, int, int, int]]:
        if dev.get("blocked"):
            return "BLOQUEADO", (255, 95, 95, 255)
        if dev.get("state") == "DESCONECTADO":
            return "DESCONECTADO", (160, 165, 175, 255)
        if dev.get("active"):
            if int(dev.get("packets", 0)) > 0:
                return "TRANSMITIENDO", (80, 235, 135, 255)
            return "ENLAZADO", (100, 200, 255, 255)
        return "INACTIVO", (160, 165, 175, 255)

    def _rebuild_devices_table(self, devices: list[dict[str, Any]]) -> None:
        if not dpg.does_item_exist("devices_table"):
            return

        children = dpg.get_item_children("devices_table", 1) or []
        for child in children:
            dpg.delete_item(child)

        self._known_table_ips = [str(d.get("ip", "")) for d in devices]

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
            ip = str(dev.get("ip", ""))
            ip_tag = ip.replace(".", "_")
            display_name = str(dev.get("display_name") or dev.get("address") or ip)
            peak_level = float(dev.get("peak_level", 0.0))
            kbps = float(dev.get("kbps", 0.0))
            pps = float(dev.get("pps", 0.0))
            bytes_received = int(dev.get("bytes", 0))
            packets = int(dev.get("packets", 0))
            lost_packets = int(dev.get("lost", 0))
            last_seen = float(dev.get("last_seen", now))
            blocked = bool(dev.get("blocked", False))

            with dpg.table_row(parent="devices_table"):
                dpg.add_text(display_name, tag=f"dev_addr_{ip_tag}")
                status_str, status_color = self._device_status_label(dev)
                dpg.add_text(status_str, tag=f"dev_status_{ip_tag}", color=status_color)
                dpg.add_progress_bar(
                    default_value=min(1.0, peak_level),
                    width=-1,
                    overlay=f"{int(peak_level * 100)}%",
                    tag=f"dev_level_{ip_tag}",
                )
                dpg.add_text(
                    f"{kbps:.0f} kbps ({pps:.0f} pkt/s)",
                    tag=f"dev_rate_{ip_tag}",
                )
                loss_str = f", {lost_packets} perd" if lost_packets > 0 else ""
                dpg.add_text(
                    f"{format_bytes(bytes_received)} ({packets} pkts{loss_str})",
                    tag=f"dev_bytes_{ip_tag}",
                )
                ago = max(0.0, now - last_seen)
                dpg.add_text(f"hace {ago:.1f}s", tag=f"dev_ago_{ip_tag}")
                btn_label = "Permitir" if blocked else "Bloquear"
                dpg.add_button(
                    label=btn_label,
                    tag=f"dev_btn_{ip_tag}",
                    user_data=ip,
                    callback=self._on_toggle_block_device,
                )

    def _build_ui(self) -> None:
        ip_items = [f"{ip}  [{label}]" for label, ip in self.local_ips]
        selected_combo = next(
            (item for item in ip_items if item.startswith(self.selected_ip + " ")),
            ip_items[0] if ip_items else "",
        )

        danger_btn_theme = create_danger_button_theme()

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
                        default_value=self.monitor_enabled,
                        tag="monitor_checkbox",
                        callback=self._on_monitor_toggled,
                    )
                    dpg.add_spacer(width=20)
                    dpg.add_checkbox(
                        label="Silenciar micrófono virtual (Mute)",
                        default_value=self.mic_muted,
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
                        default_value=self.mic_gain * 100.0,
                        min_value=0.0,
                        max_value=250.0,
                        format="%.0f%%",
                        width=220,
                        tag="mic_gain_slider",
                        callback=self._on_mic_gain_changed,
                    )
                    dpg.add_spacer(width=20)
                    dpg.add_slider_float(
                        label="Volumen Monitor (%)",
                        default_value=self.monitor_volume * 100.0,
                        min_value=0.0,
                        max_value=200.0,
                        format="%.0f%%",
                        width=220,
                        tag="monitor_vol_slider",
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

        self._update_mic_status_ui(self._initial_state)
        initial_devices = self._initial_state.get("devices")
        self._rebuild_devices_table(
            initial_devices if isinstance(initial_devices, list) else []
        )

    def _update_frame(self) -> None:
        with self._state_lock:
            state = dict(self._latest_state)

        if not state:
            return

        if state.get("should_close"):
            self._closing = True
            dpg.stop_dearpygui()
            return

        self._update_mic_status_ui(state)

        mon_enabled = bool(state.get("monitor_enabled", False))
        mic_muted = bool(state.get("mic_muted", False))
        keep_bg = bool(state.get("keep_in_background", True))
        if (
            dpg.does_item_exist("monitor_checkbox")
            and dpg.get_value("monitor_checkbox") != mon_enabled
        ):
            dpg.set_value("monitor_checkbox", mon_enabled)
        if (
            dpg.does_item_exist("mute_checkbox")
            and dpg.get_value("mute_checkbox") != mic_muted
        ):
            dpg.set_value("mute_checkbox", mic_muted)
        if (
            dpg.does_item_exist("bg_checkbox")
            and dpg.get_value("bg_checkbox") != keep_bg
        ):
            dpg.set_value("bg_checkbox", keep_bg)

        peak = float(state.get("current_peak", 0.0))
        rms = float(state.get("current_rms", 0.0))
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
            total_packets = int(state.get("total_packets", 0))
            total_bytes = int(state.get("total_bytes", 0))
            dpg.set_value(
                "total_traffic_text",
                f"Total: {total_packets} paquetes ({format_bytes(total_bytes)})",
            )

        raw_devices = state.get("devices")
        devices: list[dict[str, Any]] = (
            raw_devices if isinstance(raw_devices, list) else []
        )
        current_ips = [str(d.get("ip", "")) for d in devices]
        if current_ips != self._known_table_ips:
            self._rebuild_devices_table(devices)
        else:
            now = time.time()
            for dev in devices:
                ip = str(dev.get("ip", ""))
                ip_tag = ip.replace(".", "_")
                display_name = str(dev.get("display_name") or dev.get("address") or ip)
                if dpg.does_item_exist(f"dev_addr_{ip_tag}"):
                    dpg.set_value(f"dev_addr_{ip_tag}", display_name)
                if dpg.does_item_exist(f"dev_status_{ip_tag}"):
                    status_str, status_color = self._device_status_label(dev)
                    dpg.set_value(f"dev_status_{ip_tag}", status_str)
                    dpg.configure_item(f"dev_status_{ip_tag}", color=status_color)
                if dpg.does_item_exist(f"dev_level_{ip_tag}"):
                    lvl = min(1.0, float(dev.get("peak_level", 0.0)))
                    dpg.set_value(f"dev_level_{ip_tag}", lvl)
                    dpg.configure_item(
                        f"dev_level_{ip_tag}", overlay=f"{int(lvl * 100)}%"
                    )
                if dpg.does_item_exist(f"dev_rate_{ip_tag}"):
                    kbps = float(dev.get("kbps", 0.0))
                    pps = float(dev.get("pps", 0.0))
                    dpg.set_value(
                        f"dev_rate_{ip_tag}",
                        f"{kbps:.0f} kbps ({pps:.0f} pkt/s)",
                    )
                if dpg.does_item_exist(f"dev_bytes_{ip_tag}"):
                    lost = int(dev.get("lost", 0))
                    packets = int(dev.get("packets", 0))
                    bytes_rx = int(dev.get("bytes", 0))
                    loss_str = f", {lost} perd" if lost > 0 else ""
                    dpg.set_value(
                        f"dev_bytes_{ip_tag}",
                        f"{format_bytes(bytes_rx)} ({packets} pkts{loss_str})",
                    )
                if dpg.does_item_exist(f"dev_ago_{ip_tag}"):
                    last_seen = float(dev.get("last_seen", now))
                    ago = max(0.0, now - last_seen)
                    dpg.set_value(f"dev_ago_{ip_tag}", f"hace {ago:.1f}s")

    def run(self) -> None:
        def _on_signal(_sig: int, _frame: object) -> None:
            self._closing = True
            self._cmd_event.set()
            try:
                dpg.stop_dearpygui()
            except Exception:
                pass

        try:
            signal.signal(signal.SIGTERM, _on_signal)
            signal.signal(signal.SIGINT, _on_signal)
        except (ValueError, OSError):
            pass

        dpg.create_context()
        try:
            self._large_font = setup_theme_and_fonts()
            self._build_ui()

            viewport_title = "BindCurse — Receptor de Micrófono UDP (48kHz 16-bit Mono)"
            dpg.create_viewport(
                title=viewport_title,
                width=940,
                height=675,
                min_width=780,
                min_height=540,
            )
            dpg.setup_dearpygui()
            dpg.show_viewport()
            get_orchestrator().setup_gui_viewport(viewport_title)
            dpg.set_primary_window("primary_window", True)

            cmd_thread = threading.Thread(
                target=self._cmd_worker, name="GuiCmdDispatcher", daemon=True
            )
            cmd_thread.start()

            poller = threading.Thread(
                target=self._poll_worker, name="GuiPoller", daemon=True
            )
            poller.start()

            last_ui_update = 0.0
            while dpg.is_dearpygui_running() and not self._closing:
                now = time.time()
                if now - last_ui_update >= 0.05:
                    self._update_frame()
                    last_ui_update = now
                dpg.render_dearpygui_frame()
        finally:
            self._closing = True
            self._cmd_event.set()
            try:
                dpg.destroy_context()
            except Exception:
                pass
            terminate_dpg_glfw()


def main() -> None:
    if len(sys.argv) < 2:
        os._exit(1)
    control_port = int(sys.argv[1])
    win = GuiSessionWindow(control_port=control_port)
    win.run()
    os._exit(0)


if __name__ == "__main__":
    main()
