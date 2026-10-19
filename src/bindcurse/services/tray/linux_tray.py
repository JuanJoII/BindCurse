"""
Linux StatusNotifierItem + com.canonical.dbusmenu implementation over D-Bus with dbus-fast.

NOTE: This file intentionally does NOT use `from __future__ import annotations`
because dbus-fast inspects string types for D-Bus signature generation.
"""

import asyncio
import os
import threading
from typing import Callable, Optional

from bindcurse.services.tray.base import TrayCallbacks, TrayIcon, TrayState
from bindcurse.services.tray.icons import render_icon, to_argb32_bytes

# Menu item IDs
M_ROOT, M_OPEN, M_SEP1, M_STATUS, M_ADDR, M_SEP2, M_MONITOR, M_MUTE, M_SEP3, M_QUIT = (
    range(10)
)


class LinuxSNITray(TrayIcon):
    POLL_INTERVAL = 1.0

    def __init__(self, callbacks: TrayCallbacks) -> None:
        super().__init__(callbacks)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._bus = None
        self._item = None
        self._menu = None
        self._stop_evt: Optional[asyncio.Event] = None
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.state: TrayState = callbacks.get_state()
        self.revision = 1

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._thread_main, name="SNITray", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        loop, evt = self._loop, self._stop_evt
        if loop is not None and evt is not None and loop.is_running():
            loop.call_soon_threadsafe(evt.set)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)

    def pixmaps(self) -> list:
        st = self.state
        return [
            [
                size,
                size,
                to_argb32_bytes(render_icon(st.icon_state, size, st.monitor)),
            ]
            for size in (22, 32, 48, 64)
        ]

    def run_action(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=self._safe_call, args=(fn,), daemon=True).start()

    def request_refresh(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._refresh(force=False))
            )

    @staticmethod
    def _safe_call(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            pass

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:
            self.available = False
            self.error = str(exc)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _main(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        self._stop_evt = asyncio.Event()
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self._bus = bus

        self._item = _SNIItemInterface(self)
        self._menu = _DBusMenuInterface(self)
        bus.export("/StatusNotifierItem", self._item)
        bus.export("/MenuBar", self._menu)
        await bus.request_name(self.bus_name)

        await self._register_with_watcher()
        await self._watch_for_watcher_restarts()

        try:
            while not self._stop_evt.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_evt.wait(), timeout=self.POLL_INTERVAL
                    )
                except asyncio.TimeoutError:
                    pass
                await self._refresh(force=False)
        finally:
            try:
                bus.disconnect()
            except Exception:
                pass

    async def _register_with_watcher(self) -> None:
        from dbus_fast import Message, MessageType

        reply = await self._bus.call(
            Message(
                destination="org.kde.StatusNotifierWatcher",
                path="/StatusNotifierWatcher",
                interface="org.kde.StatusNotifierWatcher",
                member="RegisterStatusNotifierItem",
                signature="s",
                body=[self.bus_name],
            )
        )
        if reply is not None and reply.message_type == MessageType.ERROR:
            self.available = False
            self.error = f"No hay bandeja del sistema (StatusNotifierWatcher): {reply.error_name}"
        else:
            self.available = True
            self.error = None

    async def _watch_for_watcher_restarts(self) -> None:
        from dbus_fast import Message

        rule = (
            "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
            "member='NameOwnerChanged',arg0='org.kde.StatusNotifierWatcher'"
        )
        await self._bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[rule],
            )
        )

        def _handler(msg) -> None:
            if (
                msg.member == "NameOwnerChanged"
                and msg.body
                and msg.body[0] == "org.kde.StatusNotifierWatcher"
                and msg.body[2]
            ):
                asyncio.ensure_future(self._delayed_reregister())

        self._bus.add_message_handler(_handler)

    async def _delayed_reregister(self) -> None:
        await asyncio.sleep(0.5)
        try:
            await self._register_with_watcher()
        except Exception:
            pass

    async def _refresh(self, force: bool) -> None:
        try:
            new_state = self.cb.get_state()
        except Exception:
            return
        old = self.state
        if not force and new_state == old:
            return
        self.state = new_state
        if self._item is None or self._menu is None:
            return
        if (new_state.icon_state, new_state.monitor) != (
            old.icon_state,
            old.monitor,
        ) or force:
            self._item.NewIcon()
        self._item.NewToolTip()
        self.revision += 1
        self._menu.LayoutUpdated()


