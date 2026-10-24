from __future__ import annotations

import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from bindcurse.core.config import CURRENT_PLATFORM, IS_LINUX, IS_WINDOWS

if TYPE_CHECKING:
    from bindcurse.services.audio.linux_backend import LinuxAudioBackend
    from bindcurse.services.audio.windows_backend import WindowsAudioBackend
    from bindcurse.services.tray.base import TrayCallbacks, TrayIcon


class PlatformOrchestrator(ABC):
    """
    Abstract base orchestrator that isolates OS-specific runtime, process lifecycle,
    GUI viewport/font management, network interface probing, system tray, and audio
    backend creation between Linux and Windows.
    """

    platform_name: str = CURRENT_PLATFORM

    @abstractmethod
    def spawn_detached_daemon(
        self, port: int, background: bool = False
    ) -> subprocess.Popen[bytes]:
        """Spawns the background BindCurse daemon detached from the current terminal."""

    @abstractmethod
    def spawn_gui_process(self, control_port: int) -> subprocess.Popen[bytes]:
        """Spawns the isolated DearPyGui viewport child process."""

    @abstractmethod
    def stop_gui_process(
        self,
        proc: subprocess.Popen[bytes] | None,
        graceful_timeout: float = 0.0,
    ) -> None:
        """Stops the isolated DearPyGui child process cleanly."""

    @abstractmethod
    def notify_user(self, title: str, body: str) -> None:
        """Sends a non-blocking desktop notification if supported by the OS."""

    @abstractmethod
    def find_system_font(self, bold: bool = False) -> str | None:
        """Locates a system TTF font supporting Latin-1 / Spanish characters."""

    def setup_gui_viewport(self, title: str) -> None:
        """Applies OS-specific post-creation fixes to the native GUI viewport."""
        return

    @abstractmethod
    def cleanup_gui_viewport(self) -> None:
        """Cleans up native display server resources after DearPyGui context destruction."""

    @abstractmethod
    def get_local_ips(self, primary_ip: str | None = None) -> list[tuple[str, str]]:
        """Returns active local IPv4 (label, ip) pairs for the current OS."""

    @abstractmethod
    def create_tray(self, callbacks: TrayCallbacks) -> TrayIcon:
        """Creates the platform-specific system tray icon handler."""

    @abstractmethod
    def create_audio_backends(
        self, sample_rate: int, channels: int
    ) -> tuple[LinuxAudioBackend | None, WindowsAudioBackend | None]:
        """Lazily instantiates the platform-specific audio backend."""

    @abstractmethod
    def setup_audio_backend(
        self,
        linux_backend: LinuxAudioBackend | None,
        windows_backend: WindowsAudioBackend | None,
    ) -> tuple[bool, str, str, str]:
        """
        Initializes the virtual microphone backend.
        Returns (ready, backend_name, device_label, status_message).
        """

    @abstractmethod
    def get_matching_windows(self, title_substr: str) -> list[str]:
        """Returns a list of visible window titles matching `title_substr`."""

    def is_window_present(self, title_substr: str) -> bool:
        """Returns True if at least one visible window matches `title_substr`."""
        return len(self.get_matching_windows(title_substr)) > 0


class LinuxOrchestrator(PlatformOrchestrator):
    """Encapsulates all Linux-specific orchestration (PipeWire/PulseAudio, Wayland/X11, SNI Tray)."""

    platform_name = "linux"

    def spawn_detached_daemon(
        self, port: int, background: bool = False
    ) -> subprocess.Popen[bytes]:
        cmd = [
            sys.executable,
            "-m",
            "bindcurse",
            "--foreground",
            "--port",
            str(port),
        ]
        if background:
            cmd.append("--background")
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )

    def spawn_gui_process(self, control_port: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bindcurse.gui.window",
                str(control_port),
            ],
            stdin=subprocess.DEVNULL,
        )

    def stop_gui_process(
        self,
        proc: subprocess.Popen[bytes] | None,
        graceful_timeout: float = 0.0,
    ) -> None:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=max(0.4, graceful_timeout))
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def notify_user(self, title: str, body: str) -> None:
        if shutil.which("notify-send"):
            try:
                subprocess.Popen(
                    ["notify-send", "-a", "BindCurse", title, body],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    def find_system_font(self, bold: bool = False) -> str | None:
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
            ]
            if bold
            else [
                "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf",
                "/usr/share/fonts/noto/NotoSans-Regular.ttf",
            ]
        )
        for path in candidates:
            if os.path.isfile(path):
                return path
        return None

    def cleanup_gui_viewport(self) -> None:
        try:
            import ctypes
            import dearpygui._dearpygui as _dpg_ext

            lib = ctypes.CDLL(_dpg_ext.__file__)
            if hasattr(lib, "glfwTerminate"):
                lib.glfwTerminate.restype = None
                lib.glfwTerminate.argtypes = []
                lib.glfwTerminate()
        except Exception:
            pass

    def get_local_ips(self, primary_ip: str | None = None) -> list[tuple[str, str]]:
        from bindcurse.services.network.os_network import get_linux_ips

        return get_linux_ips(primary_ip)

    def create_tray(self, callbacks: TrayCallbacks) -> TrayIcon:
        from bindcurse.services.tray.linux_tray import LinuxSNITray

        return LinuxSNITray(callbacks)

    def create_audio_backends(
        self, sample_rate: int, channels: int
    ) -> tuple[LinuxAudioBackend | None, WindowsAudioBackend | None]:
        from bindcurse.services.audio.linux_backend import LinuxAudioBackend

        return LinuxAudioBackend(sample_rate, channels), None

    def setup_audio_backend(
        self,
        linux_backend: LinuxAudioBackend | None,
        windows_backend: WindowsAudioBackend | None,
    ) -> tuple[bool, str, str, str]:
        if linux_backend is None:
            return False, "No inicializado", "-", "Backend Linux no disponible"
        ok, label, msg = linux_backend.setup()
        backend_name = (
            "PipeWire / PulseAudio" if ok else "PipeWire / PulseAudio (Error)"
        )
        return ok, backend_name, label, msg

    def get_matching_windows(self, title_substr: str) -> list[str]:
        try:
            out = subprocess.check_output(
                ["hyprctl", "clients"], stderr=subprocess.DEVNULL
            ).decode("utf-8", errors="replace")
            return [
                line
                for line in out.splitlines()
                if "title:" in line and title_substr in line
            ]
        except Exception:
            return []


