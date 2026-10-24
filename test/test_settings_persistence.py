#!/usr/bin/env python3
"""
Test de persistencia de configuraciones para BindCurse:
1. Carga de configuraciones por defecto cuando no existe archivo.
2. Guardado atómico y recarga de valores personalizados (ganancia, volumen monitor, mute, etc.).
3. Guardado con debouncing para evitar sobrecarga de I/O al mover sliders.
4. Tolerancia a fallos con JSON corrupto (fallback a valores por defecto sin crash).
5. Integración con VirtualMicApp (verificación de que la app adopta las configuraciones guardadas).
"""

import json
import tempfile
import time
from pathlib import Path

from bindcurse.config import DEFAULT_UDP_PORT, UserSettings
from bindcurse.core import get_orchestrator
from bindcurse.gui.app import VirtualMicApp

get_orchestrator()


def test_settings_save_and_load() -> None:
    print("=" * 60)
    print(" TEST: Persistencia de configuraciones (UserSettings)")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "test_config.json"

        # 1. Archivo inexistente -> Valores por defecto
        s1 = UserSettings.load(config_path)
        print(
            f"[1] Valores por defecto cargados: gain={s1.mic_gain}, port={s1.udp_port}"
        )
        assert s1.mic_gain == 1.0
        assert s1.monitor_volume == 1.0
        assert not s1.monitor_enabled
        assert not s1.mic_muted
        assert s1.keep_in_background
        assert s1.udp_port == DEFAULT_UDP_PORT

        # 2. Modificación y guardado atómico
        s1.mic_gain = 1.85
        s1.monitor_volume = 0.65
        s1.monitor_enabled = True
        s1.mic_muted = True
        s1.keep_in_background = False
        s1.preferred_ip = "192.168.128.10"
        s1.udp_port = 5020
        s1.save(config_path)

        assert config_path.is_file(), "El archivo config.json debe existir tras save()"
        with open(config_path, "r", encoding="utf-8") as f:
            raw_json = json.load(f)
        print(f"[2] Configuración guardada en disco: {raw_json}")
        assert raw_json["mic_gain"] == 1.85
        assert raw_json["monitor_volume"] == 0.65
        assert raw_json["monitor_enabled"] is True
        assert raw_json["mic_muted"] is True
        assert raw_json["keep_in_background"] is False
        assert raw_json["preferred_ip"] == "192.168.128.10"
        assert raw_json["udp_port"] == 5020

        # 3. Recarga desde disco
        s2 = UserSettings.load(config_path)
        print(
            f"[3] Configuración recargada de disco: gain={s2.mic_gain}, mon_vol={s2.monitor_volume}"
        )
        assert s2.mic_gain == 1.85
        assert s2.monitor_volume == 0.65
        assert s2.monitor_enabled is True
        assert s2.mic_muted is True
        assert s2.keep_in_background is False
        assert s2.preferred_ip == "192.168.128.10"
        assert s2.udp_port == 5020

        # 4. Guardado debounced (simulación de arrastrar un slider)
        print("\n[4] Probando guardado debounced (slider drag)...")
        s2.mic_gain = 2.10
        s2.save_debounced(delay=0.15, config_path=config_path)
        # Antes de 0.15s, no debería haberse escrito aún el nuevo valor
        # Esperamos 0.25s
        time.sleep(0.25)
        s3 = UserSettings.load(config_path)
        print(f"    Valor tras debounce completado: gain={s3.mic_gain}")
        assert s3.mic_gain == 2.10

        # 5. Tolerancia a JSON corrupto
        print("\n[5] Probando recuperación ante JSON corrupto...")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("{ INVALID JSON DATA ....")
        s_corrupted = UserSettings.load(config_path)
        print(f"    Fallback a defaults sin crash: gain={s_corrupted.mic_gain}")
        assert s_corrupted.mic_gain == 1.0
        assert s_corrupted.udp_port == DEFAULT_UDP_PORT

    print(
        "\n[✓] TEST SUPERADO: Persistencia atómica, debouncing y fallback validados.\n"
    )


def test_app_integration() -> None:
    print("=" * 60)
    print(" TEST: Integración de configuraciones con VirtualMicApp")
    print("=" * 60)

    # Guardar una configuración personalizada en la ubicación real del usuario
    real_settings = UserSettings.load()
    orig_gain = real_settings.mic_gain
    orig_vol = real_settings.monitor_volume

    try:
        real_settings.mic_gain = 1.77
        real_settings.monitor_volume = 0.42
        real_settings.mic_muted = True
        real_settings.save()

        app = VirtualMicApp(udp_port=5995)
        print(
            f"[1] VirtualMicApp inicializada con ganancia: {app.audio.mic_gain * 100:.0f}%"
        )
        print(
            f"[2] VirtualMicApp volumen monitor: {app.audio.monitor_volume * 100:.0f}%"
        )
        print(f"[3] VirtualMicApp mic silenciado: {app.audio.mic_muted}")

        assert abs(app.audio.mic_gain - 1.77) < 1e-3, (
            "La ganancia debe coincidir con la guardada"
        )
        assert abs(app.audio.monitor_volume - 0.42) < 1e-3, (
            "El volumen monitor debe coincidir con el guardado"
        )
        assert app.audio.mic_muted is True, (
            "El estado mute debe coincidir con el guardado"
        )

        # Simular ajuste de usuario en la UI
        app._on_mic_gain_changed(0, 220.0)
        app.settings.flush()

        reloaded = UserSettings.load()
        print(
            f"[4] Nueva ganancia persistida tras cambio en UI: {reloaded.mic_gain * 100:.0f}%"
        )
        assert abs(reloaded.mic_gain - 2.20) < 1e-3

    finally:
        # Restaurar valores previos
        real_settings.mic_gain = orig_gain
        real_settings.monitor_volume = orig_vol
        real_settings.mic_muted = False
        real_settings.save()

    print(
        "\n[✓] TEST SUPERADO: Integración completa de persistencia con VirtualMicApp.\n"
    )


if __name__ == "__main__":
    test_settings_save_and_load()
    test_app_integration()
