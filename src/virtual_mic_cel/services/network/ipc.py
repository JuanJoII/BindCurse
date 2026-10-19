from __future__ import annotations

import json
import socket
from typing import Any, Dict


def send_ipc_command(control_port: int, command: str) -> Dict[str, Any] | None:
    """
    Sends a control command ('SHOW', 'HIDE', 'STOP', 'STATUS', 'TOGGLE_MONITOR', 'TOGGLE_MUTE')
    to an already running VirtualMicApp instance on 127.0.0.1:control_port.
    Returns parsed JSON response if an instance is running, or None if not running.
    """
    try:
        with socket.create_connection(("127.0.0.1", control_port), timeout=0.6) as sock:
            sock.sendall((command.strip().upper() + "\n").encode("utf-8"))
            raw = sock.recv(8192).decode("utf-8", errors="replace").strip()
            if raw:
                return json.loads(raw)
            return {"ok": True}
    except OSError:
        return None
    except Exception:
        return {"ok": True}