try:
    from dbus_fast import PropertyAccess, Variant
    from dbus_fast.service import (
        ServiceInterface,
        dbus_property,
        method,
        signal as dbus_signal,
    )

    class _SNIItemInterface(ServiceInterface):
        def __init__(self, tray: LinuxSNITray) -> None:
            super().__init__("org.kde.StatusNotifierItem")
            self.tray = tray

        @dbus_property(access=PropertyAccess.READ)
        def Category(self) -> "s":
            return "Hardware"

        @dbus_property(access=PropertyAccess.READ)
        def Id(self) -> "s":
            return "bindcurse"

        @dbus_property(access=PropertyAccess.READ)
        def Title(self) -> "s":
            return "BindCurse"

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":
            return "Active"

        @dbus_property(access=PropertyAccess.READ)
        def WindowId(self) -> "i":
            return 0

        @dbus_property(access=PropertyAccess.READ)
        def IconName(self) -> "s":
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def IconThemePath(self) -> "s":
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def IconPixmap(self) -> "a(iiay)":
            return self.tray.pixmaps()

        @dbus_property(access=PropertyAccess.READ)
        def OverlayIconName(self) -> "s":
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def OverlayIconPixmap(self) -> "a(iiay)":
            return []

        @dbus_property(access=PropertyAccess.READ)
        def AttentionIconName(self) -> "s":
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def AttentionIconPixmap(self) -> "a(iiay)":
            return []

        @dbus_property(access=PropertyAccess.READ)
        def AttentionMovieName(self) -> "s":
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def ToolTip(self) -> "(sa(iiay)ss)":
            st = self.tray.state
            return ["", [], "BindCurse", st.tooltip]

        @dbus_property(access=PropertyAccess.READ)
        def ItemIsMenu(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Menu(self) -> "o":
            return "/MenuBar"

        @method()
        def Activate(self, x: "i", y: "i"):
            self.tray.run_action(self.tray.cb.show_window)

        @method()
        def SecondaryActivate(self, x: "i", y: "i"):
            self.tray.run_action(self._toggle_mute_and_refresh)

        @method()
        def ContextMenu(self, x: "i", y: "i"):
            pass

        @method()
        def Scroll(self, delta: "i", orientation: "s"):
            pass

        @dbus_signal()
        def NewIcon(self):
            pass

        @dbus_signal()
        def NewToolTip(self):
            pass

        @dbus_signal()
        def NewStatus(self) -> "s":
            return "Active"

        @dbus_signal()
        def NewTitle(self):
            pass

        def _toggle_mute_and_refresh(self) -> None:
            self.tray.cb.toggle_mute()
            self.tray.request_refresh()

    class _DBusMenuInterface(ServiceInterface):
        def __init__(self, tray: LinuxSNITray) -> None:
            super().__init__("com.canonical.dbusmenu")
            self.tray = tray

        def _item_props(self, item_id: int) -> dict:
            st = self.tray.state
            if item_id == M_ROOT:
                return {"children-display": Variant("s", "submenu")}
            if item_id in (M_SEP1, M_SEP2, M_SEP3):
                return {"type": Variant("s", "separator")}
            if item_id == M_OPEN:
                return {"label": Variant("s", "Abrir interfaz")}
            if item_id == M_STATUS:
                return {
                    "label": Variant("s", st.status_line),
                    "enabled": Variant("b", False),
                }
            if item_id == M_ADDR:
                return {
                    "label": Variant("s", f"IP: {st.ip}   Puerto UDP: {st.port}"),
                    "enabled": Variant("b", False),
                }
            if item_id == M_MONITOR:
                return {
                    "label": Variant("s", "Escuchar por la salida de audio"),
                    "toggle-type": Variant("s", "checkmark"),
                    "toggle-state": Variant("i", 1 if st.monitor else 0),
                }
            if item_id == M_MUTE:
                return {
                    "label": Variant("s", "Silenciar micrófono"),
                    "toggle-type": Variant("s", "checkmark"),
                    "toggle-state": Variant("i", 1 if st.muted else 0),
                }
            if item_id == M_QUIT:
                return {"label": Variant("s", "Apagar y salir")}
            return {}

        CHILDREN = [
            M_OPEN,
            M_SEP1,
            M_STATUS,
            M_ADDR,
            M_SEP2,
            M_MONITOR,
            M_MUTE,
            M_SEP3,
            M_QUIT,
        ]

        def _layout(self, item_id: int, depth: int) -> list:
            children = []
            if item_id == M_ROOT and depth != 0:
                children = [
                    Variant("(ia{sv}av)", self._layout(cid, depth - 1))
                    for cid in self.CHILDREN
                ]
            return [item_id, self._item_props(item_id), children]

        def _all_ids(self) -> list:
            return [M_ROOT] + self.CHILDREN

        def _on_click(self, item_id: int) -> None:
            cb = self.tray.cb
            actions = {
                M_OPEN: cb.show_window,
                M_MONITOR: cb.toggle_monitor,
                M_MUTE: cb.toggle_mute,
                M_QUIT: cb.quit_app,
            }
            fn = actions.get(item_id)
            if fn is None:
                return

            def _run() -> None:
                fn()
                self.tray.request_refresh()

            self.tray.run_action(_run)

        @dbus_property(access=PropertyAccess.READ)
        def Version(self) -> "u":
            return 3

        @dbus_property(access=PropertyAccess.READ)
        def TextDirection(self) -> "s":
            return "ltr"

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":
            return "normal"

        @dbus_property(access=PropertyAccess.READ)
        def IconThemePath(self) -> "as":
            return []

        @method()
        def GetLayout(
            self, parent_id: "i", recursion_depth: "i", property_names: "as"
        ) -> "u(ia{sv}av)":
            return [self.tray.revision, self._layout(parent_id, recursion_depth)]

        @method()
        def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
            targets = list(ids) if ids else self._all_ids()
            return [[i, self._item_props(i)] for i in targets]

        @method()
        def GetProperty(self, item_id: "i", name: "s") -> "v":
            return self._item_props(item_id).get(name, Variant("s", ""))

        @method()
        def Event(self, item_id: "i", event_id: "s", data: "v", timestamp: "u"):
            if event_id == "clicked":
                self._on_click(item_id)

        @method()
        def EventGroup(self, events: "a(isvu)") -> "ai":
            for item_id, event_id, _data, _ts in events:
                if event_id == "clicked":
                    self._on_click(item_id)
            return []

        @method()
        def AboutToShow(self, item_id: "i") -> "b":
            return False

        @method()
        def AboutToShowGroup(self, ids: "ai") -> "aiai":
            return [[], []]

        @dbus_signal()
        def LayoutUpdated(self) -> "ui":
            return [self.tray.revision, 0]

        @dbus_signal()
        def ItemsPropertiesUpdated(self) -> "a(ia{sv})a(ias)":
            return [[], []]

except ImportError:
    pass
