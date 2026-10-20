#!/usr/bin/env python3
"""
Test del ciclo de vida completo de VirtualMicApp e IPC:
1. Inicia VirtualMicApp en modo segundo plano (start_hidden=True)
2. Envía comando SHOW vía IPC -> Se abre la ventana
3. Envía comando HIDE vía IPC -> Se cierra la ventana sin congelarse
4. Envía comando SHOW vía IPC -> Se reabre limpiamente una sola ventana
5. Envía comando STOP vía IPC -> Se apaga completamente la app
"""

import os
import subprocess
import sys
import threading
import time

from bindcurse.config import IS_LINUX
from bindcurse.gui.app import VirtualMicApp
from bindcurse.services.network.ipc import send_ipc_command


def get_process_rss_mb() -> float:
    try:
        with open(f"/proc/{os.getpid()}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def is_window_present(substr: str) -> bool:
    try:
        out = subprocess.check_output(["hyprctl", "clients"], stderr=subprocess.DEVNULL).decode("utf-8")
        return any(substr in line for line in out.splitlines())
    except Exception:
        return False


def test_app_ipc_flow():
    port = 5990
    control_port = port + 1
    app = VirtualMicApp(udp_port=port)

    # Correr la app en un hilo separado
    app_thread = threading.Thread(target=lambda: app.run(start_hidden=True), daemon=True)
    app_thread.start()

    time.sleep(1.0)
    print("=" * 60)
    print(" TEST: Flujo IPC y apertura/cierre dinámico de VirtualMicApp")
    print("=" * 60)

    # 1. Comprobar inicio en segundo plano
    st1 = send_ipc_command(control_port, "STATUS")
    print(f"[1] App iniciada en 2do plano. gui_active: {st1.get('gui_active') if st1 else None}")
    assert st1 is not None and not st1.get("gui_active")
    assert not is_window_present("BindCurse — Receptor"), "No debería haber ventana aún"

    # 2. Enviar SHOW
    print("\n[2] Enviando SHOW vía IPC...")
    send_ipc_command(control_port, "SHOW")
    time.sleep(1.0)
    st2 = send_ipc_command(control_port, "STATUS")
    print(f"    gui_active tras SHOW: {st2.get('gui_active') if st2 else None}")
    assert st2 and st2.get("gui_active")
    assert is_window_present("BindCurse — Receptor"), "La ventana debería estar visible"

    # 3. Enviar HIDE
    print("\n[3] Enviando HIDE vía IPC...")
    send_ipc_command(control_port, "HIDE")
    time.sleep(1.0)
    st3 = send_ipc_command(control_port, "STATUS")
    rss_after_hide = get_process_rss_mb()
    print(f"    gui_active tras HIDE: {st3.get('gui_active') if st3 else None} (Daemon RSS: {rss_after_hide:.2f} MB)")
    assert st3 and not st3.get("gui_active")
    assert not is_window_present("BindCurse — Receptor"), "La ventana debería haberse destruido por completo"
    assert "dearpygui" not in sys.modules, "dearpygui no debe cargarse en el proceso demonio"
    assert "numpy" not in sys.modules, "numpy no debe cargarse en el proceso demonio"
    if IS_LINUX:
        assert "sounddevice" not in sys.modules, "sounddevice no debe cargarse en Linux"
        assert rss_after_hide < 50.0, f"RAM del demonio demasiado alta tras HIDE: {rss_after_hide:.2f} MB"

    # 4. Enviar SHOW de nuevo
    print("\n[4] Enviando SHOW de nuevo vía IPC...")
    send_ipc_command(control_port, "SHOW")
    time.sleep(1.0)
    st4 = send_ipc_command(control_port, "STATUS")
    print(f"    gui_active tras 2do SHOW: {st4.get('gui_active') if st4 else None}")
    assert st4 and st4.get("gui_active")
    assert is_window_present("BindCurse — Receptor"), "La ventana debería estar visible nuevamente"

    # 5. Enviar STOP
    print("\n[5] Enviando STOP vía IPC...")
    send_ipc_command(control_port, "STOP")
    app_thread.join(timeout=3.0)
    print(f"    App finalizada limpiamente: {not app_thread.is_alive()}")
    assert not app_thread.is_alive(), "El hilo de la app debería haber terminado"
    assert not is_window_present("BindCurse — Receptor"), "Ninguna ventana debe quedar abierta"

    print("\n[✓] TEST SUPERADO: Flujo completo de IPC y ciclo de vida validado.\n")


if __name__ == "__main__":
    test_app_ipc_flow()
