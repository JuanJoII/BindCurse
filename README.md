# BindCurse

Aplicación en Python con interfaz **Dear PyGui** que crea un micrófono virtual en el sistema (**PipeWire / PulseAudio** en Linux o **VB-Cable** en Windows) y recibe audio **PCM (16-bit signed LE, 48000 Hz, mono)** en tiempo real por un socket **UDP en el puerto 5005**.

## Características

- **Ejecución en segundo plano y ahorro extremo de recursos**:
  - Al cerrar la ventana (o pulsar **Minimizar a 2do Plano**), el contexto OpenGL/Dear PyGui se destruye por completo (`0% GPU` y sin bucle de renderizado) y el receptor UDP pasa a modo *zero-copy* (sin conversiones NumPy ni cálculo de medidores VU), reduciendo el consumo de CPU prácticamente a `0%`.
  - Si vuelves a ejecutar `uv run bindcurse`, la instancia en segundo plano se detecta automáticamente y **reabre la interfaz gráfica al instante** conservando el estado y las estadísticas.
- **Icono en la bandeja del sistema**:
  - **Linux**: icono nativo *StatusNotifierItem* (D-Bus) que aparece en Waybar, Quickshell (Omarchy), KDE, etc. No necesita GTK ni AppIndicator.
  - **Windows**: icono en el área de notificación de la barra de tareas (`pystray`).
  - **Clic izquierdo**: abre la interfaz. **Clic central** (Linux): silencia o reactiva el micrófono. **Clic derecho**: menú con estado, IP:puerto, *Escuchar por la salida de audio*, *Silenciar micrófono* y *Apagar y salir*.
  - El color cambia según el estado: gris = esperando, verde = transmitiendo, rojo = silenciado, naranja = error del micrófono virtual; un punto azul indica que el monitor está activo.
- **Creación automática del micrófono virtual**:
  - **Linux (PipeWire / PulseAudio)**: Crea `BindCurse_Mic` (`bindcurse_source`) reconocido como micrófono real por Discord, OBS, navegadores, etc.
  - **Windows (VB-Cable)**: Se conecta automáticamente a `CABLE Input (VB-Audio Virtual Cable)`.
- **Datos de conexión para el móvil**: Muestra en grande la dirección **IP local** y el **puerto UDP (`5005`)** para ingresarlos en el dispositivo móvil.
- **Monitoreo en tiempo real**: Opción para **escuchar el micrófono por la salida de audio del sistema (Monitor)** con control de volumen independiente y ganancia/mute del micrófono.

## Comandos (`uv`)

```bash
# Iniciar la aplicación (o reabrir su ventana si ya está en segundo plano)
uv run bindcurse

# Iniciar directamente en segundo plano sin abrir ventana
uv run bindcurse --background

# Consultar estado y dispositivos conectados desde la terminal
uv run bindcurse --status

# Activar/desactivar escucha del micrófono en la salida del sistema en caliente
uv run bindcurse --toggle-monitor

# Silenciar/activar el micrófono virtual en caliente
uv run bindcurse --toggle-mute

# Apagar por completo el servicio en segundo plano y eliminar el micrófono virtual
uv run bindcurse --stop
```