class WindowsOrchestrator(PlatformOrchestrator):
    """Encapsulates all Windows-specific orchestration (Win32/DX11, pythonw, VB-Cable, WinTray)."""

    platform_name = "windows"

    def __init__(self) -> None:
        super().__init__()
        self._configure_console_encoding()

    @staticmethod
    def _configure_console_encoding() -> None:
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    @staticmethod
    def _resolve_pythonw() -> tuple[str, bool]:
        """Returns (executable_path, is_pythonw)."""
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.isfile(pythonw):
            return pythonw, True
        return sys.executable, False

    def spawn_detached_daemon(
        self, port: int, background: bool = False
    ) -> subprocess.Popen[bytes]:
        exe, _ = self._resolve_pythonw()
        cmd = [
            exe,
            "-m",
            "bindcurse",
            "--foreground",
            "--port",
            str(port),
        ]
        if background:
            cmd.append("--background")
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )

    def spawn_gui_process(self, control_port: int) -> subprocess.Popen[bytes]:
        gui_exe, is_pythonw = self._resolve_pythonw()
        creationflags = 0 if is_pythonw else getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # Always redirect stdin, stdout, and stderr to DEVNULL on Windows so
        # subprocess._get_handles never attempts DuplicateHandle on invalid
        # parent handles when running inside a detached pythonw.exe daemon.
        return subprocess.Popen(
            [
                gui_exe,
                "-m",
                "bindcurse.gui.window",
                str(control_port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )

    def stop_gui_process(
        self,
        proc: subprocess.Popen[bytes] | None,
        graceful_timeout: float = 0.0,
    ) -> None:
        if proc is None or proc.poll() is not None:
            return
        if graceful_timeout > 0.0:
            try:
                proc.wait(timeout=graceful_timeout)
                return
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def notify_user(self, title: str, body: str) -> None:
        # Windows notifications are handled via tray tooltip/state; no notify-send subprocess.
        return

    def find_system_font(self, bold: bool = False) -> str | None:
        win_dir = (
            os.environ.get("SystemRoot") or os.environ.get("windir") or "C:/Windows"
        )
        win_fonts = os.path.join(win_dir, "Fonts")
        candidates = (
            [
                os.path.join(win_fonts, "segoeuib.ttf"),
                os.path.join(win_fonts, "arialbd.ttf"),
                "C:/Windows/Fonts/segoeuib.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
            ]
            if bold
            else [
                os.path.join(win_fonts, "segoeui.ttf"),
                os.path.join(win_fonts, "arial.ttf"),
                "C:/Windows/Fonts/segoeui.ttf",
                "C:/Windows/Fonts/arial.ttf",
            ]
        )
        for path in candidates:
            if os.path.isfile(path):
                return path
        return None

    def setup_gui_viewport(self, title: str) -> None:
        """
        DearPyGui on Windows uses `CreateWindowExA` / `SetWindowTextA`, which
        misinterprets UTF-8 characters (like '—' and 'ó') under Windows ANSI codepages.
        Explicitly calling `SetWindowTextW` sets the proper UTF-16 window title.
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.FindWindowExW.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
            ]
            user32.FindWindowExW.restype = wintypes.HWND
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            ]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
            user32.SetWindowTextW.restype = wintypes.BOOL

            my_pid = os.getpid()
            hwnd = None
            pid = wintypes.DWORD()
            while True:
                hwnd = user32.FindWindowExW(None, hwnd, None, None)
                if not hwnd:
                    break
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == my_pid and user32.IsWindowVisible(hwnd):
                    user32.SetWindowTextW(hwnd, title)
        except Exception:
            pass

    def cleanup_gui_viewport(self) -> None:
        """
        DearPyGui's `destroy_context()` on Windows frees `GContext` without calling
        `DestroyWindow(hwnd)`, leaving the native Win32 HWND alive with a dangling
        `WndProc` pointer. Rebinding `GWLP_WNDPROC` to `DefWindowProcW` and calling
        `DestroyWindow` cleanly destroys any remaining viewport window in the process.
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.FindWindowExW.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
            ]
            user32.FindWindowExW.restype = wintypes.HWND
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            ]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.SetWindowLongPtrW.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_void_p,
            ]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.DestroyWindow.restype = wintypes.BOOL

            def_wnd_proc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value
            my_pid = os.getpid()
            hwnd = None
            pid = wintypes.DWORD()
            to_destroy: list[int] = []
            while True:
                hwnd = user32.FindWindowExW(None, hwnd, None, None)
                if not hwnd:
                    break
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == my_pid and user32.IsWindowVisible(hwnd):
                    to_destroy.append(hwnd)
            for h in to_destroy:
                user32.SetWindowLongPtrW(h, -4, def_wnd_proc)
                user32.DestroyWindow(h)
        except Exception:
            pass

    def get_local_ips(self, primary_ip: str | None = None) -> list[tuple[str, str]]:
        from bindcurse.services.network.os_network import get_windows_ips

        return get_windows_ips(primary_ip)

    def create_tray(self, callbacks: TrayCallbacks) -> TrayIcon:
        from bindcurse.services.tray.windows_tray import WindowsTray

        return WindowsTray(callbacks)

    def create_audio_backends(
        self, sample_rate: int, channels: int
    ) -> tuple[LinuxAudioBackend | None, WindowsAudioBackend | None]:
        from bindcurse.services.audio.windows_backend import WindowsAudioBackend

        return None, WindowsAudioBackend(sample_rate, channels)

    def setup_audio_backend(
        self,
        linux_backend: LinuxAudioBackend | None,
        windows_backend: WindowsAudioBackend | None,
    ) -> tuple[bool, str, str, str]:
        if windows_backend is None:
            return False, "No inicializado", "-", "Backend Windows no disponible"
        ok, label, msg = windows_backend.setup()
        backend_name = "VB-Audio Virtual Cable" if ok else "VB-Cable (No detectado)"
        return ok, backend_name, label, msg

    def get_matching_windows(self, title_substr: str) -> list[str]:
        matches: list[str] = []
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.FindWindowExW.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
            ]
            user32.FindWindowExW.restype = wintypes.HWND
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.InternalGetWindowText.argtypes = [
                wintypes.HWND,
                wintypes.LPWSTR,
                ctypes.c_int,
            ]
            user32.InternalGetWindowText.restype = ctypes.c_int

            hwnd = None
            buf = ctypes.create_unicode_buffer(512)
            while True:
                hwnd = user32.FindWindowExW(None, hwnd, None, None)
                if not hwnd:
                    break
                if user32.IsWindowVisible(hwnd):
                    length = user32.InternalGetWindowText(hwnd, buf, 512)
                    if length > 0:
                        raw_title = buf.value
                        normalized = raw_title
                        try:
                            normalized = raw_title.encode("cp1252").decode("utf-8")
                        except Exception:
                            pass
                        if title_substr in raw_title or title_substr in normalized:
                            matches.append(normalized)
        except Exception:
            pass
        return matches


