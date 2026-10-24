from __future__ import annotations

import argparse
import os
import signal
from typing import Any

from bindcurse.core.config import APP_NAME, DEFAULT_UDP_PORT, UserSettings
from bindcurse.core import get_orchestrator
from bindcurse.services.network.ipc import send_ipc_command


def __getattr__(name: str) -> Any:
    if name == "VirtualMicApp":
        from bindcurse.gui.app import VirtualMicApp

        return VirtualMicApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _print_status(resp: dict[str, object]) -> None:
    print(f"Estado {APP_NAME} (UDP {resp.get('ip')}:{resp.get('udp_port')}):")
    print(
        f"  Micrófono Virtual : {'ACTIVO' if resp.get('virtual_mic_ready') else 'INACTIVO'} ({resp.get('device_label')})"
    )
    print(
        f"  Interfaz Gráfica  : {'Abierta' if resp.get('gui_active') else 'En 2do plano (0% GPU)'}"
    )
    print(
        f"  Monitor de Salida : {'Activado' if resp.get('monitor_enabled') else 'Desactivado'}"
    )
    print(f"  Silenciado (Mute) : {'Sí' if resp.get('mic_muted') else 'No'}")
    print(
        f"  Tráfico Total     : {resp.get('total_packets')} paquetes ({resp.get('total_bytes')} bytes)"
    )
    devices = resp.get("devices")
    if isinstance(devices, list) and devices:
        print("  Dispositivos:")
        for d in devices:
            if isinstance(d, dict):
                estado = d.get("state") or (
                    "TRANSMITIENDO" if d.get("active") else "INACTIVO"
                )
                name_str = (
                    f" [{d.get('name')}]"
                    if d.get("name") and d.get("name") not in ("Dispositivo", "Cliente")
                    else ""
                )
                lost_str = f", {d.get('lost')} perdidos" if d.get("lost") else ""
                print(
                    f"    - {d.get('address')}{name_str} [{estado}] ({d.get('packets')} pkts{lost_str})"
                )
    else:
        print("  Dispositivos      : Ninguno conectado aún")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bindcurse",
        description="BindCurse: Micrófono virtual UDP (PCM 16-bit, 48000Hz, mono) con modo segundo plano.",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=None,
        help=f"Puerto UDP de escucha (por defecto según configuración: {DEFAULT_UDP_PORT})",
    )
    parser.add_argument(
        "-b",
        "--background",
        action="store_true",
        help="Iniciar directamente en segundo plano sin abrir la ventana gráfica",
    )
    parser.add_argument(
        "-f",
        "--foreground",
        action="store_true",
        help="Mantener el proceso acoplado a la terminal actual",
    )
    parser.add_argument(
        "--hide",
        action="store_true",
        help="Ocultar la ventana de la instancia actual y dejarla en segundo plano",
    )
    parser.add_argument(
        "-q",
        "--stop",
        "--quit",
        dest="stop",
        action="store_true",
        help="Detener la instancia en ejecución y eliminar el micrófono virtual",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Mostrar el estado de la instancia en segundo plano",
    )
    parser.add_argument(
        "--toggle-monitor",
        action="store_true",
        help="Alternar escucha del micrófono en la salida del sistema",
    )
    parser.add_argument(
        "--toggle-mute",
        action="store_true",
        help="Alternar silencio (mute) del micrófono virtual",
    )
    args = parser.parse_args()

    settings = UserSettings.load()
    port = args.port if args.port is not None else settings.udp_port
    if args.port is not None and args.port != settings.udp_port:
        settings.udp_port = args.port
        settings.save()
    control_port = port + 1

    # Check if an instance is already running
    existing = send_ipc_command(control_port, "STATUS")

    if args.stop:
        if existing is None:
            print(f"{APP_NAME} no está en ejecución.")
            return
        send_ipc_command(control_port, "STOP")
        print(f"{APP_NAME} detenido y micrófono virtual descargado.")
        return

    if args.status:
        if existing is None:
            print(f"{APP_NAME} no está en ejecución.")
            return
        _print_status(existing)
        return

    if args.hide:
        if existing is None:
            print(f"{APP_NAME} no está en ejecución.")
            return
        send_ipc_command(control_port, "HIDE")
        print("Ventana ocultada en segundo plano.")
        return

    if args.toggle_monitor:
        if existing is None:
            print(f"{APP_NAME} no está en ejecución.")
            return
        resp = send_ipc_command(control_port, "TOGGLE_MONITOR") or {}
        state = "ACTIVADO" if resp.get("monitor_enabled") else "DESACTIVADO"
        print(f"Monitor de salida del sistema: {state}")
        return

    if args.toggle_mute:
        if existing is None:
            print(f"{APP_NAME} no está en ejecución.")
            return
        resp = send_ipc_command(control_port, "TOGGLE_MUTE") or {}
        state = "SILENCIADO" if resp.get("mic_muted") else "ACTIVO"
        print(f"Micrófono virtual: {state}")
        return

    # If already running, wake up and show the GUI window!
    if existing is not None:
        if args.background:
            print(
                f"{APP_NAME} ya está activo en segundo plano ({existing.get('ip')}:{existing.get('udp_port')})."
            )
            return
        send_ipc_command(control_port, "SHOW")
        print("Instancia en segundo plano detectada — abriendo ventana gráfica.")
        return

    # By default, spawn detached session so closing the terminal
    # or closing the GUI window leaves the background service running cleanly.
    if not args.foreground:
        get_orchestrator().spawn_detached_daemon(port=port, background=args.background)
        mode_msg = "en segundo plano" if args.background else "con ventana gráfica"
        print(
            f"{APP_NAME} iniciado ({mode_msg}, UDP :{port}).\n"
            "  • Si cierras la ventana, seguirá activo en 2do plano con mínimo uso de recursos.\n"
            "  • Ejecuta 'uv run bindcurse' cuando quieras volver a abrir la interfaz.\n"
            "  • Ejecuta 'uv run bindcurse --stop' (o el botón 'Apagar y Salir') para cerrarlo del todo."
        )
        return

    from bindcurse.gui.app import VirtualMicApp

    app = VirtualMicApp(udp_port=port)

    def _handle_sig(_sig: int, _frame: object) -> None:
        app.stop_services()
        os._exit(0)

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    app.run(start_hidden=args.background)


if __name__ == "__main__":
    main()
