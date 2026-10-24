from __future__ import annotations

import json
import socket
from typing import Any, Dict


def send_ipc_command(control_port: int, command: str) -> Dict[str, Any] | None:
    """
    Sends a control command ('SHOW', 'HIDE', 'STOP', 'STATUS', 'TOGGLE_MONITOR', 'TOGGLE_MUTE', etc.)
    to an already running VirtualMicApp instance on 127.0.0.1:control_port.
    Returns parsed JSON response if an instance is running, or None if not running.
    """
    try:
        parts = command.strip().split("|", 1)
        normalized = parts[0].strip().upper()
        if len(parts) > 1:
            normalized = f"{normalized}|{parts[1].strip()}"
        with socket.create_connection(("127.0.0.1", control_port), timeout=0.6) as sock:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            sock.sendall((normalized + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                buf = sock.recv(8192)
                if not buf:
                    break
                chunks.append(buf)
                if b"\n" in buf:
                    break
            raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
            if raw:
                return json.loads(raw)
            return {"ok": True}
    except OSError:
        return None
    except Exception:
        return {"ok": True}
