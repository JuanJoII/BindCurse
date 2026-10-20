#!/usr/bin/env python3
"""
BindCurse UDP Audio Server v1.0
--------------------------------
Servidor UDP de audio de ultra baja latencia compatible con la app BindCurse.

Características:
- Responde a auto-descubrimiento en LAN (BINDCURSE_DISCOVER -> BINDCURSE_HERE)
- Negociación de enlace (BINDCURSE_HELLO -> BINDCURSE_ACK)
- Control de latencia y monitoreo de enlace (BINDCURSE_PING -> BINDCURSE_PONG)
- Recepción de paquetes de audio crudo en formato PCM 16-bit 48kHz mono
- Detección de pérdida de paquetes con número de secuencia (uint32 LE)
- Reproducción opcional en altavoces en tiempo real (vía PyAudio / sounddevice)
- Guardado opcional a archivo WAV o visualización de VU meter en consola
"""

import argparse
import os
import platform
import socket
import struct
import sys
import time
import wave
from datetime import datetime

# Protocol definitions
BINDCURSE_VERSION = 1
DEFAULT_PORT = 9920
SAMPLE_RATE = 48000
CHANNELS = 1
BYTES_PER_SAMPLE = 2  # 16-bit signed LE
AUDIO_MAGIC = b"BCPM"
AUDIO_HEADER_SIZE = 8


def get_hostname():
    try:
        return platform.node() or "BindCurse Altar"
    except Exception:
        return "BindCurse Altar"


def print_vu_meter(pcm_data, width=30):
    """Calcula y muestra en consola el nivel RMS de la ráfaga de audio."""
    if not pcm_data or len(pcm_data) < 2:
        return
    count = len(pcm_data) // 2
    samples = struct.unpack(f"<{count}h", pcm_data[: count * 2])
    sum_sq = sum(s * s for s in samples)
    rms = (sum_sq / count) ** 0.5
    normalized = min(1.0, rms / 32768.0)
    bars = int(normalized * width)
    peak_db = 20 * (math_log10(rms / 32768.0)) if rms > 0 else -60.0
    meter = "#" * bars + "-" * (width - bars)
    sys.stdout.write(f"\r[VU] [{meter}] {peak_db:6.1f} dBFS | Muestras: {count}")
    sys.stdout.flush()


def math_log10(x):
    import math

    return math.log10(max(1e-5, x))


