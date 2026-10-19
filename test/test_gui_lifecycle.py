#!/usr/bin/env python3
"""
Test de ciclo de vida de la interfaz DearPyGui en BindCurse:
Verifica que al cerrar la interfaz, la ventana nativa (GLFW / X11 / Wayland)
se destruye por completo sin congelarse ni dejar ventanas zombi,
y que al reabrir la ventana se crea una única instancia activa.
"""

import subprocess
import time

import dearpygui.dearpygui as dpg

from bindcurse.gui.helpers import terminate_dpg_glfw


def get_hyprland_windows() -> list[str]:
    try:
        out = subprocess.check_output(["hyprctl", "clients"], stderr=subprocess.DEVNULL).decode("utf-8")
        return [line for line in out.splitlines() if "title:" in line]
    except Exception:
        return []


def is_window_in_hyprland(title_substr: str) -> bool:
    lines = get_hyprland_windows()
    return any(title_substr in l for l in lines)


def test_session_lifecycle():
    title = "BindCurse_LifecycleTest"
    print("=" * 60)
    print(" TEST: Ciclo de vida de GUI y destrucción de ventana")
    print("=" * 60)

    # 1. Sesión 1
    print("\n[1] Iniciando primera sesión GUI...")
    dpg.create_context()
    dpg.create_viewport(title=title, width=400, height=300)
    dpg.setup_dearpygui()
    dpg.show_viewport()

    for _ in range(5):
        dpg.render_dearpygui_frame()
        time.sleep(0.01)

    in_hypr_1 = is_window_in_hyprland(title)
    print(f"    Ventana detectada en compositor (abierta): {in_hypr_1}")
    assert in_hypr_1, "Error: La ventana debería estar presente en el compositor"

    print("    Cerrando primera sesión y ejecutando terminate_dpg_glfw()...")
    dpg.stop_dearpygui()
    try:
        dpg.destroy_context()
    except Exception:
        pass
    terminate_dpg_glfw()

    time.sleep(0.3)
    in_hypr_after_1 = is_window_in_hyprland(title)
    print(f"    Ventana detectada en compositor (tras cerrar): {in_hypr_after_1}")
    assert not in_hypr_after_1, "Error: La ventana no se cerró y quedó congelada/zombi!"

    # 2. Sesión 2 (Simula clic en tray icon o 'uv run bindcurse' para reabrir)
    print("\n[2] Reabriendo interfaz (segunda sesión)...")
    dpg.create_context()
    dpg.create_viewport(title=title, width=400, height=300)
    dpg.setup_dearpygui()
    dpg.show_viewport()

    for _ in range(5):
        dpg.render_dearpygui_frame()
        time.sleep(0.01)

    matching_titles = [l for l in get_hyprland_windows() if title in l]
    print(f"    Número de ventanas detectadas tras reabrir: {len(matching_titles)}")
    assert len(matching_titles) == 1, f"Error: Se esperaban 1 ventana, se encontraron {len(matching_titles)}"

    print("    Cerrando segunda sesión...")
    dpg.stop_dearpygui()
    try:
        dpg.destroy_context()
    except Exception:
        pass
    terminate_dpg_glfw()

    time.sleep(0.3)
    in_hypr_after_2 = is_window_in_hyprland(title)
    print(f"    Ventana detectada en compositor tras segundo cierre: {in_hypr_after_2}")
    assert not in_hypr_after_2, "Error: La ventana no se destruyó en el segundo cierre!"

    print("\n[✓] TEST SUPERADO: Cierre, destrucción limpia y reapertura funcionan correctamente.\n")


if __name__ == "__main__":
    test_session_lifecycle()
