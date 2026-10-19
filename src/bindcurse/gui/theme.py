from __future__ import annotations

import dearpygui.dearpygui as dpg

from bindcurse.gui.helpers import find_system_font


def setup_theme_and_fonts() -> int | str | None:
    """Configures fonts and visual theme in Dear PyGui context. Returns large_font if loaded."""
    reg_font_path = find_system_font(bold=False)
    bold_font_path = find_system_font(bold=True) or reg_font_path
    large_font = None

    if reg_font_path:
        with dpg.font_registry():
            default_font = dpg.add_font(reg_font_path, 16)
            dpg.bind_font(default_font)

            if bold_font_path:
                large_font = dpg.add_font(bold_font_path, 25)

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

    return large_font


def create_danger_button_theme() -> int | str:
    """Creates a distinct red theme for destructive actions like Quit."""
    with dpg.theme() as danger_btn_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (175, 45, 55, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (205, 60, 70, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (145, 35, 45, 255))
    return danger_btn_theme
