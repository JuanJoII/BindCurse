from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from virtual_mic_cel.services.tray.base import TrayCallbacks, TrayIcon, TrayState
from virtual_mic_cel.services.tray.icons import render_icon


class WindowsTray(TrayIcon):
    POLL_INTERVAL = 1.0

    def __init__(self, callbacks: TrayCallbacks) -> None:
        super().__init__(callbacks)
        self._icon = None
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self.state: TrayState = callbacks.get_state()

    def start(self) -> None:
        try:
            import pystray
        except Exception as exc:
            self.error = f"pystray no disponible: {exc}"
            return

        cb = self.cb

        def _do(fn: Callable[[], None]) -> Callable:
            def _handler(icon, _item) -> None:
                try:
                    fn()
                finally:
                    self._apply_state(force=True)

            return _handler

        menu = pystray.Menu(
            pystray.MenuItem("Abrir interfaz", _do(cb.show_window), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _i: self.state.status_line, None, enabled=False),
            pystray.MenuItem(
                lambda _i: f"IP: {self.state.ip}   Puerto UDP: {self.state.port}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Escuchar por la salida de audio",
                _do(cb.toggle_monitor),
                checked=lambda _i: self.state.monitor,
            ),
            pystray.MenuItem(
                "Silenciar micrófono",
                _do(cb.toggle_mute),
                checked=lambda _i: self.state.muted,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Apagar y salir", _do(cb.quit_app)),
        )
        st = self.state
        self._icon = pystray.Icon(
            "virtual_mic_cel",
            render_icon(st.icon_state, 64, st.monitor),
            f"Virtual Mic Cel\n{st.tooltip}"[:127],
            menu,
        )
        try:
            self._icon.run_detached()
        except Exception as exc:
            self.error = str(exc)
            return

        self.available = True
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="WinTrayPoll", daemon=True
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        while self._running:
            time.sleep(self.POLL_INTERVAL)
            self._apply_state(force=False)

    def _apply_state(self, force: bool) -> None:
        if self._icon is None:
            return
        try:
            new_state = self.cb.get_state()
        except Exception:
            return
        if not force and new_state == self.state:
            return
        old = self.state
        self.state = new_state
        try:
            if force or (old.icon_state, old.monitor) != (
                new_state.icon_state,
                new_state.monitor,
            ):
                self._icon.icon = render_icon(
                    new_state.icon_state, 64, new_state.monitor
                )
            self._icon.title = f"Virtual Mic Cel\n{new_state.tooltip}"[:127]
            self._icon.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