def main():
    parser = argparse.ArgumentParser(description="BindCurse UDP Audio Server")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Puerto UDP a escuchar (por defecto: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "-b",
        "--bind",
        type=str,
        default="0.0.0.0",
        help="Dirección IP de enlace (por defecto: 0.0.0.0)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Ruta de archivo .wav para grabar la sesión",
    )
    parser.add_argument(
        "--play",
        action="store_true",
        help="Reproducir audio en vivo por los altavoces (requiere pyaudio o sounddevice)",
    )
    parser.add_argument(
        "--vu",
        action="store_true",
        help="Mostrar medidor de VU en tiempo real en la terminal",
    )
    args = parser.parse_args()

    # Audio playback setup (optional)
    audio_stream = None
    if args.play:
        try:
            import pyaudio

            p = pyaudio.PyAudio()
            audio_stream = p.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                output=True,
                frames_per_buffer=960,
            )
            print("[AUDIO] Salida de altavoces inicializada con PyAudio.")
        except ImportError:
            try:
                import sounddevice as sd

                audio_stream = sd.OutputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=960,
                )
                audio_stream.start()
                print("[AUDIO] Salida de altavoces inicializada con sounddevice.")
            except ImportError:
                print(
                    "[AVISO] Para escuchar el audio en vivo, instala 'pyaudio' o 'sounddevice':"
                )
                print("        pip install sounddevice numpy")

    # WAV recording setup (optional)
    wav_file = None
    if args.output:
        wav_file = wave.open(args.output, "wb")
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(BYTES_PER_SAMPLE)
        wav_file.setframerate(SAMPLE_RATE)
        print(f"[REC] Grabando sesión en: {args.output}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Enable address reuse
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass

    try:
        sock.bind((args.bind, args.port))
    except Exception as e:
        print(
            f"[ERROR FATAL] No se pudo vincular al puerto UDP {args.bind}:{args.port} -> {e}"
        )
        sys.exit(1)

    host_name = get_hostname()
    os_name = f"{platform.system()} {platform.release()}"

    print("=" * 65)
    print("   ⛧ BINDCURSE UDP AUDIO RECEIVER / ALTAR PRINCIPAL ⛧")
    print("=" * 65)
    print(f" Estado:       ESCUCHANDO")
    print(f" Host:         {host_name} ({os_name})")
    print(f" Puerto UDP:   {args.port}")
    print(f" Audio:        PCM 16-bit 48000Hz Mono")
    print("=" * 65)
    print("Esperando enlace de la app BindCurse...\n")

    current_client = None
    expected_seq = 0
    total_packets = 0
    lost_packets = 0
    total_bytes = 0
    start_time = None

    try:
        while True:
            data, addr = sock.recvfrom(65535)
            if not data:
                continue

            # Check if it is a text control message
            if data.startswith(b"BINDCURSE_"):
                try:
                    text = data.decode("utf-8", errors="ignore").strip()
                    parts = text.split("|")
                    cmd = parts[0]

                    # 1. LAN Discovery: BINDCURSE_DISCOVER
                    if cmd == "BINDCURSE_DISCOVER":
                        resp = f"BINDCURSE_HERE|{host_name}|{os_name}|{args.port}"
                        sock.sendto(resp.encode("utf-8"), addr)
                        print(
                            f"[DISCOVERY] Sonda recibida de {addr[0]}:{addr[1]} -> Respondido HERE"
                        )

                    # 2. Handshake: BINDCURSE_HELLO|<ver>|<sr>|<bits>|<ch>|<clientName>
                    elif cmd == "BINDCURSE_HELLO":
                        client_name = parts[5] if len(parts) > 5 else "Cliente"
                        current_client = addr
                        expected_seq = 0
                        total_packets = 0
                        lost_packets = 0
                        total_bytes = 0
                        start_time = time.time()
                        resp = f"BINDCURSE_ACK|{host_name}|{os_name}"
                        sock.sendto(resp.encode("utf-8"), addr)
                        print(
                            f"\n[PACTO SELLADO] Nuevo enlace con {client_name} en {addr[0]}:{addr[1]}"
                        )
                        print(
                            f"[AUDIO STREAM] Esperando flujo de audio PCM 16-bit 48kHz..."
                        )

                    # 3. Ping: BINDCURSE_PING|<seq>
                    elif cmd == "BINDCURSE_PING":
                        seq = parts[1] if len(parts) > 1 else "0"
                        resp = f"BINDCURSE_PONG|{seq}"
                        sock.sendto(resp.encode("utf-8"), addr)

                    # 4. Bye: BINDCURSE_BYE
                    elif cmd == "BINDCURSE_BYE":
                        print(
                            f"\n[DESVINCULADO] El cliente {addr[0]}:{addr[1]} ha finalizado la sesión."
                        )
                        if current_client == addr:
                            current_client = None

                except Exception as e:
                    print(f"[ERROR PROTOCOLO] {e}")
                continue

            # Check if it is an audio packet with BCPM header
            if data.startswith(AUDIO_MAGIC) and len(data) >= AUDIO_HEADER_SIZE:
                seq = struct.unpack("<I", data[4:8])[0]
                pcm_data = data[AUDIO_HEADER_SIZE:]

                total_packets += 1
                total_bytes += len(pcm_data)

                # Packet loss tracking
                if expected_seq != 0 and seq != expected_seq:
                    if seq > expected_seq:
                        lost = seq - expected_seq
                        lost_packets += lost
                expected_seq = (seq + 1) & 0xFFFFFFFF

                # Live playback
                if audio_stream:
                    try:
                        if hasattr(audio_stream, "write"):
                            audio_stream.write(pcm_data)
                    except Exception:
                        pass

                # File recording
                if wav_file:
                    wav_file.writeframes(pcm_data)

                # VU Meter display
                if args.vu and total_packets % 4 == 0:
                    print_vu_meter(pcm_data)

            # Raw PCM without BCPM header (direct stream fallback)
            elif current_client and addr[0] == current_client[0]:
                total_packets += 1
                total_bytes += len(data)
                if audio_stream and hasattr(audio_stream, "write"):
                    audio_stream.write(data)
                if wav_file:
                    wav_file.writeframes(data)
                if args.vu and total_packets % 4 == 0:
                    print_vu_meter(data)

    except KeyboardInterrupt:
        print("\n[SERVIDOR] Deteniendo altar UDP...")
    finally:
        sock.close()
        if wav_file:
            wav_file.close()
            print(f"[REC] Archivo guardado correctamente ({total_bytes} bytes).")
        if audio_stream:
            try:
                audio_stream.stop()
                audio_stream.close()
            except Exception:
                pass
        print(
            f"[RESUMEN] Total paquetes: {total_packets} | Perdidos: {lost_packets} | Bytes: {total_bytes}"
        )


if __name__ == "__main__":
    main()
