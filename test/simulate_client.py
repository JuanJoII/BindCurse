#!/usr/bin/env python3
"""
Simulador de cliente móvil BindCurse para pruebas de conexión UDP.
Ejecuta los handshakes del protocolo:
1. BINDCURSE_DISCOVER -> Recibe BINDCURSE_HERE
2. BINDCURSE_HELLO    -> Recibe BINDCURSE_ACK
3. BINDCURSE_PING     -> Recibe BINDCURSE_PONG y calcula RTT (latencia)
4. Transmisión de paquetes de audio con cabecera BCPM y número de secuencia
5. BINDCURSE_BYE      -> Desconexión limpia
"""

import argparse
import socket
import struct
import time
import math


def main():
    parser = argparse.ArgumentParser(description="Simulador de cliente móvil BindCurse")
    parser.add_argument(
        "-H",
        "--host",
        default="127.0.0.1",
        help="IP del servidor (por defecto: 127.0.0.1)",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=5005,
        help="Puerto UDP del servidor (por defecto: 5005)",
    )
    parser.add_argument(
        "-n", "--name", default="FlutterPhone_Test", help="Nombre del cliente móvil"
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=5.0,
        help="Duración del audio de prueba en segundos",
    )
    args = parser.parse_args()

    target = (args.host, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)

    print("=" * 60)
    print("   Simulador Cliente Móvil BindCurse")
    print("=" * 60)
    print(f"Destino: {args.host}:{args.port}")
    print(f"Dispositivo: {args.name}")
    print("=" * 60)

    # 1. Discovery
    print("\n[1] Enviando BINDCURSE_DISCOVER...")
    sock.sendto(b"BINDCURSE_DISCOVER", target)
    try:
        data, addr = sock.recvfrom(1024)
        print(
            f"    <- Respuesta recibida de {addr}: {data.decode('utf-8', errors='replace')}"
        )
    except socket.timeout:
        print("    [!] Tiempo de espera agotado para DISCOVER")

    # 2. Handshake Hello
    print("\n[2] Enviando BINDCURSE_HELLO...")
    hello_msg = f"BINDCURSE_HELLO|1|48000|16|1|{args.name}"
    sock.sendto(hello_msg.encode("utf-8"), target)
    try:
        data, addr = sock.recvfrom(1024)
        print(
            f"    <- Respuesta recibida de {addr}: {data.decode('utf-8', errors='replace')}"
        )
    except socket.timeout:
        print("    [!] Tiempo de espera agotado para HELLO")

    # 3. Ping / Pong Latency
    print("\n[3] Enviando BINDCURSE_PING para medir latencia RTT...")
    t0 = time.time()
    sock.sendto(b"BINDCURSE_PING|1", target)
    try:
        data, addr = sock.recvfrom(1024)
        rtt_ms = (time.time() - t0) * 1000.0
        print(f"    <- {data.decode('utf-8', errors='replace')} (RTT: {rtt_ms:.2f} ms)")
    except socket.timeout:
        print("    [!] Tiempo de espera agotado para PING")

    # 4. Stream Audio Packets with BCPM header
    print(
        f"\n[4] Transmitiendo audio PCM 48kHz mono con cabecera BCPM ({args.duration}s)..."
    )
    sample_rate = 48000
    chunk_samples = 960  # 20ms
    total_chunks = int((args.duration * sample_rate) / chunk_samples)

    for seq in range(total_chunks):
        sine_bytes = bytearray()
        for i in range(chunk_samples):
            sample_val = int(
                math.sin(
                    2.0 * math.pi * 440.0 * (seq * chunk_samples + i) / sample_rate
                )
                * 16000.0
            )
            sine_bytes.extend(struct.pack("<h", sample_val))

        packet = b"BCPM" + struct.pack("<I", seq) + sine_bytes
        sock.sendto(packet, target)
        time.sleep(chunk_samples / sample_rate)

    print(f"    Transmisión finalizada ({total_chunks} paquetes enviados).")

    # 5. Disconnect Bye
    print("\n[5] Enviando BINDCURSE_BYE para finalizar sesión...")
    sock.sendto(b"BINDCURSE_BYE", target)
    print("    Sesión cerrada correctamente.")

    sock.close()


if __name__ == "__main__":
    main()