class FallbackOrchestrator(LinuxOrchestrator):
    """Fallback orchestrator for unsupported platforms."""

    platform_name = CURRENT_PLATFORM

    def create_tray(self, callbacks: TrayCallbacks) -> TrayIcon:
        from bindcurse.services.tray.base import TrayIcon

        return TrayIcon(callbacks)

    def create_audio_backends(
        self, sample_rate: int, channels: int
    ) -> tuple[LinuxAudioBackend | None, WindowsAudioBackend | None]:
        return None, None

    def setup_audio_backend(
        self,
        linux_backend: LinuxAudioBackend | None,
        windows_backend: WindowsAudioBackend | None,
    ) -> tuple[bool, str, str, str]:
        msg = f"Plataforma no soportada: {self.platform_name}"
        return False, "No inicializado", "-", msg


_ORCHESTRATOR_INSTANCE: PlatformOrchestrator | None = None


def get_orchestrator() -> PlatformOrchestrator:
    """Returns the singleton platform orchestrator for the current operating system."""
    global _ORCHESTRATOR_INSTANCE
    if _ORCHESTRATOR_INSTANCE is None:
        if IS_LINUX:
            _ORCHESTRATOR_INSTANCE = LinuxOrchestrator()
        elif IS_WINDOWS:
            _ORCHESTRATOR_INSTANCE = WindowsOrchestrator()
        else:
            _ORCHESTRATOR_INSTANCE = FallbackOrchestrator()
    return _ORCHESTRATOR_INSTANCE
