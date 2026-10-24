from __future__ import annotations

import socket
import subprocess
from typing import List, Tuple


def get_linux_ips(primary_ip: str | None = None) -> List[Tuple[str, str]]:
    """Returns local IPs using Linux iproute2 tools."""
    results: List[Tuple[str, str]] = []
    seen: set[str] = set()

    try:
        out = subprocess.check_output(
            ["ip", "-4", "-br", "addr", "show"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                iface = parts[0]
                for addr_part in parts[2:]:
                    ip = addr_part.split("/")[0]
                    if ip and not ip.startswith("127."):
                        if ip not in seen:
                            seen.add(ip)
                            label = (
                                f"{iface} (Principal)" if ip == primary_ip else iface
                            )
                            if ip == primary_ip:
                                results.insert(0, (label, ip))
                            else:
                                results.append((label, ip))
    except Exception:
        pass

    return results


def get_windows_ips(primary_ip: str | None = None) -> List[Tuple[str, str]]:
    """Returns local IPs using hostname resolution for Windows."""
    results: List[Tuple[str, str]] = []
    seen: set[str] = set()

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(info[4][0])
            if ip and not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                label = "Red Local (Principal)" if ip == primary_ip else "Red Local"
                if ip == primary_ip:
                    results.insert(0, (label, ip))
                else:
                    results.append((label, ip))
    except OSError:
        pass

    return results


def probe_primary_ip() -> str | None:
    """Probes the primary outbound interface IP via a dummy UDP connection."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return str(ip)
    except OSError:
        pass
    return None
